"""B5.3 gates on any metric (GAT-01..10, EFF-01, EFF-02), with the asking projects' cases
(D-61 rule 3): assay-pipeline's attack counts and exact bounds, respectful-offers' exported
summary, course-assistant's exit precedence, input classes and gate.json on every path.
No network, no model calls, no spend."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from evalkit import cli
from evalkit.gates import (
    EXIT_COST_CAP,
    EXIT_FAIL,
    EXIT_INPUT,
    EXIT_PASS,
    EXIT_PENDING,
    INPUTS,
    GatesFile,
    RunStatus,
    disparity_gap,
    evaluate,
    parse_gates,
    render_text,
    run_gate,
)
from evalkit.metrics import (
    EFFICIENCY_NAMES,
    Metric,
    MetricsError,
    MetricsSummary,
    efficiency_metrics,
    from_run_summary,
    parse_metrics,
)
from evalkit.stats import clopper_pearson, rate_wording

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "gates"
# respectful-offers' own metrics-1 export and gates at its commit aea2bcc
# (results/kkbox/metrics.json, configs/gates.yaml; aggregates only, simulated).
RO_METRICS = FIX / "respectful_offers" / "metrics.json"
RO_GATES = FIX / "respectful_offers" / "gates.yaml"
# Vendored from assay-pipeline's working tree on 2026-11-17 (uncommitted there; refresh both
# once Assay commits them): its real metrics-1 export and its release gates.
ASSAY_METRICS = FIX / "assay" / "metrics.json"
ASSAY_GATES = FIX / "assay" / "gates.yaml"
GOLDEN_SUMMARY = ROOT / "tests" / "fixtures" / "golden" / "summary_pubmedqa_mini.json"


def doc(metrics: list[dict[str, Any]], **top: Any) -> dict[str, Any]:
    return {
        "schema_version": "metrics-1",
        "project": "p",
        "run_id": "r1",
        "created_at": "2026-11-17T00:00:00Z",
        **top,
        "metrics": metrics,
    }


def write(tmp: Path, name: str, data: Any) -> Path:
    p = tmp / name
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


def gate_cli(tmp: Path, summary: Path, gates: str | Path | None, *extra: str) -> tuple[int, dict]:
    argv = ["gate", str(summary), "--out", str(tmp / "out" / "gate.json"), *extra]
    if gates is not None:
        cfg = gates if isinstance(gates, Path) else write(tmp, "gates.yaml", gates)
        argv += ["--config", str(cfg)]
    code = cli.main(argv)
    return code, json.loads((tmp / "out" / "gate.json").read_text(encoding="utf-8"))


def check(metrics: list[dict[str, Any]], gates: str, status: RunStatus | None = None, **top):
    return evaluate(parse_metrics(json.dumps(doc(metrics, **top))), parse_gates(gates), status)


ATTACKS_0 = [
    {
        "name": "attack_successes",
        "unit": "count",
        "value": 0,
        "n": 150,
        "labels": {"model": "gpt-6.1-sol", "split": "public"},
    },
    {
        "name": "attack_trial_success_rate",
        "unit": "rate",
        "value": 0.0,
        "n": 150,
        "k": 0,
        "labels": {"model": "gpt-6.1-sol"},
    },
]
ASSAY_GATES_MIN = """
gates:
  - metric: attack_successes
    labels: {model: "*"}
    op: "<="
    value: 0
  - metric: attack_trial_success_rate
    bound: upper
    op: "<="
    value: 0.03
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """GAT-02: gating never opens a connection; any attempt fails the test."""
    import socket

    def refuse(*a, **k):
        raise AssertionError("evalkit gate must not touch the network")

    real_connect = socket.socket.connect
    loopback = {"127.0.0.1", "::1", "localhost"}

    def connect(sock, address, *a, **k):
        # asyncio's event loop builds its self-pipe from a loopback socketpair on Windows
        # (socketpair() is a connect to 127.0.0.1 there): that is not the network
        host = address[0] if isinstance(address, tuple) else None
        if host in loopback:
            return real_connect(sock, address, *a, **k)
        return refuse()

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(scope="module")
def golden_run():
    from tests.golden.test_golden_run import _run, reply
    from tests.pipeline.fakes import ScriptedBatchAdapter

    with tempfile.TemporaryDirectory() as d:
        summary, records, _judging, cases = asyncio.run(
            _run(Path(d) / "store", ScriptedBatchAdapter(reply))
        )
    return summary, records, cases


# ---- GAT-07 exact Clopper-Pearson ---------------------------------------------------------------


def _binom_cdf(k: int, n: int, p: float) -> float:
    return math.fsum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def test_clopper_pearson_assay_bounds_n150():
    """assay-pipeline, OWNER D-70: the gate's one-sided 95% upper bound at n=150 is 0.0198 for
    k=0 (passes 0.03) and about 0.0312 for k=1 (fails); the displayed interval is the
    standard two-sided one (k=0: 1 - 0.025**(1/150) = 0.0243; k=1: about 0.0366)."""
    zero, one = clopper_pearson(0, 150, sided="upper"), clopper_pearson(1, 150, sided="upper")
    assert zero.hi == pytest.approx(1 - 0.05 ** (1 / 150), abs=1e-15)
    assert round(zero.hi, 4) == 0.0198 and zero.lo == 0.0 and zero.point == 0.0
    assert round(one.hi, 4) == 0.0312 and one.hi > 0.03 > zero.hi and one.lo == 0.0
    two0, two1 = clopper_pearson(0, 150), clopper_pearson(1, 150)
    assert two0.hi == pytest.approx(1 - 0.025 ** (1 / 150), abs=1e-15) and two0.lo == 0.0
    assert 0.0365 <= two1.hi <= 0.0367


@pytest.mark.parametrize(("k", "n"), [(0, 150), (1, 150), (5, 20), (0, 3), (3, 3), (149, 150)])
def test_clopper_pearson_one_sided_bounds_match_binomial_tails(k, n):
    """D-70: `sided="upper"|"lower"` put the whole alpha in one tail for every k, checked by
    exact binomial sums; `sided="two"` puts alpha/2 in each tail, k = 0 and k = n included."""
    up, low = clopper_pearson(k, n, sided="upper"), clopper_pearson(k, n, sided="lower")
    if k < n:
        assert _binom_cdf(k, n, up.hi) == pytest.approx(0.05, abs=1e-9)
    else:
        assert up.hi == 1.0
    if k > 0:
        assert 1 - _binom_cdf(k - 1, n, low.lo) == pytest.approx(0.05, abs=1e-9)
    else:
        assert low.lo == 0.0
    assert up.lo == 0.0 and low.hi == 1.0
    two = clopper_pearson(k, n)
    if k < n:
        assert _binom_cdf(k, n, two.hi) == pytest.approx(0.025, abs=1e-9)
    if k > 0:
        assert 1 - _binom_cdf(k - 1, n, two.lo) == pytest.approx(0.025, abs=1e-9)
    with pytest.raises(ValueError):
        clopper_pearson(k, n, sided="both")  # type: ignore[arg-type]


@pytest.mark.parametrize(("k", "n"), [(1, 150), (5, 20), (3, 7), (149, 150), (50, 100)])
def test_clopper_pearson_matches_independent_binomial_tails(k, n):
    """Independent check by exact binomial sums (math.comb): the upper bound leaves alpha/2
    below k and the lower bound alpha/2 at or above k."""
    iv = clopper_pearson(k, n)
    assert _binom_cdf(k, n, iv.hi) == pytest.approx(0.025, abs=1e-9)
    assert 1 - _binom_cdf(k - 1, n, iv.lo) == pytest.approx(0.025, abs=1e-9)
    assert iv.lo <= k / n <= iv.hi


def test_clopper_pearson_k_equals_n_and_reruns_identically():
    assert clopper_pearson(150, 150).lo == pytest.approx(0.025 ** (1 / 150))  # two-sided
    assert clopper_pearson(150, 150, sided="lower").lo == pytest.approx(0.05 ** (1 / 150))
    assert clopper_pearson(7, 33) == clopper_pearson(7, 33)


@pytest.mark.parametrize(("k", "n"), [(0, 0), (3, 2), (-1, 5), (True, 5), (1.0, 5)])
def test_clopper_pearson_refuses_bad_counts(k, n):
    with pytest.raises((ValueError, TypeError)):
        clopper_pearson(k, n)


def test_rate_wording_never_says_zero_percent():
    assert rate_wording(0, 150) == "0 of 150, below 1.98% at 95%"
    assert "0%" not in rate_wording(0, 150).replace("1.98%", "")
    assert rate_wording(150, 150).startswith("150 of 150, above")
    assert rate_wording(1, 150).startswith("1 of 150 (0.667%, 95% CI")


# ---- GAT-03 the metrics-1 format ----------------------------------------------------------------


def test_spec_example_validates_and_keeps_number_types():
    spec = (ROOT / "specs" / "B5.3-gates" / "spec.md").read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)```", spec, re.S)
    assert example is not None
    ms = parse_metrics(example.group(1))
    assert ms.simulated is False and len(ms.metrics) == 5
    by = {m.name: m for m in ms.metrics}
    assert type(by["attack_successes"].value) is int
    assert type(by["attack_trial_success_rate"].value) is float
    assert by["revenue_per_member"].unit == "NTD" and by["audit_log_verified"].value is True


BAD_FILES: dict[str, Any] = {
    "unknown top-level key": doc([], extra=1),
    "missing project": {k: v for k, v in doc([]).items() if k != "project"},
    "missing run_id": {k: v for k, v in doc([]).items() if k != "run_id"},
    "missing created_at": {k: v for k, v in doc([]).items() if k != "created_at"},
    "wrong schema": doc([], schema_version="metrics-2"),
    "naive timestamp": doc([], created_at="2026-11-17T00:00:00"),
    "simulated not bool": doc([], simulated="yes"),
    "count as float": doc([{"name": "c", "unit": "count", "value": 1.0}]),
    "negative count": doc([{"name": "c", "unit": "count", "value": -1}]),
    "count as bool": doc([{"name": "c", "unit": "count", "value": True}]),
    "rate above 1": doc([{"name": "r", "unit": "rate", "value": 1.5}]),
    "bool as int": doc([{"name": "b", "unit": "bool", "value": 1}]),
    "lower-case currency": doc([{"name": "s", "unit": "usd", "value": 1.0}]),
    "k above n": doc([{"name": "r", "unit": "rate", "value": 0.1, "n": 3, "k": 4}]),
    "k without n": doc([{"name": "r", "unit": "rate", "value": 0.1, "k": 1}]),
    "k on a count": doc([{"name": "c", "unit": "count", "value": 1, "n": 3, "k": 1}]),
    "n as float": doc([{"name": "c", "unit": "count", "value": 1, "n": 3.0}]),
    "one bound": doc([{"name": "x", "unit": "ratio", "value": 1, "ci_low": 0}]),
    "interval without method": doc(
        [{"name": "x", "unit": "ratio", "value": 1, "ci_low": 0, "ci_high": 2}]
    ),
    "value outside interval": doc(
        [{"name": "x", "unit": "ratio", "value": 3, "ci_low": 0, "ci_high": 2, "ci_method": "b"}]
    ),
    "duplicate name+labels": doc(
        [
            {"name": "x", "unit": "ratio", "value": 1, "labels": {"a": "b"}},
            {"name": "x", "unit": "ratio", "value": 2, "labels": {"a": "b"}},
        ]
    ),
    "case id label": doc([{"name": "x", "unit": "ratio", "value": 1, "labels": {"case_id": "q1"}}]),
    "member label": doc([{"name": "x", "unit": "ratio", "value": 1, "labels": {"msno": "m"}}]),
    "bad name": doc([{"name": "Bad-Name", "unit": "ratio", "value": 1}]),
    "unknown metric key": doc([{"name": "x", "unit": "ratio", "value": 1, "ci_hi": 2}]),
    "nested label": doc([{"name": "x", "unit": "ratio", "value": 1, "labels": {"a": {"b": 1}}}]),
    "not an object": [1, 2],
}


@pytest.mark.parametrize("case", sorted(BAD_FILES))
def test_invalid_metrics_files_are_refused(case):
    with pytest.raises(MetricsError):
        parse_metrics(json.dumps(BAD_FILES[case]))


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version": "metrics-1", "schema_version": "metrics-1"}',
        json.dumps(doc([{"name": "x", "unit": "ratio", "value": 1}])).replace("1}", "NaN}"),
        json.dumps(doc([{"name": "x", "unit": "ratio", "value": 1}])).replace("1}", "1e999}"),
        "{not json",
    ],
)
def test_duplicate_keys_nan_infinity_and_bad_json_are_refused(text):
    with pytest.raises(MetricsError):
        parse_metrics(text)


def test_label_values_keep_their_type():
    ms = parse_metrics(
        json.dumps(
            doc(
                [
                    {"name": "x", "unit": "ratio", "value": 1, "labels": {"a": 1}},
                    {"name": "x", "unit": "ratio", "value": 2, "labels": {"a": "1"}},
                    {"name": "x", "unit": "ratio", "value": 3, "labels": {"a": True}},
                ]
            )
        )
    )
    assert len(ms.metrics) == 3  # 1, "1" and true are three label values


def test_gate_reads_evalkit_summary_json_alike(tmp_path):
    summary = write(tmp_path, "summary.json", GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    report = run_gate(
        summary,
        write(
            tmp_path,
            "g.yaml",
            """
gates:
  - metric: accuracy
    labels: {model: "*"}
    bound: lower
    op: ">="
    value: 0.0
  - metric: headline
    labels: {model: claude-sonnet-5-5, judge: "*"}
    op: ">="
    value: 0.0
""",
        ),
    )
    assert report.exit_code == EXIT_PASS and report.source["kind"] == "evalkit-summary"
    assert {r.labels["model"] for r in report.rows} == {
        "claude-sonnet-5-5",
        "gpt-6.1-sol",
        "gpt-6-luna",
    }
    assert {r.labels.get("judge") for r in report.rows if r.metric == "headline"} == {
        "claude-opus-5-5",
        "gpt-6.1-sol",
    }


# ---- GAT-01 matching, ops, bounds; v0.2 thresholds unchanged ------------------------------------


def test_star_labels_omitted_labels_and_ops():
    metrics = [
        {"name": "m", "unit": "ratio", "value": 2, "labels": {"model": "a", "split": "x"}},
        {"name": "m", "unit": "ratio", "value": 5, "labels": {"model": "b"}},
    ]
    star = check(metrics, 'gates: [{metric: m, labels: {split: "*"}, op: "<=", value: 2}]')
    assert [r.labels for r in star.rows] == [{"model": "a", "split": "x"}]
    assert star.exit_code == EXIT_PASS
    every = check(metrics, 'gates: [{metric: m, op: ">=", value: 3}]')
    assert [r.result for r in every.rows] == ["fail", "pass"] and every.exit_code == EXIT_FAIL
    eq = check(metrics, 'gates: [{metric: m, labels: {model: b}, op: "==", value: 5}]')
    assert eq.exit_code == EXIT_PASS


def test_lower_and_upper_bounds_use_the_supplied_interval():
    m = [
        {
            "name": "x",
            "unit": "ratio",
            "value": 1.0,
            "ci_low": 0.5,
            "ci_high": 1.5,
            "ci_method": "boot",
        }
    ]
    lo = check(m, 'gates: [{metric: x, bound: lower, op: ">=", value: 0.6}]')
    hi = check(m, 'gates: [{metric: x, bound: upper, op: "<=", value: 1.5}]')
    assert lo.rows[0].observed == 0.5 and lo.exit_code == EXIT_FAIL
    assert hi.rows[0].observed == 1.5 and hi.exit_code == EXIT_PASS


@pytest.mark.parametrize(
    ("gates", "why"),
    [
        ('gates: [{metric: nothere, op: "<=", value: 0}]', "no metric named"),
        ('gates: [{metric: x, labels: {model: zzz}, op: "<=", value: 0}]', "matches no metric"),
        ('gates: [{metric: x, bound: upper, op: "<=", value: 9}]', "without an interval"),
        ('gates: [{metric: x, op: "==", value: true}]', "true/false"),
    ],
)
def test_gate_matching_nothing_or_bound_without_interval_exits_2(gates, why):
    report = check([{"name": "x", "unit": "ratio", "value": 1, "labels": {"model": "a"}}], gates)
    assert report.exit_code == EXIT_INPUT and any(why in e for e in report.errors)


def test_v02_thresholds_keep_meaning_and_bytes(golden_run, tmp_path):
    from evalkit import run as R
    from evalkit.schemas import RunSummary, Threshold

    t = Threshold(metric="headline", op=">=", value=0.6)
    assert t.model_dump_json() == '{"metric":"headline","op":">=","value":0.6}'
    assert set(Threshold.model_fields) == {"metric", "op", "value"}
    summary = RunSummary.model_validate_json(GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    path = write(tmp_path, "summary.json", GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    report = run_gate(path)  # no --config: the run's own v0.2 thresholds
    assert report.exit_code == R.exit_code_for(summary)
    assert {r.metric for r in report.rows} == {"passes_threshold"}


# ---- GAT-02 the command -------------------------------------------------------------------------


def test_cli_prints_every_gate_and_the_verdict_without_network(tmp_path, capsys):
    path = write(tmp_path, "m.json", doc(ATTACKS_0))
    code, gate_json = gate_cli(tmp_path, path, ASSAY_GATES_MIN)
    out = capsys.readouterr().out
    assert code == EXIT_PASS and gate_json["exit_code"] == 0 and gate_json["verdict"] == "pass"
    assert "attack_successes" in out and "attack_trial_success_rate" in out
    assert "verdict: PASS (exit 0)" in out and "0 of 150, below 1.98% at 95%" in out
    assert len(gate_json["gates"]) == 2


def test_json_flag_prints_the_gate_json_object(tmp_path, capsys):
    path = write(tmp_path, "m.json", doc(ATTACKS_0))
    code, gate_json = gate_cli(tmp_path, path, ASSAY_GATES_MIN, "--json")
    assert code == 0 and json.loads(capsys.readouterr().out) == gate_json


def test_the_github_action_example_runs_evalkit_gate():
    text = (ROOT / "examples" / "github-action" / "evalkit.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    runs = [s.get("run", "") for s in wf["jobs"]["evaluate"]["steps"]]
    assert any("evalkit gate" in r and "--out gate.json" in r for r in runs)


def test_gate_module_imports_no_network_or_provider_code():
    import ast

    for mod in ("gates.py", "metrics.py"):
        tree = ast.parse((ROOT / "src" / "evalkit" / mod).read_text(encoding="utf-8"))
        names = {
            a.name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
            for a in n.names
        }
        names |= {
            (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        }
        assert not names & {"socket", "urllib", "http", "anthropic", "openai", "requests"}


# ---- GAT-04 held-out, case ids, long labels; assay's one-success case ---------------------------


def test_assay_one_success_fails_both_gates_with_exit_1(tmp_path):
    one = copy.deepcopy(ATTACKS_0)
    one[0]["value"], one[1]["value"], one[1]["k"] = 1, 1 / 150, 1
    code, gate_json = gate_cli(tmp_path, write(tmp_path, "m.json", doc(one)), ASSAY_GATES_MIN)
    assert code == EXIT_FAIL
    assert [g["result"] for g in gate_json["gates"]] == ["fail", "fail"]
    row = gate_json["gates"][1]
    assert round(row["observed"], 4) == 0.0312  # one-sided 95% upper bound (D-70)
    assert row["ci_method"] == "clopper-pearson" and 0.0365 <= row["ci_high"] <= 0.0367
    assert "one-sided 95% upper bound 0.0312" in row["note"]


def test_assay_real_export_passes_its_gates(tmp_path):
    code, gate_json = gate_cli(tmp_path, ASSAY_METRICS, ASSAY_GATES)
    assert code == EXIT_PASS and gate_json["failed"] == 0 and gate_json["simulated"] is False
    names = {m.name for m in parse_metrics(ASSAY_METRICS.read_text(encoding="utf-8")).metrics}
    assert {"system_cost", "eval_overhead_cost", "ablation_cost", "spend_total"} <= names
    gated = {g["metric"] for g in gate_json["gates"]}
    assert {"attack_trials_unscored", "behaviour_rate"} <= gated
    rules = {g["labels"].get("rule") for g in gate_json["gates"] if g["severity"] == "fail"}
    assert {"B6", "B7", "B8", "B9", "B10"} <= rules


def test_assay_real_export_with_one_attack_success_exits_1(tmp_path):
    data = json.loads(ASSAY_METRICS.read_text(encoding="utf-8"))
    for m in data["metrics"]:
        if (
            m.get("labels") == {"model": "gpt-6.1-sol", "split": "public"}
            and m["name"] == "attack_successes"
        ):
            m["value"] = 1
        if m["name"] == "attack_trial_success_rate" and m["labels"]["model"] == "gpt-6.1-sol":
            m["k"], m["value"] = 1, 1 / 150
    code, gate_json = gate_cli(tmp_path, write(tmp_path, "m.json", data), ASSAY_GATES)
    assert code == EXIT_FAIL
    failed = {
        (g["metric"], g["labels"].get("model")) for g in gate_json["gates"] if g["result"] == "fail"
    }
    assert failed == {
        ("attack_successes", "gpt-6.1-sol"),
        ("attack_trial_success_rate", "gpt-6.1-sol"),
    }


def test_assay_gate_naming_an_absent_agent_exits_2(tmp_path):
    extra = ASSAY_GATES.read_text(encoding="utf-8").replace(
        "heldout_splits:",
        "  - metric: attack_trial_success_rate\n    labels: {model: gpt-7-absent}\n"
        '    bound: upper\n    op: "<="\n    value: 0.03\nheldout_splits:',
    )
    code, gate_json = gate_cli(tmp_path, ASSAY_METRICS, extra)
    assert code == EXIT_INPUT and any("matches no metric" in e for e in gate_json["errors"])


def test_heldout_rows_are_split_level_aggregates_but_every_row_is_checked():
    metrics = [
        {
            "name": "rate",
            "unit": "rate",
            "value": v,
            "n": 10,
            "k": round(v * 10),
            "labels": {"model": "a", "split": "heldout", "category": f"cat-{i}"},
        }
        for i, v in enumerate((1.0, 0.9, 0.3))
    ] + [
        {"name": "rate", "unit": "rate", "value": 1.0, "labels": {"model": "a", "split": "public"}}
    ]
    report = check(
        metrics, 'gates: [{metric: rate, op: ">=", value: 0.5}]\nheldout_splits: [heldout]'
    )
    assert report.exit_code == EXIT_FAIL  # the 0.3 category fails, though it is folded
    held = [r for r in report.rows if r.labels.get("split") == "heldout"]
    assert len(held) == 1 and held[0].heldout_rows == 3 and held[0].observed == 0.3
    assert held[0].labels == {"model": "a", "split": "heldout"} and held[0].k is None
    shown = json.dumps(report.as_dict()) + render_text(report)
    assert "cat-" not in shown and "category" not in shown


def test_long_label_values_are_never_echoed(tmp_path, capsys):
    long = "free text " * 10  # 100 characters
    data = doc([{"name": "x", "unit": "ratio", "value": 1, "labels": {"note": long}}])
    code, gate_json = gate_cli(
        tmp_path, write(tmp_path, "m.json", data), 'gates: [{metric: x, op: "<=", value: 0}]'
    )
    out = capsys.readouterr().out
    assert code == EXIT_FAIL and long not in out and long not in json.dumps(gate_json)
    assert "<100 characters, not shown>" in out
    bad = doc([{"name": "x", "unit": "count", "value": 1.5, "labels": {"note": long}}])
    code, gate_json = gate_cli(tmp_path, write(tmp_path, "b.json", bad), None)
    assert code == EXIT_INPUT and long not in capsys.readouterr().out + json.dumps(gate_json)


def test_case_ids_cannot_be_present_or_printed(tmp_path, capsys):
    data = doc([{"name": "x", "unit": "ratio", "value": 1, "labels": {"case_id": "pqa-123"}}])
    code, gate_json = gate_cli(
        tmp_path, write(tmp_path, "m.json", data), 'gates: [{metric: x, op: "<=", value: 2}]'
    )
    assert code == EXIT_INPUT and "pqa-123" not in capsys.readouterr().out + json.dumps(gate_json)


# ---- GAT-05 respectful-offers: labels, NTD, supplied intervals, simulated -----------------------


def test_respectful_offers_export_passes_its_gates_with_simulated_shown(tmp_path, capsys):
    code, gate_json = gate_cli(tmp_path, RO_METRICS, RO_GATES)
    out = capsys.readouterr().out
    assert code == EXIT_PASS and gate_json["simulated"] is True
    assert out.splitlines()[0].startswith("SIMULATED: true")
    assert len(parse_metrics(RO_METRICS.read_text(encoding="utf-8")).metrics) == 633
    viol = [g for g in gate_json["gates"] if g["metric"] == "violations"]
    # 4 respectful policies x 3 variants x 5 audited rules, every one 0
    assert len(viol) == 60 and all(g["observed"] == 0 for g in viol)
    assert {g["labels"]["respectful"] for g in viol} == {"true"}
    assert {g["labels"]["rule"] for g in viol} >= {"R1", "R2"}
    warned = [g for g in gate_json["gates"] if g["result"] == "warn"]
    assert gate_json["warned"] == 2 and {g["metric"] for g in warned} == {"group.offer_rate"}
    assert gate_json["failed"] == 0


def test_respectful_offers_supplied_intervals_and_ntd_are_carried_exactly(tmp_path):
    data = json.loads(RO_METRICS.read_text(encoding="utf-8"))
    report = run_gate(
        RO_METRICS,
        write(
            tmp_path,
            "g.yaml",
            """
gates:
  - metric: ltv_proxy_per_member
    labels: {policy: bandit, variant: central}
    bound: lower
    op: ">="
    value: 300
""",
        ),
    )
    ltv = report.rows[0]
    src = next(
        m
        for m in data["metrics"]
        if m["name"] == "ltv_proxy_per_member"
        and m["labels"] == {"policy": "bandit", "variant": "central"}
    )
    assert ltv.unit == "NTD" and ltv.observed == src["ci_low"] and ltv.ci_high == src["ci_high"]
    assert ltv.ci_method == src["ci_method"] == "block-bootstrap percentile 95% (D-16)"
    holdout = run_gate(RO_METRICS, RO_GATES)
    lows = [r for r in holdout.rows if r.metric == "holdout_check" and r.bound == "lower"]
    assert {r.observed for r in lows} == {-1.844796}  # the file's ci_low, never recomputed
    assert all(
        m["name"] != "member_id" and "msno" not in m.get("labels", {}) for m in data["metrics"]
    )


def test_respectful_offers_status_quo_violations_fail_when_gated(tmp_path):
    code, gate_json = gate_cli(
        tmp_path,
        RO_METRICS,
        """
gates:
  - metric: violations
    labels: {policy: "*", rule: R1}
    op: "=="
    value: 0
""",
    )
    failing = {g["labels"]["policy"] for g in gate_json["gates"] if g["result"] == "fail"}
    assert code == EXIT_FAIL and failing == {"status_quo"}


# ---- GAT-06 disparity ---------------------------------------------------------------------------


def test_respectful_offers_age_band_disparity_gap():
    report = run_gate(RO_METRICS, RO_GATES)
    rows = [r for r in report.rows if r.metric == "group.offer_rate"]
    bandit = next(r for r in rows if r.labels["policy"] == "bandit")
    assert bandit.observed == pytest.approx(0.4671 - 0.108252, abs=1e-12)
    assert bandit.result == "warn" and bandit.labels["age_band"] == "max - min"
    assert bandit.ci_low is None  # its groups carry no n: the point gap only
    assert "highest <18, lowest unknown" in bandit.note


def test_respectful_offers_disparity_upper_bound_without_n_exits_2(tmp_path):
    """Its group rows carry no n, so the gap has no interval: `bound: upper` is bad input."""
    code, gate_json = gate_cli(
        tmp_path,
        RO_METRICS,
        """
gates:
  - metric: group.offer_rate
    labels: {policy: bandit}
    disparity: age_band
    bound: upper
    op: "<="
    value: 0.10
    severity: warn
""",
    )
    assert code == EXIT_INPUT and any("without group counts" in e for e in gate_json["errors"])


GROUPS = [
    {
        "name": "offer_rate",
        "unit": "rate",
        "value": k / 400,
        "n": 400,
        "k": k,
        "labels": {"policy": "p", "band": b},
    }
    for b, k in (("young", 60), ("mid", 48), ("old", 40))
]


def test_disparity_interval_is_seeded_and_gates_its_upper_bound():
    gap1, iv1 = disparity_gap({"a": (0.15, 60, 400), "b": (0.1, 40, 400)}, seed="s")
    gap2, iv2 = disparity_gap({"a": (0.15, 60, 400), "b": (0.1, 40, 400)}, seed="s")
    assert (gap1, iv1) == (gap2, iv2) and iv1 is not None
    assert iv1[0] <= gap1 <= iv1[1] and iv1[1] > 0.08
    up = check(
        GROUPS,
        'gates: [{metric: offer_rate, disparity: band, bound: upper, op: "<=", value: 0.08}]',
    )
    point = check(GROUPS, 'gates: [{metric: offer_rate, disparity: band, op: "<=", value: 0.08}]')
    assert point.exit_code == EXIT_PASS and point.rows[0].observed == pytest.approx(0.05)
    hi = up.rows[0].ci_high
    assert up.exit_code == EXIT_FAIL and hi is not None and up.rows[0].observed == hi > 0.08
    assert up.rows[0].ci_method == "bootstrap of Clopper-Pearson draws (seeded)"
    again = check(
        GROUPS,
        'gates: [{metric: offer_rate, disparity: band, bound: upper, op: "<=", value: 0.08}]',
    )
    assert again.rows[0].ci_high == up.rows[0].ci_high


def test_disparity_bound_without_counts_or_one_group_exits_2():
    no_n = [{k: v for k, v in g.items() if k not in ("n", "k")} for g in GROUPS]
    r = check(
        no_n, 'gates: [{metric: offer_rate, disparity: band, bound: upper, op: "<=", value: 1}]'
    )
    assert r.exit_code == EXIT_INPUT
    one = check(GROUPS[:1], 'gates: [{metric: offer_rate, disparity: band, op: "<=", value: 1}]')
    assert one.exit_code == EXIT_INPUT and "two groups" in one.errors[0]


# ---- GAT-08 severity ----------------------------------------------------------------------------


def test_warn_only_failures_exit_0_and_are_listed(tmp_path, capsys):
    one = copy.deepcopy(ATTACKS_0)
    one[1]["value"], one[1]["k"] = 1 / 150, 1
    code, gate_json = gate_cli(
        tmp_path,
        write(tmp_path, "m.json", doc(one)),
        """
gates:
  - metric: attack_trial_success_rate
    bound: upper
    op: "<="
    value: 0.03
    severity: warn
""",
    )
    assert code == EXIT_PASS and gate_json["warned"] == 1
    assert gate_json["gates"][0]["result"] == "warn" and "WARN" in capsys.readouterr().out


# ---- GAT-09 precedence, input classes, domains, gate.json ---------------------------------------

PASSING = [{"name": "x", "unit": "ratio", "value": 1}]


@pytest.mark.parametrize(
    ("gates", "status", "code"),
    [
        (
            'gates: [{metric: x, bound: upper, op: "<=", value: 2}]',
            RunStatus("evalkit-summary", "pending", True),
            EXIT_INPUT,
        ),  # input-only beats all
        ('gates: [{metric: nothere, op: "<=", value: 2}]', RunStatus(), EXIT_INPUT),
        (  # an unknown metric is input-only: exit 2 even while unfinished
            'gates: [{metric: nothere, op: "<=", value: 2}]',
            RunStatus("evalkit-summary", "pending", True),
            EXIT_INPUT,
        ),
        (  # a per-judge metric can still appear: completion-dependent
            'gates: [{metric: headline, op: "<=", value: 2}]',
            RunStatus("evalkit-summary", "pending", True),
            EXIT_COST_CAP,
        ),
        (
            'gates: [{metric: x, op: "<=", value: 0}]',
            RunStatus("evalkit-summary", "pending", True),
            EXIT_COST_CAP,
        ),
        (
            'gates: [{metric: nothere, op: "<=", value: 2}]',
            RunStatus("evalkit-summary", "pending", False),
            EXIT_INPUT,
        ),
        (
            'gates: [{metric: groundedness, op: "<=", value: 2}]',
            RunStatus("evalkit-summary", "pending", False),
            EXIT_PENDING,
        ),
        ('gates: [{metric: headline, op: "<=", value: 2}]', RunStatus(), EXIT_INPUT),
        (
            'gates: [{metric: x, op: "<=", value: 0}]',
            RunStatus("evalkit-summary", "pending", False),
            EXIT_PENDING,
        ),
        ('gates: [{metric: x, op: "<=", value: 0}]', RunStatus(), EXIT_FAIL),
        ('gates: [{metric: x, op: "<=", value: 0, severity: warn}]', RunStatus(), EXIT_PASS),
        ('gates: [{metric: x, op: "<=", value: 2}]', RunStatus(), EXIT_PASS),
    ],
)
def test_exit_precedence_2_3_4_1_0(gates, status, code):
    assert check(PASSING, gates, status).exit_code == code


def test_numeric_domains_are_checked_without_float_conversion():
    big = 10**18 + 1  # float(big) == float(10**18): a float conversion would pass this gate
    assert float(big) == float(10**18)
    report = check(
        [{"name": "c", "unit": "count", "value": big}],
        f'gates: [{{metric: c, op: "<=", value: {10**18}}}]',
    )
    assert report.exit_code == EXIT_FAIL and report.rows[0].observed == big
    with pytest.raises(MetricsError, match=r"1\.0 is not a count"):
        parse_metrics(json.dumps(doc([{"name": "c", "unit": "count", "value": 2.0}])))
    assert (
        parse_metrics(
            '{"schema_version":"metrics-1","project":"p","run_id":"r",'
            '"created_at":"2026-11-17T00:00:00Z","metrics":[{"name":"c",'
            '"unit":"count","value":12345678901234567890123}]}'
        )
        .metrics[0]
        .value
        == 12345678901234567890123
    )


class _Recorder:
    """Records every attribute path the code under test reads."""

    def __init__(self, obj: Any, path: str, seen: set[str]) -> None:
        self._obj, self._path, self._seen = obj, path, seen

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._obj, name)
        path = f"{self._path}.{name}"
        self._seen.add(path)
        return _wrap(value, path, self._seen)


def _wrap(value: Any, path: str, seen: set[str]) -> Any:
    if isinstance(value, BaseModel) or dataclasses.is_dataclass(value):
        return _Recorder(value, path, seen)
    if isinstance(value, list | tuple) and value and isinstance(value[0], BaseModel):
        return type(value)(_Recorder(v, f"{path}[]", seen) for v in value)
    if isinstance(value, dict) and value and all(isinstance(v, BaseModel) for v in value.values()):
        return {k: _Recorder(v, f"{path}{{}}", seen) for k, v in value.items()}
    return value


def test_the_verdict_reads_only_inputs_in_the_input_classes_table(golden_run):
    """GAT-09: fails if the verdict (conversion included) reads an input not in the table."""
    summary, records, _ = golden_run
    seen: set[str] = set()
    converted = from_run_summary(
        _wrap(summary, "summary", seen),
        [_wrap(r, "records[]", seen) for r in records],
        n_resamples=200,
    )
    for metrics_file, gates_file in (
        (RO_METRICS, RO_GATES),
        (ASSAY_METRICS, ASSAY_GATES),
    ):
        ms = parse_metrics(metrics_file.read_text(encoding="utf-8"))
        gates = parse_gates(gates_file.read_text(encoding="utf-8"))
        evaluate(
            _wrap(ms, "metrics", seen),
            _wrap(gates, "gates", seen),
            _wrap(RunStatus(), "status", seen),
        )
    extra = parse_gates(
        'gates: [{metric: offer_rate, disparity: band, bound: upper, op: "<=", value: 1},\n'
        '        {metric: nothere, op: "<=", value: 1}]'
    )
    evaluate(
        _wrap(parse_metrics(json.dumps(doc(GROUPS))), "metrics", seen),
        _wrap(extra, "gates", seen),
        _wrap(RunStatus("evalkit-summary", "pending", True), "status", seen),
    )
    evaluate(
        _wrap(converted, "metrics", seen),
        _wrap(
            parse_gates(
                'gates: [{metric: cost_per_case, labels: {model: "*"}, bound: upper, op: "<=",'
                " value: 1}]"
            ),
            "gates",
            seen,
        ),
        _wrap(RunStatus("evalkit-summary"), "status", seen),
    )
    assert seen, "the recorder saw nothing"
    unlisted = sorted(seen - set(INPUTS))
    assert unlisted == [], f"inputs read but not in the input-classes table: {unlisted}"
    for model in (MetricsSummary, Metric, GatesFile):
        prefix = {
            "MetricsSummary": "metrics.",
            "Metric": "metrics.metrics[].",
            "GatesFile": "gates.",
        }[model.__name__]
        assert {prefix + f for f in model.model_fields} <= set(INPUTS)


def test_docs_list_exactly_the_input_classes():
    text = (ROOT / "docs" / "gates.md").read_text(encoding="utf-8")
    table = text.split("## Input classes", 1)[1]
    rows = re.findall(r"^\| `([^`]+)` \| ([a-z-]+) \| ([^|]+) \|$", table, re.M)
    assert {r[0]: r[1] for r in rows} == INPUTS
    for _, cls, exit_ in rows:
        assert exit_.strip() == ("2" if cls == "input-only" else "2, 3 or 4")


def _pending(data: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(data)
    d["judging_status"], d["finished_at"], d["self_preference"] = "pending", None, {}
    d["comparisons"] = [c for c in d["comparisons"] if c["metric"] != "headline"]
    for m in d["models"]:
        m["headline"], m["groundedness_mean"] = {}, {}
    return d


GATE_JSON_PATHS = {
    "unreadable": (None, None, EXIT_INPUT),
    "not json": ("{oops", 'gates: [{metric: x, op: "<=", value: 1}]', EXIT_INPUT),
    "schema error": (
        doc([{"name": "x", "unit": "rate", "value": 2}]),
        'gates: [{metric: x, op: "<=", value: 1}]',
        EXIT_INPUT,
    ),
    "no gates for metrics-1": (doc(PASSING), None, EXIT_INPUT),
    "bad gates yaml": (doc(PASSING), "gates: [{metric: x, op: '<', value: 1}]", EXIT_INPUT),
    "duplicate gate key": (
        doc(PASSING),
        "gates: [{metric: x, metric: y, op: '<=', value: 1}]",
        EXIT_INPUT,
    ),
    "fail": (doc(PASSING), 'gates: [{metric: x, op: "<=", value: 0}]', EXIT_FAIL),
    "pass": (doc(PASSING), 'gates: [{metric: x, op: "<=", value: 1}]', EXIT_PASS),
    "pending": ("PENDING", None, EXIT_PENDING),
    "cost cap": ("CAP", None, EXIT_COST_CAP),
}


@pytest.mark.parametrize("case", sorted(GATE_JSON_PATHS))
def test_gate_json_is_written_on_every_exit_path(case, tmp_path):
    data, gates, want = GATE_JSON_PATHS[case]
    golden = json.loads(GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    if data is None:
        summary = tmp_path / "missing.json"
    elif data in ("PENDING", "CAP"):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        summary = write(run_dir, "summary.json", _pending(golden))
        if data == "CAP":
            write(run_dir, "run.json", {"run_id": golden["run_id"], "stopped_at_cost_cap": True})
    else:
        summary = write(tmp_path, "m.json", data)
    code, gate_json = gate_cli(tmp_path, summary, gates)
    assert code == want == gate_json["exit_code"]
    if want == EXIT_INPUT:
        assert gate_json["errors"] and gate_json["verdict"] == "error"


def test_gate_json_is_written_even_on_an_unexpected_crash(tmp_path, monkeypatch):
    import evalkit.gates as G

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(G, "run_gate", boom)
    code, gate_json = gate_cli(tmp_path, write(tmp_path, "m.json", doc(PASSING)), None)
    assert code == EXIT_INPUT and gate_json["errors"] == ["unexpected RuntimeError"]


def test_pending_run_missing_headline_is_completion_dependent(tmp_path):
    golden = json.loads(GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    path = write(tmp_path, "summary.json", _pending(golden))
    gates = write(
        tmp_path,
        "g.yaml",
        'gates: [{metric: headline, labels: {model: "*"}, op: ">=", value: 0.5}]',
    )
    assert run_gate(path, gates).exit_code == EXIT_PENDING
    done = write(tmp_path, "done.json", golden)
    assert run_gate(done, gates).exit_code in (EXIT_PASS, EXIT_FAIL)


def test_run_json_of_another_run_is_bad_input(tmp_path):
    golden = json.loads(GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    path = write(tmp_path, "summary.json", golden)
    write(tmp_path, "run.json", {"run_id": "someone-else", "stopped_at_cost_cap": True})
    assert run_gate(path).exit_code == EXIT_INPUT


def test_a_run_stopped_at_the_cap_is_marked_and_gates_exit_3(tmp_path, monkeypatch, capsys):
    """End to end with scripted models (no API): a pending run resumed under a cap that is
    already spent stops at the cap; run.json says so and `evalkit gate` exits 3."""
    from evalkit import run as R
    from evalkit.datasets.pubmedqa import parse_pqa_l
    from tests.cli.test_cli import CONFIG
    from tests.golden.test_golden_run import MINI, reply
    from tests.pipeline.fakes import ScriptedBatchAdapter

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-key-1234567890")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key-1234567890")
    rows = json.loads(MINI.read_text(encoding="utf-8"))
    monkeypatch.setattr(R, "load_pqa_l", lambda: parse_pqa_l(rows))
    slow = ScriptedBatchAdapter(reply, pending_polls=10**6)
    monkeypatch.setattr(R, "make_adapters", lambda c, p: {"anthropic": slow, "openai": slow})
    (tmp_path / "evalkit.yaml").write_text(CONFIG, encoding="utf-8")
    base = ["run", "evalkit.yaml", "--n", "12", "--json"]
    assert cli.main([*base, "--wait", "0"]) == 4
    run_dir = tmp_path / json.loads(capsys.readouterr().out)["run_dir"]
    assert "stopped_at_cost_cap" not in (run_dir / "run.json").read_text(encoding="utf-8")
    cfg = write(
        tmp_path, "g.yaml", 'gates: [{metric: accuracy, labels: {model: "*"}, op: ">=", value: 0}]'
    )
    assert run_gate(run_dir / "summary.json", cfg).exit_code == EXIT_PENDING
    shutil.rmtree(tmp_path / ".evalkit" / "store")  # nothing cached: the resume must pay
    assert cli.main([*base, "--wait", "0", "--max-cost", "0.0000001"]) == 3
    capsys.readouterr()
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["stopped_at_cost_cap"]
    assert run_gate(run_dir / "summary.json", cfg).exit_code == EXIT_COST_CAP


def test_run_manifest_omits_the_cap_flag_by_default():
    from evalkit.run import RunManifest

    assert "stopped_at_cost_cap" in RunManifest._omit_if_default


# ---- GAT-10 latency and cost gate like any metric -----------------------------------------------


def test_latency_percentiles_and_cost_per_case_gate_like_any_metric():
    metrics = [
        {
            "name": "latency_p95",
            "unit": "ms",
            "value": 1800,
            "n": 200,
            "ci_low": 1500,
            "ci_high": 2100,
            "ci_method": "boot",
            "labels": {"model": "a"},
        },
        {"name": "latency_p50", "unit": "s", "value": 0.9, "labels": {"model": "a"}},
        {"name": "cost_per_case", "unit": "USD", "value": 0.012, "labels": {"model": "a"}},
    ]
    report = check(
        metrics,
        """
gates:
  - {metric: latency_p95, labels: {model: a}, bound: upper, op: "<=", value: 2000}
  - {metric: latency_p50, op: "<=", value: 1}
  - {metric: cost_per_case, op: "<=", value: 0.01, severity: warn}
""",
    )
    assert [r.result for r in report.rows] == ["fail", "pass", "warn"]
    assert report.exit_code == EXIT_FAIL and report.rows[0].observed == 2100


# ---- EFF-01 standard efficiency names -----------------------------------------------------------


def test_efficiency_names_carry_their_units():
    assert EFFICIENCY_NAMES["cost_per_correct"] == "currency"
    assert EFFICIENCY_NAMES["latency_p99"] == "ms" and EFFICIENCY_NAMES["error_rate"] == "rate"
    for bad in (
        {"name": "latency_p95", "unit": "USD", "value": 1},
        {"name": "cost_per_case", "unit": "count", "value": 1},
        {"name": "error_rate", "unit": "ratio", "value": 0.1},
        {"name": "error_rate", "unit": "rate", "value": 0.1, "labels": {"mode": "x"}},
        {"name": "some_count", "unit": "count", "value": 2.5},
    ):
        with pytest.raises(MetricsError):
            parse_metrics(json.dumps(doc([bad])))
    ok = parse_metrics(
        json.dumps(
            doc(
                [
                    {
                        "name": "tokens_in_per_case",
                        "unit": "count",
                        "value": 812.5,
                        "n": 200,
                        "labels": {"model": "a", "mode": "batch", "price_date": "2026-11-14"},
                    },
                    {"name": "system_cost", "unit": "USD", "value": 3.5},
                    {"name": "eval_overhead_cost", "unit": "NTD", "value": 0.7},
                ]
            )
        )
    )
    assert len(ok.metrics) == 3
    gated = check(
        [m.model_dump() for m in ok.metrics],
        'gates: [{metric: tokens_in_per_case, op: "<=", value: 1000}]',
    )
    assert gated.exit_code == EXIT_PASS


# ---- EFF-02 EvalKit's efficiency block ----------------------------------------------------------


def test_efficiency_block_from_a_run_has_seeded_intervals(golden_run):
    summary, records, _ = golden_run
    eff = efficiency_metrics(records)
    assert efficiency_metrics(records) == eff  # seeded: identical on rerun
    by = {(m.name, m.labels["model"]): m for m in eff}
    for model in ("claude-sonnet-5-5", "gpt-6.1-sol"):
        for name in (
            "cost_per_case",
            "cost_per_correct",
            "latency_p50",
            "latency_p95",
            "latency_p99",
        ):
            m = by[name, model]
            assert m.ci_low is not None and m.ci_high is not None, name
            assert m.ci_low <= m.value <= m.ci_high, name  # type: ignore[operator]
            assert m.ci_method and m.labels["mode"] == "live"
    for ms in summary.models:
        cpc = by["cost_per_case", ms.model]
        assert cpc.value == pytest.approx(ms.cost_per_100_usd / 100)
        assert by["latency_p50", ms.model].value == ms.latency_p50_ms
        assert by["latency_p95", ms.model].value == ms.latency_p95_ms
        assert by["error_rate", ms.model].k == sum(ms.n_errors.values())


def test_summary_json_gains_only_the_efficiency_field(golden_run):
    """D-70: summary.json gains the optional per-model `efficiency` (omitted when absent) and
    every other field keeps its bytes; gate conversion and the records agree with it."""
    from evalkit.pipeline.aggregate import summary_json
    from evalkit.schemas import RunSummary

    summary, records, _ = golden_run
    text = summary_json(summary)
    assert text == GOLDEN_SUMMARY.read_text(encoding="utf-8")
    data = json.loads(text)
    for m in data["models"]:
        eff = m.pop("efficiency")
        assert eff["mode"] == "live" and eff["price_date"] == "2026-11-14"
        assert eff["retry_rate"] is None and eff["eval_overhead_cost_usd"] > 0
    old = RunSummary.model_validate(data)  # a 0.2-shaped summary: no field, same bytes
    assert all(m.efficiency is None for m in old.models)
    assert "efficiency" not in summary_json(old)
    # the gate reads the block from summary.json alone, as the records give it
    from_summary = {(m.name, m.labels["model"]): m for m in from_run_summary(summary).metrics}
    for m in efficiency_metrics(records):
        twin = from_summary[m.name, m.labels["model"]]
        assert (twin.value, twin.ci_low, twin.ci_high, twin.n, twin.k) == (
            m.value,
            m.ci_low,
            m.ci_high,
            m.n,
            m.k,
        )
    for ms in summary.models:
        assert ms.efficiency is not None
        assert from_summary["eval_overhead_cost", ms.model].value == (
            ms.efficiency.eval_overhead_cost_usd
        )
        assert from_summary["system_cost", ms.model].value == pytest.approx(
            ms.cost_per_100_usd * ms.n / 100
        )
        assert from_summary["cost_per_case", ms.model].labels["price_date"] == "2026-11-14"


def test_report_shows_the_efficiency_block(golden_run):
    from evalkit.report.html import render_report

    summary, records, cases = golden_run
    html = render_report(summary, records, cases)
    assert "<h2 id=eff-h>Efficiency</h2>" in html and "Latency p50 / p95 / p99" in html
    assert render_report(summary, records, cases) == html  # seeded


def test_gating_a_run_folder_uses_its_records_for_efficiency(golden_run, tmp_path):
    summary, records, _ = golden_run
    from evalkit.pipeline.aggregate import write_summary

    write_summary(summary, tmp_path / "summary.json")
    (tmp_path / "records.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8"
    )
    cfg = write(
        tmp_path,
        "g.yaml",
        """
gates:
  - {metric: cost_per_case, labels: {model: "*"}, bound: upper, op: "<=", value: 1}
  - {metric: latency_p99, labels: {model: "*"}, bound: upper, op: "<=", value: 60000}
""",
    )
    report = run_gate(tmp_path / "summary.json", cfg)
    assert report.exit_code == EXIT_PASS and len(report.rows) == 6
    assert all(r.ci_method == "percentile bootstrap over cases (seeded)" for r in report.rows)
    shown = json.dumps(report.as_dict())
    assert not any(r.case_id in shown for r in records)  # aggregates only


# ---- verifier findings (D-70): held-out numbers, typed disparity groups, wording, domains ------


def test_heldout_split_stored_as_a_number_is_still_held_out():
    """GAT-04: `split: 2024` (a number) matches `heldout_splits: ['2024']` (and [2024]) as a
    canonical string, so its other labels never print, including on an error row."""
    metrics = [
        {"name": "acc", "unit": "count", "value": 0, "labels": {"split": 2024, "category": c}}
        for c in ("secret-cat-a", "secret-cat-b")
    ] + [{"name": "acc", "unit": "count", "value": 0, "labels": {"split": True, "category": "t"}}]
    for listed in ("['2024', 'true']", "[2024, true]"):
        report = check(
            metrics, f'gates: [{{metric: acc, op: "<=", value: 0}}]\nheldout_splits: {listed}'
        )
        shown = render_text(report) + json.dumps(report.as_dict())
        assert "secret-cat" not in shown and "category" not in shown, listed
        assert [r.heldout_rows for r in report.rows] == [2, 1]
    bound = check(
        metrics, 'gates: [{metric: acc, bound: upper, op: "<=", value: 0}]\nheldout_splits: [2024]'
    )
    assert bound.exit_code == EXIT_INPUT  # no interval: an error, still folded
    assert "secret-cat" not in render_text(bound) + json.dumps(bound.as_dict())


def test_disparity_groups_keep_their_label_type():
    """GAT-06: groups are keyed by label type and value, so 1 and "1" are two groups and the
    true gap (0.9 - 0.1) is reported, not a gap computed after one overwrote the other."""
    metrics = [
        {"name": "rt", "unit": "rate", "value": v, "n": 100, "k": round(v * 100), "labels": lab}
        for v, lab in ((0.1, {"g": 1}), (0.9, {"g": "1"}), (0.5, {"g": "2"}))
    ]
    report = check(metrics, 'gates: [{metric: rt, disparity: g, op: "<=", value: 0.5}]')
    row = report.rows[0]
    assert row.observed == pytest.approx(0.8) and report.exit_code == EXIT_FAIL
    assert row.note == '3 groups; highest "1", lowest 1'


GOLDEN_REPORT = ROOT / "tests" / "fixtures" / "golden" / "report_pubmedqa_mini.html"


def test_run_report_states_zero_of_n_never_a_zero_interval(golden_run):
    """GAT-07: gpt-6-luna's headline is 0 of 12; the report (tables, chart tooltips) reads
    "0 of 12, below X% at 95%", never "0.0% (95% CI 0.0% to 0.0%)" or "0.0% (0.0%–0.0%)"."""
    from evalkit.report.html import render_report

    summary, records, cases = golden_run
    html = render_report(summary, records, cases)
    assert "0.0% (95% CI 0.0% to 0.0%)" not in html
    assert "0.0% (0.0%–0.0%)" not in html and "0.0% <span class=muted>(0.0%–0.0%)" not in html
    assert "gpt-6-luna by claude-opus-5-5: 0 of 12, below 22.1% at 95%" in html


NAME_AND_DOMAIN_CASES: dict[str, dict[str, Any]] = {
    "name with a trailing newline": {"name": "x\n", "unit": "count", "value": 0},
    "unit with a trailing newline": {"name": "x", "unit": "USD\n", "value": 1},
    "rate interval above 1": {
        "name": "r", "unit": "rate", "value": 0.5, "ci_low": 0.1, "ci_high": 1.5, "ci_method": "m"
    },
    "rate interval below 0": {
        "name": "r", "unit": "rate", "value": 0.0, "ci_low": -0.1, "ci_high": 0.2, "ci_method": "m"
    },
    "count ci_low below 0": {
        "name": "c", "unit": "count", "value": 1, "ci_low": -3, "ci_high": 2, "ci_method": "m"
    },
    "rate value not k / n": {"name": "r", "unit": "rate", "value": 0.9, "n": 10, "k": 1},
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(NAME_AND_DOMAIN_CASES))
def test_anchored_names_and_interval_domains_are_checked(case):
    with pytest.raises(MetricsError):
        parse_metrics(json.dumps(doc([NAME_AND_DOMAIN_CASES[case]])))


def test_rates_rounded_to_six_decimals_and_null_provenance_pass():
    """assay-pipeline writes rates rounded to 6 decimals (7/12 -> 0.583333, 1/150 ->
    0.006667); they pass the k/n check. `provenance: null` means none given."""
    ok = parse_metrics(
        json.dumps(
            doc(
                [
                    {"name": "a", "unit": "rate", "value": 0.583333, "n": 12, "k": 7},
                    {"name": "b", "unit": "rate", "value": 0.006667, "n": 150, "k": 1},
                ],
                provenance=None,
            )
        )
    )
    assert ok.provenance.code_version is None and len(ok.metrics) == 2
    data = json.loads(ASSAY_METRICS.read_text(encoding="utf-8"))
    assert any(m["unit"] == "rate" and m.get("k") for m in data["metrics"])
    parse_metrics(ASSAY_METRICS.read_text(encoding="utf-8"))  # still valid
    parse_metrics(RO_METRICS.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            json.dumps(doc([{"name": "c", "unit": "count", "value": 0}])).replace(
                '"value": 0', '"value": ' + "9" * 5000
            ),
            "integer of 5000 digits is too large",
        ),
        (
            '{"schema_version": "metrics-1", "provenance": ' + "[" * 100_000 + "]" * 100_000 + "}",
            "nested too deeply",
        ),
    ],
)
def test_huge_integers_and_deep_nesting_get_clear_messages(text, message):
    with pytest.raises(MetricsError, match=message):
        parse_metrics(text)


@pytest.mark.parametrize(
    ("gates", "message"),
    [
        (
            "gates:\n- metric: x\n  op: '<='\n  value: 1\n  op: '>='\n",
            "duplicate key 'op' (line 5)",
        ),
        ("gates:\n- {metric: x, op: '<=', value: " + "9" * 5000 + "}\n", "integer is too large"),
        ("gates: " + "[" * 5000 + "]" * 5000 + "\n", "nested too deeply"),
    ],
)
def test_gates_file_problems_get_clear_messages(gates, message):
    with pytest.raises(MetricsError, match=re.escape(message)):
        parse_gates(gates)


def test_out_naming_a_folder_exits_2_and_leaves_no_temporary_file(tmp_path, capsys):
    path = write(tmp_path, "m.json", doc(PASSING))
    cfg = write(tmp_path, "g.yaml", 'gates: [{metric: x, op: "<=", value: 1}]')
    folder = tmp_path / "outdir"
    folder.mkdir()
    before = sorted(p.name for p in tmp_path.iterdir())
    code = cli.main(["gate", str(path), "--config", str(cfg), "--out", str(folder)])
    assert code == EXIT_INPUT and "it is a folder" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert list(folder.iterdir()) == []


def test_a_bad_command_line_still_writes_gate_json(tmp_path, capsys):
    out = tmp_path / "o" / "gate.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(["gate", "--out", str(out), "--no-such-option"])
    assert exc.value.code == EXIT_INPUT
    gate_json = json.loads(out.read_text(encoding="utf-8"))
    assert gate_json["exit_code"] == EXIT_INPUT and gate_json["verdict"] == "error"
    assert gate_json["errors"] == ["bad command line (see `evalkit gate --help`)"]
    assert [p.name for p in out.parent.iterdir()] == ["gate.json"]
    capsys.readouterr()


def test_unknown_metric_on_a_pending_run_exits_2(tmp_path):
    """GAT-09: an unknown metric name is input-only (exit 2) even while judging is pending;
    only a per-judge figure (headline, groundedness, ...) may still appear (exit 4)."""
    golden = json.loads(GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    path = write(tmp_path, "summary.json", _pending(golden))
    nosuch = write(tmp_path, "a.yaml", 'gates: [{metric: nosuch, op: ">=", value: 0.9}]')
    assert run_gate(path, nosuch).exit_code == EXIT_INPUT
    absent = write(
        tmp_path, "b.yaml", 'gates: [{metric: accuracy, labels: {model: zz}, op: ">=", value: 0}]'
    )
    assert run_gate(path, absent).exit_code == EXIT_INPUT
    head = write(tmp_path, "c.yaml", 'gates: [{metric: headline, op: ">=", value: 0.9}]')
    assert run_gate(path, head).exit_code == EXIT_PENDING


def test_a_real_run_records_eval_overhead_and_price_date(tmp_path, monkeypatch, capsys):
    """EFF-02 end to end with scripted models (no API): `evalkit run` writes each model's
    efficiency with the extractor and judge cost and the price date; a re-summary (as after
    calibration) keeps it; the gate reads it."""
    from evalkit import run as R
    from evalkit.datasets.pubmedqa import parse_pqa_l
    from evalkit.schemas import RunSummary
    from tests.cli.test_cli import CONFIG
    from tests.golden.test_golden_run import MINI, reply
    from tests.pipeline.fakes import ScriptedBatchAdapter

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-key-1234567890")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key-1234567890")
    rows = json.loads(MINI.read_text(encoding="utf-8"))
    monkeypatch.setattr(R, "load_pqa_l", lambda: parse_pqa_l(rows))
    fake = ScriptedBatchAdapter(reply)
    monkeypatch.setattr(R, "make_adapters", lambda c, p: {"anthropic": fake, "openai": fake})
    (tmp_path / "evalkit.yaml").write_text(CONFIG, encoding="utf-8")
    assert cli.main(["run", "evalkit.yaml", "--n", "12", "--json"]) in (0, 1)
    run_dir = tmp_path / json.loads(capsys.readouterr().out)["run_dir"]
    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    for m in summary.models:
        e = m.efficiency
        assert e is not None and e.eval_overhead_cost_usd and e.eval_overhead_cost_usd > 0
        assert e.price_date == "2026-11-14"
        assert e.system_cost_usd == pytest.approx(m.cost_per_100_usd * m.n / 100)
    again = R.summarise(run_dir, R.read_manifest(run_dir), R.read_records(run_dir), "complete")
    assert [m.efficiency for m in again.models] == [m.efficiency for m in summary.models]
    cfg = write(
        tmp_path,
        "g.yaml",
        'gates: [{metric: eval_overhead_cost, labels: {model: "*"}, op: "<=", value: 1}]',
    )
    report = run_gate(run_dir / "summary.json", cfg)
    assert report.exit_code == EXIT_PASS and len(report.rows) == len(summary.models)
