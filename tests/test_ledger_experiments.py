"""B5.7 experiment ledger and freeze guard (LED-03), across the five proof dimensions.

The freeze guard reuses B5.14: a look is a `heldout_scored` line `look:<candidate sha256>` over
every case of the split, so a second look for the same candidate is refused (exit 1, nothing
written). Fixtures: respectful-offers' and assay-pipeline's vendored metrics-1 exports
(tests/fixtures/gates, tests/fixtures/compare) and synthetic runs. No network, no keys.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from evalkit import cli
from evalkit import heldout as H
from evalkit import ledger as L
from evalkit.ledger.experiments import verdict_for
from tests.ledger_helpers import (
    EXAMPLE_RUN,
    FIX,
    HELD_IDS,
    HELDOUT_FIX,
    MANIFEST,
    at,
    no_network,  # noqa: F401
    rate,
    write_run,
)

RO_BASE = FIX / "gates" / "respectful_offers" / "metrics.json"
ASSAY = FIX / "compare" / "assay" / "old" / "metrics.json"


@pytest.fixture(autouse=True)
def _offline(no_network):  # noqa: F811
    yield


def _ab(tmp: Path, before_k: int, after_k: int, **settings: str) -> tuple[Path, Path]:
    before = write_run(
        tmp / "before",
        [rate("acc", before_k, 1000)],
        run_id="b",
        project="course-assistant",
        settings={"code_version": "v1", **settings},
    )
    after = write_run(
        tmp / "after",
        [rate("acc", after_k, 1000)],
        run_id="a",
        project="course-assistant",
        settings={"code_version": "v2", **settings},
    )
    return before, after


def _open(ledger: Path, baseline: Path, direction: str = "up", metric: str = "acc") -> str:
    entry = L.open_experiment(
        ledger,
        hypothesis="tighter grounding raises accuracy",
        change="grounding bar 0.7 -> 0.8",
        metric=metric,
        direction=direction,  # type: ignore[arg-type]
        baseline=baseline,
        at=at(1),
    )
    return entry["id"]


def _manifest_hashes() -> list[str]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [e["id_sha256"] for s in data["splits"].values() for e in s]


# ---- logical -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after_k", "direction", "verdict"),
    [
        (600, "up", "supported"),
        (600, "down", "refuted"),
        (400, "down", "supported"),
        (505, "up", "inconclusive"),
    ],
)
def test_verdict_from_compares_flag_and_the_declared_direction(
    tmp_path, after_k, direction, verdict
):
    """logical: supported when the primary row is flagged in the declared direction, refuted
    when flagged the other way, inconclusive when not flagged."""
    ledger = tmp_path / "experiments.jsonl"
    before, after = _ab(tmp_path, 500, after_k)
    exp = _open(ledger, before, direction)
    res = L.close_experiment(ledger, exp, before=before, after=after, at=at(2))
    assert res.verdict == verdict and res.exit_code == 0
    row = res.entry["primary_row"]
    assert row["flagged"] == (verdict != "inconclusive")
    assert verdict_for(row, direction) == verdict
    with pytest.raises(L.LedgerError, match="already closed"):
        L.close_experiment(ledger, exp, before=before, after=after)


def test_second_look_for_the_same_candidate_exits_1_and_writes_nothing(tmp_path, capsys):
    """logical: the first look is reserved in the B5.14 log; a second look for the same
    candidate is refused with exit 1 and neither ledger nor log changes."""
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "heldout-log.jsonl"
    before, after = _ab(tmp_path, 500, 600)
    exp = _open(ledger, before)
    entry = L.reserve_look(ledger, exp, candidate=after, manifest=MANIFEST, log=log, at=at(2))
    assert entry["n_cases"] == len(HELD_IDS)
    snapshot = (ledger.read_bytes(), log.read_bytes())
    with pytest.raises(L.LedgerError) as exc:
        L.reserve_look(ledger, exp, candidate=after, manifest=MANIFEST, log=log)
    assert exc.value.code == 1
    assert (ledger.read_bytes(), log.read_bytes()) == snapshot
    code = cli.main(
        [
            "experiment",
            "look",
            exp,
            "--candidate",
            str(after),
            "--manifest",
            str(MANIFEST),
            "--log",
            str(log),
            "--experiments",
            str(ledger),
        ]
    )
    assert code == 1 and (ledger.read_bytes(), log.read_bytes()) == snapshot
    # a second experiment cannot buy the same candidate another look either
    exp2 = _open(ledger, before)
    snapshot = (ledger.read_bytes(), log.read_bytes())
    other_ledger = tmp_path / "other.jsonl"
    exp3 = _open(other_ledger, before)
    with pytest.raises(L.LedgerError) as exc:  # a fresh experiment ledger, the same B5.14 log
        L.reserve_look(other_ledger, exp3, candidate=after, manifest=MANIFEST, log=log)
    assert exc.value.code == 1 and log.read_bytes() == snapshot[1]
    with pytest.raises(L.LedgerError) as exc:
        L.reserve_look(ledger, exp2, candidate=after, manifest=MANIFEST, log=log)
    assert exc.value.code == 1 and (ledger.read_bytes(), log.read_bytes()) == snapshot


def test_a_reseeded_or_upgraded_candidate_is_the_same_candidate(tmp_path):
    """logical: seed, EvalKit version and results format do not change the candidate hash;
    any other setting does; a recorded candidate_sha256 wins."""
    a = write_run(tmp_path / "a", [rate("acc", 5, 10)], settings={"prompt_hash": "p1", "seed": 7})
    b = write_run(
        tmp_path / "b",
        [rate("acc", 6, 10)],
        settings={"prompt_hash": "p1", "seed": 99, "results_format": "1.1"},
    )
    c = write_run(tmp_path / "c", [rate("acc", 5, 10)], settings={"prompt_hash": "p2", "seed": 7})
    assert L.candidate_hash(a) == L.candidate_hash(b) != L.candidate_hash(c)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"candidate_sha256": "AB" * 32, "seed": 3}), encoding="utf-8")
    assert L.candidate_hash(settings) == "ab" * 32
    bare = write_run(tmp_path / "bare", [rate("acc", 5, 10)])
    with pytest.raises(L.LedgerError, match="identifies it") as exc:
        L.candidate_hash(bare)
    assert exc.value.code == 2


# ---- contract ------------------------------------------------------------------------------------


@pytest.mark.contract
def test_experiment_entry_shapes(tmp_path):
    """contract: experiment_opened, experiment_look and experiment_closed carry the spec §3
    fields; ids are E<n> in ledger order."""
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "log.jsonl"
    before, after = _ab(tmp_path, 500, 600)
    opened = L.open_experiment(
        ledger, hypothesis="h", change="c", metric="acc", direction="up", baseline=before, at=at(1)
    )
    assert set(opened) == {
        "kind",
        "schema",
        "at",
        "project",
        "id",
        "hypothesis",
        "change",
        "primary",
        "baseline",
        "prev",
    }
    assert opened["id"] == "E1" and opened["primary"] == {
        "metric": "acc",
        "labels": {},
        "direction": "up",
    }
    assert set(opened["baseline"]) == {"run_id", "summary_sha256"}
    look = L.reserve_look(ledger, "E1", candidate=after, manifest=MANIFEST, log=log, at=at(2))
    assert set(look) == {
        "kind",
        "schema",
        "at",
        "project",
        "id",
        "candidate_sha256",
        "manifest_sha256",
        "split",
        "n_cases",
        "log_line_sha256",
        "prev",
    }
    closed = L.close_experiment(ledger, "E1", before=before, after=after, at=at(3)).entry
    assert set(closed) == {
        "kind",
        "schema",
        "at",
        "project",
        "id",
        "before",
        "after",
        "comparison",
        "primary_row",
        "verdict",
        "prev",
    }
    assert closed["comparison"]["schema_version"] == "compare-1"
    assert closed["verdict"] in ("supported", "refuted", "inconclusive")
    second = L.open_experiment(
        ledger, hypothesis="h2", change="c2", metric="acc", direction="down", baseline=before
    )
    assert second["id"] == "E2" and L.verify_ledger(ledger) == []
    with pytest.raises(L.LedgerError, match="no row"):
        L.open_experiment(
            ledger, hypothesis="h", change="c", metric="nope", direction="up", baseline=before
        )
    shown = L.list_experiments(ledger)
    assert [e["status"] for e in shown["experiments"]] == ["closed", "open"]
    assert shown["looks"]["heldout"][0]["candidate_sha256"] == look["candidate_sha256"]


@pytest.mark.contract
def test_look_writes_a_well_formed_heldout_scored_line(tmp_path):
    """contract: the look is a B5.14 `heldout_scored` line, rubric `look:<candidate sha256>`,
    over every case of the split, chained; the experiment entry names that line's sha256."""
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "log.jsonl"
    before, after = _ab(tmp_path, 500, 600)
    _open(ledger, before)
    cases = {"heldout": HELDOUT_FIX / "cases.jsonl", "public": HELDOUT_FIX / "public.yaml"}
    look = L.reserve_look(
        ledger, "E1", candidate=after, manifest=MANIFEST, log=log, cases=cases, at=at(2)
    )
    (line,) = H.read_log(log)
    assert line["kind"] == "heldout_scored"
    assert line["rubric_hash"] == "look:" + L.candidate_hash(after)
    assert line["case_id_sha256"] == sorted(H.case_id_hash(i) for i in HELD_IDS)
    assert line["n_cases"] == 3 and H.chain_problems(log) == []
    assert H.is_certified(log, list(HELD_IDS), line["rubric_hash"])
    raw = log.read_text(encoding="utf-8").splitlines()[0]
    import hashlib

    assert look["log_line_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    bad = tmp_path / "M.json"
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["counts"]["heldout"] = 4
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(L.LedgerError, match="edited by hand"):
        L.reserve_look(ledger, "E1", candidate=before, manifest=bad, log=log)


# ---- golden --------------------------------------------------------------------------------------


@pytest.mark.golden
def test_respectful_offers_challenger_case(tmp_path):
    """golden: respectful-offers' challenger (its OWNER D-50): the experiment declares churn.auc
    up against its real export, the challenger (by its challenger hash) gets one held-out look,
    the close records compare's output, a `supported` verdict and the held-out aggregate."""
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "tuning-log.jsonl"
    base = tmp_path / "baseline"
    base.mkdir()
    shutil.copyfile(RO_BASE, base / "metrics.json")
    data = json.loads(RO_BASE.read_text(encoding="utf-8"))
    data["run_id"] = "challenger-1"
    for m in data["metrics"]:
        if m["name"] == "churn.auc":
            m.update(value=0.861, ci_low=0.8594, ci_high=0.8627)
    challenger = tmp_path / "challenger"
    challenger.mkdir()
    (challenger / "metrics.json").write_text(json.dumps(data), encoding="utf-8")
    cand = "c" * 64
    (challenger / "settings.json").write_text(
        json.dumps({"candidate_sha256": cand}), encoding="utf-8"
    )
    result = write_run(
        tmp_path / "heldout-result",
        [
            rate("churn.recall", 41, 50, split="heldout"),
            rate("churn.recall", 4, 5, split="heldout", segment="s1"),
        ],
        project="respectful-offers",
        run_id="challenger-1-heldout",
        settings={"candidate_sha256": cand},
    )
    exp = L.open_experiment(
        ledger,
        hypothesis="bandit challenger ranks churn better",
        change="challenger model",
        metric="churn.auc",
        direction="up",
        baseline=base,
        at=at(1),
    )["id"]
    L.reserve_look(ledger, exp, candidate=challenger, manifest=MANIFEST, log=log, at=at(2))
    res = L.close_experiment(ledger, exp, before=base, after=challenger, heldout=result, at=at(3))
    assert res.verdict == "supported" and res.exit_code == 0
    assert res.entry["primary_row"]["metric"] == "churn.auc"
    assert res.entry["primary_row"]["method"].startswith("unpaired: MOVER")
    held = res.entry["heldout"]
    assert held["candidate_sha256"] == cand and held["split"] == "heldout"
    assert held["metrics"] == [
        {
            "k": 41,
            "labels": {"split": "heldout"},
            "n": 50,
            "name": "churn.recall",
            "unit": "rate",
            "value": 0.82,
        }
    ]
    assert held["heldout_rows_folded"] == 1
    assert L.verify_ledger(ledger) == [] and H.chain_problems(log) == []


@pytest.mark.golden
def test_assay_pipeline_heldout_runs_once(tmp_path):
    """golden: assay-pipeline's rule "the held-out set runs once, before publishing" (its OWNER
    D-12): its export as the candidate, frozen by its settings; a re-run with a new seed is the
    same candidate and its second look is refused."""
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "heldout-log.jsonl"
    cand = tmp_path / "main"
    cand.mkdir()
    shutil.copyfile(ASSAY, cand / "metrics.json")
    with pytest.raises(L.LedgerError, match="identifies it"):  # its export alone names nothing
        L.candidate_hash(cand)
    (cand / "settings.json").write_text(
        json.dumps({"db_sha256": "d" * 64, "seed": 7}), encoding="utf-8"
    )
    exp = L.open_experiment(
        ledger,
        hypothesis="publish main",
        change="none",
        metric="attack_successes",
        labels={"model": "claude-sonnet-5-5", "split": "public"},
        direction="down",
        baseline=cand,
    )["id"]
    assert L.reserve_look(ledger, exp, candidate=cand, manifest=MANIFEST, log=log)
    rerun = tmp_path / "main-rerun"
    shutil.copytree(cand, rerun)
    (rerun / "settings.json").write_text(
        json.dumps({"db_sha256": "d" * 64, "seed": 8}), encoding="utf-8"
    )
    snapshot = (ledger.read_bytes(), log.read_bytes())
    with pytest.raises(L.LedgerError) as exc:
        L.reserve_look(ledger, exp, candidate=rerun, manifest=MANIFEST, log=log)
    assert exc.value.code == 1 and (ledger.read_bytes(), log.read_bytes()) == snapshot


# ---- repro ---------------------------------------------------------------------------------------


def test_closing_twice_from_the_same_inputs_gives_identical_comparison_bytes(tmp_path):
    """repro: two fresh ledgers closed from the same inputs store byte-identical comparisons."""
    before, after = _ab(tmp_path, 500, 600)
    out = []
    for name in ("a.jsonl", "b.jsonl"):
        ledger = tmp_path / name
        exp = _open(ledger, before)
        res = L.close_experiment(ledger, exp, before=before, after=after, at=at(2))
        out.append(json.dumps(res.entry["comparison"], sort_keys=True))
        out.append((tmp_path / name).read_bytes())
    assert out[0] == out[2] and out[1] == out[3]


# ---- safety --------------------------------------------------------------------------------------


@pytest.mark.safety
def test_no_case_hash_or_id_in_the_experiment_ledger(tmp_path, capsys):
    """safety: the experiment ledger and every command's output hold no held-out case id or id
    hash; the look's case hashes live only in the B5.14 log."""
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "log.jsonl"
    before, after = _ab(tmp_path, 500, 600)
    assert (
        cli.main(
            [
                "experiment",
                "open",
                "--hypothesis",
                "h",
                "--change",
                "c",
                "--metric",
                "acc",
                "--direction",
                "up",
                "--baseline",
                str(before),
                "--experiments",
                str(ledger),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "experiment",
                "look",
                "E1",
                "--candidate",
                str(after),
                "--manifest",
                str(MANIFEST),
                "--log",
                str(log),
                "--experiments",
                str(ledger),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "experiment",
                "look",
                "E1",
                "--candidate",
                str(after),
                "--manifest",
                str(MANIFEST),
                "--log",
                str(log),
                "--experiments",
                str(ledger),
            ]
        )
        == 1
    )
    assert (
        cli.main(
            [
                "experiment",
                "close",
                "E1",
                "--before",
                str(before),
                "--after",
                str(after),
                "--experiments",
                str(ledger),
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["experiment", "show", "--experiments", str(ledger), "--json"]) == 0
    printed = capsys.readouterr()
    text = ledger.read_text(encoding="utf-8") + printed.out + printed.err
    for h in _manifest_hashes():
        assert h not in text and h[:12] not in text
    for case in HELD_IDS:
        assert case not in text
    assert all(
        h in log.read_text(encoding="utf-8") for h in sorted(H.case_id_hash(i) for i in HELD_IDS)
    )
    # a planted held-out id in the hypothesis is refused with --manifest (exit 2, not written)
    before_bytes = ledger.read_bytes()
    assert (
        cli.main(
            [
                "experiment",
                "open",
                "--hypothesis",
                f"fix {HELD_IDS[1]}",
                "--change",
                "c",
                "--metric",
                "acc",
                "--direction",
                "up",
                "--baseline",
                str(before),
                "--experiments",
                str(ledger),
                "--manifest",
                str(MANIFEST),
            ]
        )
        == 2
    )
    assert ledger.read_bytes() == before_bytes


@pytest.mark.safety
def test_heldout_result_only_as_split_aggregates_and_only_with_a_reserved_look(tmp_path):
    """safety: a held-out result without a reserved look for its candidate is refused (exit 2,
    nothing written); with one, only split-level rows are stored; a second result is refused."""
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "log.jsonl"
    before, after = _ab(tmp_path, 500, 600)
    result = write_run(
        tmp_path / "res",
        [rate("acc", 2, 3, split="heldout"), rate("acc", 1, 1, split="heldout", topic="x1")],
        project="course-assistant",
        run_id="res",
        settings={"code_version": "v2"},
    )
    exp = _open(ledger, before)
    snapshot = ledger.read_bytes()
    with pytest.raises(L.LedgerError, match="no held-out look was reserved") as exc:
        L.close_experiment(ledger, exp, before=before, after=after, heldout=result)
    assert exc.value.code == 2 and ledger.read_bytes() == snapshot
    other = write_run(
        tmp_path / "other",
        [rate("acc", 1, 3, split="heldout")],
        project="course-assistant",
        settings={"code_version": "v3"},
    )
    L.reserve_look(ledger, exp, candidate=after, manifest=MANIFEST, log=log)
    with pytest.raises(L.LedgerError, match="no held-out look"):
        L.close_experiment(ledger, exp, before=before, after=after, heldout=other)
    res = L.close_experiment(ledger, exp, before=before, after=after, heldout=result)
    held = res.entry["heldout"]
    assert [m["labels"] for m in held["metrics"]] == [{"split": "heldout"}]
    assert held["heldout_rows_folded"] == 1 and "x1" not in ledger.read_text(encoding="utf-8")
    exp2 = _open(ledger, before)
    with pytest.raises(L.LedgerError):  # the candidate's result is already recorded
        L.close_experiment(ledger, exp2, before=before, after=after, heldout=result)


# ---- freeze guard: candidate identity (verifier findings) ----------------------------------------


def _evalkit_run(
    tmp: Path,
    name: str,
    *,
    seed: int = 7,
    run_id: str = "g",
    effort: str | None = None,
    prompt_hash: str | None = None,
) -> Path:
    """A copy of the bundled EvalKit example run, re-seeded the way `evalkit run` re-seeds:
    the seed, the config hash (which covers the seed), the run id and the times change."""
    folder = tmp / name
    shutil.copytree(EXAMPLE_RUN, folder)
    config_hash = "golden" if seed == 7 else f"cfg-seed-{seed}"
    stamp = f"2026-11-{14 + seed % 10:02d}T12:00:00Z"
    run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    times = {"started_at": stamp, "finished_at": stamp.replace("T12", "T13")}
    run.update(seed=seed, config_hash=config_hash, run_id=run_id, **times)
    (folder / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    summary.update(config_hash=config_hash, run_id=run_id, **times)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = []
    for line in (folder / "records.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        rec.update(seed=seed, run_id=run_id, timestamp=stamp)
        if prompt_hash is not None:
            rec["prompt_hash"] = prompt_hash
        if effort is not None and rec["completion"]["model"] == "gpt-6-luna":
            rec["completion"]["params"]["effort"] = effort
        lines.append(json.dumps(rec))
    (folder / "records.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def test_a_reseeded_evalkit_run_is_the_same_candidate(tmp_path):
    """logical (LED-03): an EvalKit run re-seeded (new seed, so a new config hash, run id and
    times) is the same candidate and gets no second look; a changed prompt or a changed model
    effort is a different candidate."""
    a = _evalkit_run(tmp_path, "a")
    b = _evalkit_run(tmp_path, "b", seed=99, run_id="r-reseeded")
    assert L.candidate_hash(a) == L.candidate_hash(b) == L.candidate_hash(b / "summary.json")
    prompt = _evalkit_run(tmp_path, "p", prompt_hash="ffffffffffffffff")
    effort = _evalkit_run(tmp_path, "e", effort="high")
    assert len({L.candidate_hash(x) for x in (a, prompt, effort)}) == 3
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "log.jsonl"
    exp = L.open_experiment(
        ledger,
        hypothesis="h",
        change="c",
        metric="accuracy",
        labels={"model": "claude-sonnet-5-5"},
        direction="up",
        baseline=a,
    )["id"]
    L.reserve_look(ledger, exp, candidate=a, manifest=MANIFEST, log=log)
    snapshot = (ledger.read_bytes(), log.read_bytes())
    with pytest.raises(L.LedgerError) as exc:
        L.reserve_look(ledger, exp, candidate=b, manifest=MANIFEST, log=log)
    assert exc.value.code == 1 and (ledger.read_bytes(), log.read_bytes()) == snapshot


def test_a_run_and_its_settings_json_are_one_candidate(tmp_path):
    """logical (LED-03): a run folder and the settings.json inside it hash the same candidate,
    so naming the other buys no second look; a settings.json on its own identifies a candidate
    only by its candidate_sha256 (exit 2 otherwise); different candidates still differ."""
    run = write_run(
        tmp_path / "cand",
        [rate("acc", 5, 10)],
        project="evalkit",
        settings={"prompt_hash": "p9", "seed": 1},
        provenance={"code_version": "c1"},
    )
    settings = run.parent / "settings.json"
    assert L.candidate_hash(run.parent) == L.candidate_hash(settings) == L.candidate_hash(run)
    ek = _evalkit_run(tmp_path, "ek")
    (ek / "settings.json").write_text(json.dumps({"rules": "r1"}), encoding="utf-8")
    assert L.candidate_hash(ek) == L.candidate_hash(ek / "settings.json")
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "log.jsonl"
    exp = L.open_experiment(
        ledger, hypothesis="h", change="c", metric="acc", direction="up", baseline=run
    )["id"]
    L.reserve_look(ledger, exp, candidate=run.parent, manifest=MANIFEST, log=log)
    snapshot = (ledger.read_bytes(), log.read_bytes())
    with pytest.raises(L.LedgerError) as exc:
        L.reserve_look(ledger, exp, candidate=settings, manifest=MANIFEST, log=log)
    assert exc.value.code == 1 and (ledger.read_bytes(), log.read_bytes()) == snapshot
    alone = tmp_path / "copied" / "settings.json"
    alone.parent.mkdir()
    shutil.copyfile(settings, alone)
    with pytest.raises(L.LedgerError, match="candidate_sha256") as exc:
        L.candidate_hash(alone)
    assert exc.value.code == 2
    other = write_run(
        tmp_path / "other",
        [rate("acc", 5, 10)],
        project="evalkit",
        settings={"prompt_hash": "p10", "seed": 1},
        provenance={"code_version": "c1"},
    )
    assert L.candidate_hash(other) != L.candidate_hash(run)


def _edit_run_json(folder: Path, **changes: Any) -> Path:
    run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    run.update(changes)
    (folder / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    return folder


def _edit_records(folder: Path, **changes: Any) -> Path:
    lines = []
    for line in (folder / "records.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        rec.update(changes)
        lines.append(json.dumps(rec))
    (folder / "records.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def test_set_like_fields_are_canonical_and_systems_and_code_version_identify(tmp_path):
    """logical (LED-03, verifier pass 2): the candidate identity sorts every set-like field, so
    the judges (or models, or systems) listed in another order are the same candidate and buy
    no second look; `systems` (run.json, or a settings.json list) and the code version (the
    records' git_sha) are part of it where present, so changing either is a new candidate."""
    base = _evalkit_run(tmp_path, "base")
    recorded = json.loads((base / "run.json").read_text(encoding="utf-8"))
    swapped = _edit_run_json(_evalkit_run(tmp_path, "swapped"), judges=recorded["judges"][::-1])
    reordered = _edit_run_json(_evalkit_run(tmp_path, "models"), models=recorded["models"][::-1])
    assert L.candidate_hash(base) == L.candidate_hash(swapped) == L.candidate_hash(reordered)
    systems = {"claude-sonnet-5-5": "rag-v1", "gpt-6-luna": "rag-v1"}
    with_sys = _edit_run_json(_evalkit_run(tmp_path, "sys"), systems=systems)
    sys_rev = _edit_run_json(
        _evalkit_run(tmp_path, "sys-rev"), systems=dict(reversed(list(systems.items())))
    )
    sys_other = _edit_run_json(
        _evalkit_run(tmp_path, "sys-other"), systems={**systems, "gpt-6-luna": "rag-v2"}
    )
    assert L.candidate_hash(with_sys) == L.candidate_hash(sys_rev)
    assert len({L.candidate_hash(x) for x in (base, with_sys, sys_other)}) == 3
    code = _edit_records(_evalkit_run(tmp_path, "code"), git_sha="abc1234")
    assert L.candidate_hash(code) != L.candidate_hash(base)
    a = write_run(
        tmp_path / "ea",
        [rate("acc", 5, 10)],
        project="p",
        settings={"prompt_hash": "p1", "judges": ["j1", "j2"], "systems": ["s1", "s2"]},
    )
    b = write_run(
        tmp_path / "eb",
        [rate("acc", 5, 10)],
        project="p",
        settings={"prompt_hash": "p1", "judges": ["j2", "j1"], "systems": ["s2", "s1"]},
    )
    assert L.candidate_hash(a) == L.candidate_hash(b)
    ledger, log = tmp_path / "experiments.jsonl", tmp_path / "log.jsonl"
    exp = L.open_experiment(
        ledger,
        hypothesis="h",
        change="c",
        metric="accuracy",
        labels={"model": "claude-sonnet-5-5"},
        direction="up",
        baseline=base,
    )["id"]
    L.reserve_look(ledger, exp, candidate=base, manifest=MANIFEST, log=log)
    snapshot = (ledger.read_bytes(), log.read_bytes())
    with pytest.raises(L.LedgerError) as exc:
        L.reserve_look(ledger, exp, candidate=swapped, manifest=MANIFEST, log=log)
    assert exc.value.code == 1 and (ledger.read_bytes(), log.read_bytes()) == snapshot
