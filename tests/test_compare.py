"""B5.6 run-vs-run compare (RVR-01..03, D-62, D-65): `evalkit compare OLD NEW` and
`evalkit.compare`. Every test runs offline: no network, no model call, no key.

Fixtures: tests/fixtures/compare/assay/old is a byte copy of assay-pipeline's real export
(tests/fixtures/gates/assay); new is the same run where gpt-6.1-sol gains one attack success
(one public attack task fails pass^k). Each carries a per-case sidecar of pass^k over the 40
public attack tasks per agent, with held-out rows that carry no case id.
tests/fixtures/compare/respectful_offers/new perturbs respectful-offers' real export (new code
version and config hash, one interval moved, one metric dropped, one added, a rules hash).
"""

from __future__ import annotations

import json
import math
import random
import shutil
from pathlib import Path
from typing import Any

import pytest

from evalkit import cli
from evalkit.compare import (
    COMPARE_SCHEMA,
    EXIT_INPUT,
    EXIT_PASS,
    EXIT_REGRESSION,
    CompareError,
    Comparison,
    compare,
    compare_runs,
    load_input,
    paired_bootstrap,
    render_markdown,
    report_json,
)
from evalkit.gates import parse_gates
from evalkit.stats import Resamples, clopper_pearson, holm, paired

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
ASSAY_OLD = FIX / "compare" / "assay" / "old"
ASSAY_NEW = FIX / "compare" / "assay" / "new"
ASSAY_GATES = FIX / "gates" / "assay" / "gates.yaml"
RO_OLD = FIX / "gates" / "respectful_offers" / "metrics.json"
RO_NEW = FIX / "compare" / "respectful_offers" / "new" / "metrics.json"
EXAMPLE_RUN = ROOT / "src" / "evalkit" / "examples" / "example_run"
G = "gpt-6.1-sol"
S = "claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Comparing never opens a connection and needs no key; any attempt fails the test."""
    import socket

    def refuse(*a, **k):
        raise AssertionError("evalkit compare must not touch the network")

    real_connect = socket.socket.connect
    loopback = {"127.0.0.1", "::1", "localhost"}

    def connect(sock, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else None
        if host in loopback:  # asyncio's Windows self-pipe is a loopback socketpair
            return real_connect(sock, address, *a, **k)
        return refuse()

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---- helpers ----------------------------------------------------------------------------------


def metrics_doc(metrics: list[dict[str, Any]], **top: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "metrics-1",
        "project": "course-assistant",
        "run_id": "r1",
        "created_at": "2026-11-17T00:00:00Z",
        "metrics": metrics,
    }
    base.update(top)
    return base


def write_run(
    folder: Path,
    metrics: list[dict[str, Any]],
    cases: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    **top: Any,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "metrics.json").write_text(json.dumps(metrics_doc(metrics, **top)), "utf-8")
    if cases is not None:
        (folder / "cases.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8"
        )
    if settings is not None:
        (folder / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return folder / "metrics.json"


def rows(c: Comparison, metric: str, **labels: Any) -> list[dict[str, Any]]:
    want = {k: str(v) for k, v in labels.items()}
    return [
        ch
        for ch in c.as_dict()["changes"]
        if ch["metric"] == metric and all(ch["labels"].get(k) == v for k, v in want.items())
    ]


def one(c: Comparison, metric: str, **labels: Any) -> dict[str, Any]:
    found = rows(c, metric, **labels)
    assert len(found) == 1, found
    return found[0]


def cli_compare(tmp: Path, old: Path, new: Path, *extra: str) -> tuple[int, dict[str, Any], str]:
    out = tmp / "compare.json"
    md = tmp / "compare.md"
    code = cli.main(["compare", str(old), str(new), "--out", str(out), "--md", str(md), *extra])
    data = json.loads(out.read_text(encoding="utf-8"))
    text = md.read_text(encoding="utf-8") if md.exists() else ""
    return code, data, text


def independent_paired(
    old: dict[str, list[float]], new: dict[str, list[float]], draws: int, seed: int
) -> tuple[float, float, float]:
    """A second, separately written paired two-level bootstrap: resample shared cases, then each
    side's trials within each drawn case; 2.5% and 97.5% linear-interpolation percentiles."""
    rng = random.Random(f"independent:{seed}")
    ids = sorted(set(old) & set(new))
    n = len(ids)

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs)

    point = mean([mean(new[c]) - mean(old[c]) for c in ids])
    boot = []
    for _ in range(draws):
        total = 0.0
        for _ in range(n):
            c = ids[rng.randrange(n)]
            o = [old[c][rng.randrange(len(old[c]))] for _ in old[c]]
            w = [new[c][rng.randrange(len(new[c]))] for _ in new[c]]
            total += mean(w) - mean(o)
        boot.append(total / n)
    boot.sort()

    def q(p: float) -> float:
        pos = p * (len(boot) - 1)
        lo = math.floor(pos)
        hi = min(lo + 1, len(boot) - 1)
        return boot[lo] + (boot[hi] - boot[lo]) * (pos - lo)

    return point, q(0.025), q(0.975)


def mover(p_new, lo_new, hi_new, p_old, lo_old, hi_old) -> tuple[float, float, float]:
    """MOVER (Zou & Donner 2008) for new - old from two independent intervals."""
    d = p_new - p_old
    lo = d - math.sqrt((p_new - lo_new) ** 2 + (hi_old - p_old) ** 2)
    hi = d + math.sqrt((hi_new - p_new) ** 2 + (p_old - lo_old) ** 2)
    return d, lo, hi


# ---- RVR-01: paired two-level bootstrap, checked independently ----------------------------------


def _trial_data(seed: int = 3) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    rng = random.Random(seed)
    old, new = {}, {}
    for i in range(30):
        p = rng.random()
        old[f"c{i:02d}"] = [float(rng.random() < p) for _ in range(3)]
        new[f"c{i:02d}"] = [float(rng.random() < min(1.0, p + 0.15)) for _ in range(3)]
    return old, new


def test_paired_two_level_matches_an_independent_paired_bootstrap():
    old, new = _trial_data()
    iv, p, method = paired_bootstrap(old, new, seed=7, resamples=10_000)
    point, lo, hi = independent_paired(old, new, 10_000, 11)
    assert iv.point == pytest.approx(point, abs=1e-12)
    assert iv.lo == pytest.approx(lo, abs=0.012)
    assert iv.hi == pytest.approx(hi, abs=0.012)
    assert method == "paired two-level bootstrap (cases, then trials)"
    assert 0 < p <= 1
    # the trial level is really resampled: wider than resampling case means alone
    means_old = [sum(v) / 3 for v in old.values()]
    means_new = [sum(v) / 3 for v in new.values()]
    case_only, _ = paired(means_new, means_old, Resamples(30, 10_000, seed=7))
    assert (iv.hi - iv.lo) > (case_only.hi - case_only.lo)


def test_case_only_resample_when_there_is_no_trial_level_equals_stats_paired():
    rng = random.Random(5)
    old = {f"t{i}": [float(rng.random() < 0.7)] for i in range(25)}
    new = {f"t{i}": [float(rng.random() < 0.5)] for i in range(25)}
    iv, p, method = paired_bootstrap(old, new, seed=7, resamples=10_000)
    ids = sorted(old)
    want, want_p = paired([new[c][0] for c in ids], [old[c][0] for c in ids], Resamples(25, seed=7))
    assert (iv.point, iv.lo, iv.hi, p) == (want.point, want.lo, want.hi, want_p)
    assert method == "paired case bootstrap (no trial level)"


def test_paired_bootstrap_uses_only_shared_cases():
    old = {"a": [1.0], "b": [0.0], "c": [1.0], "only_old": [1.0]}
    new = {"a": [0.0], "b": [0.0], "c": [0.0], "only_new": [0.0]}
    iv, _, _ = paired_bootstrap(old, new, resamples=1000)
    assert iv.point == pytest.approx(-2 / 3)
    with pytest.raises(ValueError):
        paired_bootstrap({"a": [1.0]}, {"b": [1.0]}, resamples=1000)


def test_assay_one_gained_attack_success_pass_k_paired_over_cases_only():
    c = compare_runs(ASSAY_OLD, ASSAY_NEW)
    assert c.exit_code == EXIT_PASS  # no gates file: never exit 1
    row = one(c, "attack_pass_k", model=G, split="public")
    assert row["pairing"] == "paired"
    assert row["method"] == "paired case bootstrap (no trial level)"
    assert row["n_shared"] == 40 and row["not_paired"] == {"old": 0, "new": 0}
    assert (row["old"], row["new"]) == (1.0, 0.975)
    assert row["diff"] == pytest.approx(-1 / 40)
    # one lost task of 40: the resampled mean is -j/40, so the interval is on that grid
    assert row["ci_low"] == pytest.approx(-3 / 40) and row["ci_high"] == 0.0
    assert row["flagged"] is False
    # identical per-case values: a zero change, paired too
    same = one(c, "attack_pass_k", model=S, split="public")
    assert (same["pairing"], same["diff"], same["ci_low"], same["ci_high"]) == (
        "paired",
        0.0,
        0.0,
        0.0,
    )
    # the trial-level rate has no per-case values: unpaired, exact intervals combined (MOVER)
    rate = one(c, "attack_trial_success_rate", model=G)
    assert rate["pairing"] == "unpaired"
    a, b = clopper_pearson(1, 150), clopper_pearson(0, 150)
    d, lo, hi = mover(1 / 150, a.lo, a.hi, 0.0, b.lo, b.hi)
    assert rate["diff"] == pytest.approx(d, abs=1e-6)
    assert (rate["ci_low"], rate["ci_high"]) == (pytest.approx(lo, abs=1e-6), pytest.approx(hi))
    assert rate["interval_sources"] == {"old": "clopper-pearson", "new": "clopper-pearson"}
    assert rate["flagged"] is False  # one success in 150 is not a significant change
    # a count without an interval: the change is shown, labelled, never flagged
    count = one(c, "attack_successes", model=G, split="public")
    assert (count["diff"], count["ci_low"], count["pairing"]) == (1, None, "unpaired")
    assert count["method"] == "unpaired: no interval"


def test_unpaired_rows_are_labelled_in_json_and_markdown(tmp_path):
    code, data, text = cli_compare(tmp_path, RO_OLD, RO_NEW)
    assert code == EXIT_PASS
    labels = {ch["pairing"] for ch in data["changes"]}
    assert labels == {"unpaired"}  # no sidecar on either side
    for ch in data["changes"]:
        assert ch["method"].startswith("unpaired")
    table = [ln for ln in text.splitlines() if ln.startswith("| diff.")]
    assert table and all("unpaired" in ln for ln in table)


# ---- RVR-02: Holm flags, settings, outputs, held-out ------------------------------------------


def test_holm_flags_only_after_adjustment_and_where_the_interval_excludes_zero(tmp_path):
    # 12 metrics with a modest shift and one with a large one; per-case values on both sides
    rng = random.Random(1)
    metrics_old, metrics_new, cases_old, cases_new = [], [], [], []
    names = [f"m{i:02d}" for i in range(12)] + ["big"]
    for name in names:
        shift = 0.5 if name == "big" else 0.12
        o = [float(rng.random() < 0.5) for _ in range(60)]
        w = [float(rng.random() < 0.5 + shift) for _ in range(60)]
        metrics_old.append({"name": name, "unit": "rate", "value": sum(o) / 60, "n": 60})
        metrics_new.append({"name": name, "unit": "rate", "value": sum(w) / 60, "n": 60})
        cases_old += [{"case_id": f"c{j}", "metric": name, "value": v} for j, v in enumerate(o)]
        cases_new += [{"case_id": f"c{j}", "metric": name, "value": v} for j, v in enumerate(w)]
    c = compare_runs(
        write_run(tmp_path / "old", metrics_old, cases_old),
        write_run(tmp_path / "new", metrics_new, cases_new),
    )
    data = c.as_dict()
    ps = {ch["metric"]: ch["p_value"] for ch in data["changes"]}
    rejected = holm(ps)
    for ch in data["changes"]:
        excludes = ch["ci_low"] > 0 or ch["ci_high"] < 0
        assert ch["flagged"] == (rejected[ch["metric"]] and excludes)
        assert ch["p_holm"] >= ch["p_value"]
    assert one(c, "big")["flagged"] is True
    unadjusted = [ch for ch in data["changes"] if ch["p_value"] <= 0.05 and not ch["flagged"]]
    assert unadjusted, "some rows pass unadjusted but not after Holm (the test's premise)"
    assert data["holm"] == {"alpha": 0.05, "family": 13}


def test_settings_differences_are_listed_at_the_top(tmp_path):
    code, data, text = cli_compare(tmp_path, RO_OLD, RO_NEW)
    assert code == EXIT_PASS  # different settings are stated, not an error
    diffs = {d["setting"]: d for d in data["settings"]["differences"]}
    assert diffs["code_version"]["old"] == "2faad55e0a77"
    assert diffs["code_version"]["new"] == "7c41d09be2aa"
    assert diffs["config_hash"]["new"] == "a0b1c2d3e4f50617"
    assert diffs["rules_hash"] == {
        "setting": "rules_hash",
        "old": None,
        "new": "r2-41aa09",
        "note": "recorded on one side only",
    }
    assert "snapshot" not in diffs  # recorded identically: not a difference
    assert data["settings"]["status"] == "different"
    head = text.split("| metric")[0]
    assert "Settings differ" in head and "code_version" in head and "config_hash" in head


def test_respectful_offers_moved_interval_is_an_unpaired_flagged_change(tmp_path):
    c = compare_runs(RO_OLD, RO_NEW)
    row = one(
        c,
        "diff.incremental_retained_per_1000_ntd",
        a="bandit",
        b="rule_following_baseline",
        variant="central",
    )
    d, lo, hi = mover(3.912004, 3.401187, 4.430918, 5.570037, 5.064173, 6.096223)
    assert row["diff"] == pytest.approx(d)
    assert (row["ci_low"], row["ci_high"]) == (pytest.approx(lo), pytest.approx(hi))
    assert row["interval_sources"] == {"old": "supplied", "new": "supplied"}
    assert row["pairing"] == "unpaired" and row["flagged"] is True
    assert row["direction"] == "down"
    # unchanged rows: zero change, not flagged
    same = one(c, "retained_per_1000", policy="no_offer", variant="conservative")
    assert same["diff"] == 0.0 and not same["flagged"]


def test_json_and_markdown_outputs(tmp_path, capsys):
    code, data, text = cli_compare(tmp_path, ASSAY_OLD, ASSAY_NEW)
    assert code == 0
    assert data["schema_version"] == COMPARE_SCHEMA == "compare-1"
    assert data["seed"] == 7 and data["resamples"] == 10_000
    for key in ("old", "new", "settings", "changes", "missing", "errors", "exit_code", "verdict"):
        assert key in data
    assert data["old"]["run_id"] == "main-2026-11-16" and data["new"]["run_id"] == "main-2026-11-17"
    assert "| metric | labels | old | new | change (95% CI) | pairing | flag |" in text
    printed = capsys.readouterr().out
    assert "| metric |" in printed and "compare.json" in printed
    code = cli.main(
        ["compare", str(ASSAY_OLD), str(ASSAY_NEW), "--json", "--out", str(tmp_path / "c.json")]
    )
    printed = capsys.readouterr().out
    assert json.loads(printed) == json.loads((tmp_path / "c.json").read_text("utf-8"))


def test_heldout_rows_are_split_level_aggregates_only(tmp_path):
    c = compare_runs(ASSAY_OLD, ASSAY_NEW, gates=ASSAY_GATES)
    held = [ch for ch in c.as_dict()["changes"] if ch.get("heldout_rows")]
    assert held, "the assay export has held-out rows"
    for ch in held:
        assert set(ch["labels"]) <= {"split", "model", "judge"}
        assert ch["labels"]["split"] == "heldout"
        assert ch["pairing"] == "unpaired"  # held-out rows are never paired
    pk = [ch for ch in held if ch["metric"] == "attack_pass_k" and ch["labels"]["model"] == G]
    # the sidecar's held-out rows carry no case id: unpaired, from the per-case values; every
    # case passes on both sides, so the bootstrap has zero width and the exact interval over
    # the cases (Clopper-Pearson) stands in: never a zero-width "certain" interval
    assert pk[0]["interval_sources"] == {
        "old": "clopper-pearson (cases)",
        "new": "clopper-pearson (cases)",
    }
    assert pk[0]["interval_status"] == "ok" and pk[0]["ci_low"] < 0 < pk[0]["ci_high"]


def test_heldout_case_ids_are_never_printed_exposure_check(tmp_path):
    """B5.14's exposure-style check: no held-out case id in any output, with a manifest that
    pins some of the run's cases as held-out and a sidecar that (wrongly) names them."""
    from evalkit.heldout import build_manifest, ids_in_text

    held_ids = [f"atk-{i:02d}" for i in range(31, 41)]
    pub_ids = [f"atk-{i:02d}" for i in range(1, 31)]
    (tmp_path / "held.jsonl").write_text(
        "".join(json.dumps({"id": i, "q": f"held {i}"}) + "\n" for i in held_ids), "utf-8"
    )
    (tmp_path / "pub.jsonl").write_text(
        "".join(json.dumps({"id": i, "q": f"pub {i}"}) + "\n" for i in pub_ids), "utf-8"
    )
    manifest = build_manifest(
        {"heldout": tmp_path / "held.jsonl", "public": tmp_path / "pub.jsonl"}
    )
    mpath = tmp_path / "MANIFEST.json"
    mpath.write_text(json.dumps(manifest), "utf-8")
    code, data, text = cli_compare(
        tmp_path, ASSAY_OLD, ASSAY_NEW, "--manifest", str(mpath), "--gates", str(ASSAY_GATES)
    )
    assert code == EXIT_PASS
    blob = json.dumps(data) + "\n" + text
    assert ids_in_text(blob, manifest, split="heldout") == []
    assert ids_in_text(blob + " atk-31", manifest, split="heldout")  # the check would catch one
    row = one(
        compare_runs(ASSAY_OLD, ASSAY_NEW, manifest=mpath), "attack_pass_k", model=G, split="public"
    )
    assert row["n_shared"] == 30  # the 10 manifest held-out cases lost their ids
    assert row["not_paired"] == {"old": 10, "new": 10}
    # no case id of any kind is printed, public or held-out
    for i in pub_ids + held_ids:
        assert i not in blob


# ---- RVR-03: baselines, sidecar, missing --------------------------------------------------------

CA_GATES = """
gates:
  - metric: rubric_pass_rate
    op: ">="
    value: 0.8
  - metric: hallucination_rate
    op: "<="
    value: 0.05
"""


def _ca_cases(metric: str, values: list[float], ids: list[str] | None = None) -> list[dict]:
    ids = ids or [f"q{i:02d}" for i in range(len(values))]
    return [{"case_id": i, "metric": metric, "value": v} for i, v in zip(ids, values, strict=True)]


def _ca_metrics(rubric: float, halluc: float | None, n: int = 20) -> list[dict]:
    out = [{"name": "rubric_pass_rate", "unit": "rate", "value": rubric, "n": n}]
    if halluc is not None:
        out.append({"name": "hallucination_rate", "unit": "rate", "value": halluc, "n": n})
    return out


def test_course_assistant_baseline_missing_a_gated_metric_exits_2(tmp_path):
    gates = tmp_path / "gates.yaml"
    gates.write_text(CA_GATES, "utf-8")
    vals = [1.0] * 18 + [0.0] * 2
    old = write_run(tmp_path / "old", _ca_metrics(0.9, None), _ca_cases("rubric_pass_rate", vals))
    new = write_run(
        tmp_path / "new",
        _ca_metrics(0.9, 0.0),
        _ca_cases("rubric_pass_rate", vals) + _ca_cases("hallucination_rate", [0.0] * 20),
    )
    code, data, text = cli_compare(tmp_path, old, new, "--gates", str(gates))
    assert code == EXIT_INPUT == 2
    assert any("baseline" in e and "hallucination_rate" in e for e in data["errors"])
    assert data["verdict"] == "error"
    # never silently skipped: listed as missing on the old side too
    assert {"side": "old", "metric": "hallucination_rate", "labels": {}, "what": "metric"} in data[
        "missing"
    ]
    assert "hallucination_rate" in text


def test_course_assistant_fewer_than_two_shared_cases_exits_2(tmp_path):
    gates = tmp_path / "gates.yaml"
    gates.write_text(CA_GATES, "utf-8")
    old = write_run(
        tmp_path / "old",
        _ca_metrics(2 / 3, 0.0, n=3),  # the sidecar must average to the metric's value
        _ca_cases("rubric_pass_rate", [1.0, 1.0, 0.0], ["a", "b", "c"])
        + _ca_cases("hallucination_rate", [0.0, 0.0, 0.0], ["a", "b", "c"]),
    )
    new = write_run(
        tmp_path / "new",
        _ca_metrics(1.0, 0.0, n=3),
        _ca_cases("rubric_pass_rate", [1.0, 1.0, 1.0], ["c", "d", "e"])  # one shared case
        + _ca_cases("hallucination_rate", [0.0, 0.0, 0.0], ["a", "b", "c"]),
    )
    c = compare_runs(old, new, gates=gates)
    assert c.exit_code == EXIT_INPUT
    assert any("rubric_pass_rate" in e and "1 shared case" in e for e in c.errors)
    assert not any("hallucination_rate" in e for e in c.errors)


def test_course_assistant_recorded_settings_differences_are_listed(tmp_path):
    vals = [1.0] * 18 + [0.0] * 2
    cases = _ca_cases("rubric_pass_rate", vals) + _ca_cases("hallucination_rate", [0.0] * 20)
    old = write_run(
        tmp_path / "old",
        _ca_metrics(0.9, 0.0),
        cases,
        settings={"rules_hash": "aaa111", "bar": 0.8, "assessments_hash": "s1"},
        provenance={"config_hash": "c1", "code_version": "v1"},
    )
    new = write_run(
        tmp_path / "new",
        _ca_metrics(0.9, 0.0),
        cases,
        settings={"rules_hash": "bbb222", "bar": 0.85, "assessments_hash": "s1"},
        provenance={"config_hash": "c2", "code_version": "v1"},
    )
    c = compare_runs(old, new)
    got = {d.setting: (d.old, d.new) for d in c.settings}
    assert got == {
        "rules_hash": ("aaa111", "bbb222"),
        "bar": ("0.8", "0.85"),
        "config_hash": ("c1", "c2"),
    }
    md = render_markdown(c)
    assert "rules_hash" in md and "bar" in md and "assessments_hash" not in md


def test_a_gated_regression_in_a_fail_gate_exits_1_and_a_warn_gate_exits_0(tmp_path):
    old_v = [1.0] * 38 + [0.0] * 2
    new_v = [1.0] * 20 + [0.0] * 20
    ids = [f"q{i:02d}" for i in range(40)]
    old = write_run(
        tmp_path / "old",
        [{"name": "rubric_pass_rate", "unit": "rate", "value": 0.95, "n": 40}],
        _ca_cases("rubric_pass_rate", old_v, ids),
    )
    new = write_run(
        tmp_path / "new",
        [{"name": "rubric_pass_rate", "unit": "rate", "value": 0.5, "n": 40}],
        _ca_cases("rubric_pass_rate", new_v, ids),
    )
    fail = tmp_path / "fail.yaml"
    fail.write_text("gates:\n  - {metric: rubric_pass_rate, op: '>=', value: 0.8}\n", "utf-8")
    warn = tmp_path / "warn.yaml"
    warn.write_text(
        "gates:\n  - {metric: rubric_pass_rate, op: '>=', value: 0.8, severity: warn}\n", "utf-8"
    )
    c = compare_runs(old, new, gates=fail)
    row = one(c, "rubric_pass_rate")
    assert row["flagged"] and row["regression"] and row["gate"] == "fail"
    assert c.exit_code == EXIT_REGRESSION == 1 and c.verdict == "regression"
    w = compare_runs(old, new, gates=warn)
    assert one(w, "rubric_pass_rate")["regression"] is True
    assert w.exit_code == EXIT_PASS
    # the same drop read the other way (lower is better) is an improvement, not a regression
    lower = tmp_path / "lower.yaml"
    lower.write_text("gates:\n  - {metric: rubric_pass_rate, op: '<=', value: 0.8}\n", "utf-8")
    assert compare_runs(old, new, gates=lower).exit_code == EXIT_PASS
    assert compare_runs(old, new).exit_code == EXIT_PASS  # no gates: never 1


def test_sidecar_is_optional_and_heldout_rows_without_case_id_fall_back_to_unpaired(tmp_path):
    shutil.copytree(ASSAY_OLD, tmp_path / "old")
    shutil.copytree(ASSAY_NEW, tmp_path / "new")
    (tmp_path / "old" / "cases.jsonl").unlink()  # one side without a sidecar
    c = compare_runs(tmp_path / "old", tmp_path / "new")
    assert c.exit_code == EXIT_PASS
    row = one(c, "attack_pass_k", model=G, split="public")
    assert row["pairing"] == "unpaired"
    assert row["interval_sources"] == {"old": "clopper-pearson", "new": "bootstrap"}
    # id-less public rows on both sides: unpaired from both per-case bootstraps
    for side in ("old", "new"):
        src = (ASSAY_OLD if side == "old" else ASSAY_NEW) / "cases.jsonl"
        lines = [json.loads(x) for x in src.read_text("utf-8").splitlines()]
        for x in lines:
            x.pop("case_id", None)
        (tmp_path / side / "cases.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in lines), "utf-8"
        )
    row = one(
        compare_runs(tmp_path / "old", tmp_path / "new"), "attack_pass_k", model=G, split="public"
    )
    assert row["pairing"] == "unpaired"
    # old passes all 40: a zero-width bootstrap, so its exact interval over the cases stands in
    assert row["interval_sources"] == {"old": "clopper-pearson (cases)", "new": "bootstrap"}


def test_missing_metrics_and_labels_are_listed_never_skipped():
    c = compare_runs(RO_OLD, RO_NEW)
    missing = c.as_dict()["missing"]
    assert {
        "side": "new",
        "metric": "holdout_check",
        "labels": {"variant": "optimistic"},
        "what": "labels",
    } in missing
    assert {
        "side": "old",
        "metric": "offers_per_member_month",
        "labels": {"policy": "no_offer", "variant": "pessimistic"},
        "what": "labels",
    } in missing
    md = render_markdown(c)
    assert "Missing" in md and "holdout_check" in md and "pessimistic" in md
    # every key on both sides is either compared or listed as missing
    old = load_input(RO_OLD).metrics.metrics
    new = load_input(RO_NEW).metrics.metrics
    assert len(c.changes) + len(missing) == len({(m.name, m.label_key()) for m in (*old, *new)})


def test_run_folders_pair_on_records_and_list_setting_differences(tmp_path):
    from evalkit import run as R
    from evalkit.pipeline.aggregate import write_summary
    from evalkit.schemas import Record

    old = tmp_path / "old"
    new = tmp_path / "new"
    shutil.copytree(EXAMPLE_RUN, old)
    shutil.copytree(EXAMPLE_RUN, new)
    records = R.read_records(new)
    changed = []
    flipped = 0
    for r in records:
        data = json.loads(r.model_dump_json())
        data["prompt_hash"] = "new-template-01"
        if (
            r.completion.model == "gpt-6.1-sol"
            and r.scores
            and r.scores.label_correct
            and flipped < 3
        ):
            data["scores"]["label_correct"] = False
            data["scores"]["correct_and_grounded"] = {
                k: False for k in data["scores"]["correct_and_grounded"]
            }
            flipped += 1
        changed.append(Record.model_validate(data))
    assert flipped >= 2
    (new / "records.jsonl").write_text(
        "".join(x.model_dump_json() + "\n" for x in changed), "utf-8"
    )
    manifest = R.read_manifest(new)
    write_summary(R.summarise(new, manifest, changed, "complete"), new / "summary.json")
    c = compare_runs(old, new)
    assert c.exit_code == EXIT_PASS
    acc = one(c, "accuracy", model="gpt-6.1-sol")
    assert acc["pairing"] == "paired" and acc["n_shared"] == 12
    assert acc["diff"] == pytest.approx(-flipped / 12)
    assert acc["diff"] == pytest.approx(acc["new"] - acc["old"])
    assert one(c, "accuracy", model="claude-sonnet-5-5")["diff"] == 0.0
    head = one(c, "headline", model="gpt-6.1-sol", judge="claude-opus-5-5")
    assert head["pairing"] == "paired"
    diffs = {d.setting for d in c.settings}
    assert "prompt_hash" in diffs
    assert load_input(old / "summary.json").kind == "evalkit-run"
    cost = [ch for ch in c.as_dict()["changes"] if ch["metric"] == "cost_per_case"]
    assert cost and all(ch["pairing"] == "paired" for ch in cost)  # records carry cost per case


# ---- input errors, determinism, safety ----------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ({"case_id": "a", "metric": "rubric_pass_rate", "value": 1, "extra": 1}, "extra"),
        ({"case_id": "a", "metric": "nope", "value": 1}, "no metric"),
        ({"case_id": "a", "metric": "rubric_pass_rate", "value": "x"}, "value"),
        ({"case_id": "", "metric": "rubric_pass_rate", "value": 1}, "case_id"),
        (
            {"case_id": "a", "metric": "rubric_pass_rate", "value": 1, "labels": {"user_id": 3}},
            "id",
        ),
        ({"case_id": "a", "metric": "rubric_pass_rate", "value": 1, "trial": -1}, "trial"),
    ],
)
def test_bad_sidecar_lines_exit_2_without_echoing_values(tmp_path, line, why):
    old = write_run(tmp_path / "old", _ca_metrics(0.9, None), [line])
    new = write_run(tmp_path / "new", _ca_metrics(0.9, None))
    c = compare_runs(old, new)
    assert c.exit_code == EXIT_INPUT
    assert any("cases.jsonl line 1" in e for e in c.errors), c.errors
    assert "secret-case-text" not in json.dumps(c.as_dict())


def test_duplicate_and_mixed_trial_rows_are_refused(tmp_path):
    dup = [{"case_id": "a", "metric": "rubric_pass_rate", "value": 1}] * 2
    with pytest.raises(CompareError, match="line 2"):
        load_input(write_run(tmp_path / "d", _ca_metrics(0.9, None), dup))
    mixed = [
        {"case_id": "a", "metric": "rubric_pass_rate", "value": 1},
        {"case_id": "a", "trial": 0, "metric": "rubric_pass_rate", "value": 1},
    ]
    with pytest.raises(CompareError, match="trial"):
        load_input(write_run(tmp_path / "m", _ca_metrics(0.9, None), mixed))


def test_unreadable_inputs_and_unit_mismatch_exit_2_and_still_write_compare_json(tmp_path):
    code, data, _ = cli_compare(tmp_path, tmp_path / "nope", ASSAY_NEW)
    assert code == 2 and data["exit_code"] == 2 and data["errors"]
    old = write_run(tmp_path / "o", [{"name": "x", "unit": "rate", "value": 0.5, "n": 10}])
    new = write_run(tmp_path / "n", [{"name": "x", "unit": "ratio", "value": 0.5}])
    c = compare_runs(old, new)
    assert c.exit_code == 2 and any("unit" in e for e in c.errors)


def test_outputs_are_byte_reproducible(tmp_path):
    a = compare_runs(ASSAY_OLD, ASSAY_NEW, gates=ASSAY_GATES)
    b = compare_runs(ASSAY_OLD, ASSAY_NEW, gates=ASSAY_GATES)
    assert report_json(a) == report_json(b)
    assert render_markdown(a) == render_markdown(b)
    old, new = _trial_data()
    m_old = [{"name": "s", "unit": "rate", "value": sum(map(sum, old.values())) / 90, "n": 90}]
    m_new = [{"name": "s", "unit": "rate", "value": sum(map(sum, new.values())) / 90, "n": 90}]
    c_old = [
        {"case_id": k, "trial": t, "metric": "s", "value": v}
        for k, vs in old.items()
        for t, v in enumerate(vs)
    ]
    c_new = [
        {"case_id": k, "trial": t, "metric": "s", "value": v}
        for k, vs in new.items()
        for t, v in enumerate(vs)
    ]
    po = write_run(tmp_path / "o", m_old, c_old)
    pn = write_run(tmp_path / "n", m_new, c_new)
    first = cli_compare(tmp_path / "x", po, pn)
    second = cli_compare(tmp_path / "y", po, pn)
    assert (tmp_path / "x" / "compare.json").read_bytes() == (
        tmp_path / "y" / "compare.json"
    ).read_bytes()
    assert first[2] == second[2]
    assert one_from(first[1], "s")["method"] == "paired two-level bootstrap (cases, then trials)"
    other = compare_runs(po, pn, seed=8)
    assert report_json(other) != report_json(compare_runs(po, pn))


def one_from(data: dict[str, Any], metric: str) -> dict[str, Any]:
    found = [ch for ch in data["changes"] if ch["metric"] == metric]
    assert len(found) == 1
    return found[0]


def test_compare_module_imports_no_network_or_provider_code():
    import ast

    tree = ast.parse((ROOT / "src" / "evalkit" / "compare.py").read_text(encoding="utf-8"))
    names = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    names |= {
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    }
    assert not names & {"socket", "urllib", "http", "anthropic", "openai", "requests", "httpx"}


def test_long_label_values_are_never_echoed(tmp_path):
    long = "x" * 200
    m = [{"name": "r", "unit": "rate", "value": 0.5, "n": 10, "labels": {"note": long}}]
    c = compare_runs(write_run(tmp_path / "o", m), write_run(tmp_path / "n", m))
    blob = report_json(c) + render_markdown(c)
    assert long not in blob and "<200 characters, not shown>" in blob


def test_public_api_and_cli_contract():
    import evalkit.compare as mod

    assert "compare" in cli.build_parser().format_help()
    for name in mod.__all__:
        assert hasattr(mod, name)
    assert (mod.EXIT_PASS, mod.EXIT_REGRESSION, mod.EXIT_INPUT) == (0, 1, 2)
    assert callable(compare)
    help_text = cli.COMPARE_EXIT_CODES
    for code in ("0", "1", "2"):
        assert f"  {code}  " in help_text
    g = parse_gates("gates:\n  - {metric: a, op: '>=', value: 1}\n")
    c = compare(load_input(ASSAY_OLD), load_input(ASSAY_NEW), gates=g, resamples=1000)
    assert c.exit_code == EXIT_INPUT  # the gate names a metric neither run has


def test_docs_state_the_exit_codes_sidecar_and_methods():
    text = (ROOT / "docs" / "compare.md").read_text(encoding="utf-8")
    for code in ("| 0 |", "| 1 |", "| 2 |"):
        assert code in text
    for key in ("case_id", "trial", "metric", "labels", "value", "settings.json"):
        assert f"`{key}`" in text
    for word in ("MOVER", "Holm", "unpaired", "at least 2 shared case ids", "never paired"):
        assert word in text
    assert "compare.md" in (ROOT / "docs" / "versioning.md").read_text(encoding="utf-8")


# ---- verifier findings (B5.6 pass 2): claims need enough data, and agree with the summary ------


GATE_ACC = "gates:\n  - {metric: acc, op: '>=', value: 0.5, severity: fail}\n"


def _acc_run(folder: Path, value: float, cases: list[tuple[str, float]]) -> Path:
    m = [{"name": "acc", "unit": "rate", "value": value, "n": len(cases)}]
    return write_run(folder, m, [{"case_id": i, "metric": "acc", "value": v} for i, v in cases])


def test_one_case_per_side_is_never_flagged(tmp_path):
    """Probe A: one case each side, unpaired: the bootstrap of one value has zero width."""
    old = _acc_run(tmp_path / "o", 0.0, [("x", 0.0)])
    new = _acc_run(tmp_path / "n", 1.0, [("y", 1.0)])
    row = one(compare_runs(old, new), "acc")
    assert row["flagged"] is False
    assert row["ci_low"] < row["ci_high"]  # never a zero-width interval passed off as certain


def test_two_shared_cases_are_too_few_to_claim_a_regression(tmp_path):
    """Probe B: 2 shared cases that both drop: RVR-03 is met (>= 2), but no claim is made."""
    gates = tmp_path / "g.yaml"
    gates.write_text(GATE_ACC, "utf-8")
    old = _acc_run(tmp_path / "o", 1.0, [("c0", 1.0), ("c1", 1.0)])
    new = _acc_run(tmp_path / "n", 0.0, [("c0", 0.0), ("c1", 0.0)])
    code, data, text = cli_compare(tmp_path, old, new, "--gates", str(gates))
    assert code == EXIT_PASS and data["errors"] == []
    row = one_from(data, "acc")
    assert row["pairing"] == "paired" and row["n_shared"] == 2
    assert row["interval_status"] == "too_few_cases"
    assert (row["ci_low"], row["ci_high"]) == (-1.0, -1.0)  # shown, not claimed
    assert row["flagged"] is False and row["regression"] is False and row["p_holm"] is None
    assert data["holm"]["family"] == 0
    assert "too few cases" in text


def test_every_shared_case_moving_alike_is_a_claim_not_degenerate(tmp_path):
    """Lead's review: with enough shared cases a zero-width paired interval is the strongest
    evidence (every case went from right to wrong) and must be flagged, not hidden."""
    ids = [f"c{i}" for i in range(12)]
    gates = tmp_path / "g.yaml"
    gates.write_text(GATE_ACC, "utf-8")
    old = _acc_run(tmp_path / "o", 1.0, [(i, 1.0) for i in ids])
    new = _acc_run(tmp_path / "n", 0.0, [(i, 0.0) for i in ids])
    c = compare_runs(old, new, gates=gates)
    row = one(c, "acc")
    assert row["interval_status"] == "ok" and row["ci_low"] == row["ci_high"] == -1.0
    assert row["flagged"] is True and c.exit_code == EXIT_REGRESSION
    # and with fewer than MIN_CASES shared cases the same move is never a claim
    few = [f"f{i}" for i in range(3)]
    old3 = _acc_run(tmp_path / "o3", 1.0, [(i, 1.0) for i in few])
    new3 = _acc_run(tmp_path / "n3", 0.0, [(i, 0.0) for i in few])
    c3 = compare_runs(old3, new3, gates=gates)
    assert one(c3, "acc")["flagged"] is False and c3.exit_code == EXIT_PASS


def test_partial_overlap_is_compared_unpaired_and_states_the_unpartnered_cases(tmp_path):
    """Probe C: 2 of 100 cases shared: no paired claim from 2 cases, never exit 1."""
    gates = tmp_path / "g.yaml"
    gates.write_text(GATE_ACC, "utf-8")
    shared_old = [("s1", 1.0), ("s2", 1.0)]
    shared_new = [("s1", 0.0), ("s2", 0.0)]
    old = _acc_run(tmp_path / "o", 1.0, [(f"o{i}", 1.0) for i in range(98)] + shared_old)
    new = _acc_run(tmp_path / "n", 0.98, [(f"n{i}", 1.0) for i in range(98)] + shared_new)
    code, data, text = cli_compare(tmp_path, old, new, "--gates", str(gates))
    assert code == EXIT_PASS and data["errors"] == []
    row = one_from(data, "acc")
    assert row["pairing"] == "unpaired" and row["partial_overlap"] is True
    assert row["n_shared"] == 2 and row["not_paired"] == {"old": 98, "new": 98}
    assert row["flagged"] is False and row["regression"] is False
    assert row["ci_low"] < row["diff"] < row["ci_high"]
    line = next(ln for ln in text.splitlines() if ln.startswith("| acc"))
    assert "partial overlap" in line and "98 old" in line and "98 new" in line


def test_paired_needs_the_shared_cases_to_be_most_of_each_side(tmp_path):
    # 40 shared of 100 on the new side: below half, so unpaired; 40 of 60: paired
    base = [(f"c{i}", float(i % 2)) for i in range(40)]
    old = _acc_run(tmp_path / "o", 0.5, base)
    extra = [(f"x{i}", float(i % 2)) for i in range(60)]
    row = one(compare_runs(old, _acc_run(tmp_path / "n", 0.5, base + extra)), "acc")
    assert row["pairing"] == "unpaired" and row["partial_overlap"] is True
    row2 = one(compare_runs(old, _acc_run(tmp_path / "n2", 0.5, base + extra[:20])), "acc")
    assert row2["pairing"] == "paired" and row2["partial_overlap"] is False
    assert row2["not_paired"] == {"old": 0, "new": 20}


def test_count_metrics_refuse_a_per_case_sidecar(tmp_path):
    m = [{"name": "atk", "unit": "count", "value": 1, "n": 40}]
    cases = [{"case_id": f"c{i}", "metric": "atk", "value": int(i == 0)} for i in range(40)]
    with pytest.raises(CompareError, match="count"):
        load_input(write_run(tmp_path / "n", m, cases))
    c = compare_runs(write_run(tmp_path / "o", m), tmp_path / "n" / "metrics.json")
    assert c.exit_code == EXIT_INPUT
    assert any("count" in e and "cases.jsonl" in e for e in c.errors)
    # a count without a sidecar: the change on the count scale (new count - old count)
    old = write_run(tmp_path / "o2", [{"name": "atk", "unit": "count", "value": 0, "n": 40}])
    new = write_run(tmp_path / "n2", [{"name": "atk", "unit": "count", "value": 1, "n": 40}])
    assert one(compare_runs(old, new), "atk")["diff"] == 1


def test_a_sidecar_that_contradicts_its_metric_value_is_refused(tmp_path):
    """Probe D: value 0.9 but every per-case value 0."""
    old = write_run(
        tmp_path / "o",
        [{"name": "acc", "unit": "rate", "value": 0.9, "n": 4}],
        [{"case_id": f"c{i}", "metric": "acc", "value": 0} for i in range(4)],
    )
    with pytest.raises(CompareError, match="do not average"):
        load_input(old)
    assert compare_runs(old, old).exit_code == EXIT_INPUT
    # within the metrics-1 rate tolerance (a value rounded to 6 decimals) is accepted
    ok = write_run(
        tmp_path / "ok",
        [{"name": "acc", "unit": "rate", "value": 0.333333, "n": 3}],
        [{"case_id": f"c{i}", "metric": "acc", "value": int(i == 0)} for i in range(3)],
    )
    assert load_input(ok).cases is not None


def test_model_ids_are_listed_per_model_within_the_shown_limit(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    shutil.copytree(EXAMPLE_RUN, old)
    shutil.copytree(EXAMPLE_RUN, new)
    out = []
    for ln in (new / "records.jsonl").read_text("utf-8").splitlines():
        r = json.loads(ln)
        if r["completion"]["model"] == S:
            r["completion"]["model_snapshot"] = "claude-sonnet-5-5-snap2"
        out.append(json.dumps(r))
    (new / "records.jsonl").write_text("\n".join(out) + "\n", "utf-8")
    c = compare_runs(old, new)
    diffs = {d.setting: d for d in c.settings}
    d = diffs[f"model_ids.{S}"]
    assert d.new == "claude-sonnet-5-5-snap2" and d.old and "not shown" not in d.old
    assert not any(k.startswith("model_ids.gpt") for k in diffs)  # unchanged: not listed
    assert "not shown" not in render_markdown(c)


def test_typed_labels_are_told_apart_in_json_and_markdown(tmp_path):
    old = write_run(
        tmp_path / "o",
        [{"name": "acc", "unit": "rate", "value": 0.5, "n": 10, "labels": {"cohort": 2024}}],
    )
    new = write_run(
        tmp_path / "n",
        [{"name": "acc", "unit": "rate", "value": 0.5, "n": 10, "labels": {"cohort": "2024"}}],
    )
    c = compare_runs(old, new)
    shown = {m["side"]: m["labels"]["cohort"] for m in c.as_dict()["missing"]}
    assert shown == {"new": "2024", "old": '"2024"'}  # missing from old: the text "2024"
    md = render_markdown(c)
    assert "cohort=2024" in md and 'cohort="2024"' in md


def test_a_heldout_group_of_one_never_shows_category_figures(tmp_path):
    held = {"split": "heldout"}

    def acc(value: float, **labels: Any) -> dict[str, Any]:
        return {"name": "acc", "unit": "rate", "value": value, "n": 50, "labels": labels}

    old = write_run(tmp_path / "o", [acc(0.2, **held, cat="A"), acc(0.9, **held, cat="B")])
    new = write_run(tmp_path / "n", [acc(0.6, **held, cat="A")])
    c = compare_runs(old, new)
    data = c.as_dict()
    (row,) = data["changes"]
    assert row["labels"] == held and row["heldout_rows"] == 1
    assert (row["old"], row["new"], row["diff"], row["ci_low"]) == (None, None, None, None)
    (miss,) = data["missing"]
    assert miss["labels"] == held and miss["heldout_rows"] == 1
    md = render_markdown(c)
    for figure in ("0.2", "0.6", "0.9"):
        assert figure not in md
    # a split-level row (no other label) is already an aggregate: it keeps its figures
    a = write_run(tmp_path / "a", [acc(0.2, **held)])
    b = write_run(tmp_path / "b", [acc(0.6, **held)])
    assert one(compare_runs(a, b), "acc")["new"] == 0.6


def test_an_unwritable_markdown_report_records_exit_2_in_compare_json(tmp_path):
    out = tmp_path / "compare.json"
    blocker = tmp_path / "file"
    blocker.write_text("x", "utf-8")
    code = cli.main(
        ["compare", str(RO_OLD), str(RO_NEW), "--out", str(out), "--md", str(blocker / "x.md")]
    )
    data = json.loads(out.read_text("utf-8"))
    assert code == EXIT_INPUT and data["exit_code"] == EXIT_INPUT and data["verdict"] == "error"
    assert data["errors"]


def test_a_run_folder_without_an_evalkit_version_lists_it_as_unrecorded():
    c = compare_runs(EXAMPLE_RUN, EXAMPLE_RUN)
    diffs = {d.setting: d.as_dict() for d in c.settings}
    assert diffs["evalkit"]["old"] is None and diffs["evalkit"]["new"] is None
    assert "unrecorded" in diffs["evalkit"]["note"]
    assert "`evalkit`: unrecorded → unrecorded" in render_markdown(c)
