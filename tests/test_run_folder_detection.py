"""Strict run-folder detection (0.4.0) and clustered per-trial rates in security metrics-1.

A folder is an EvalKit run only when its run.json / summary.json carries EvalKit's marker
(`evalkit.runfolder`): a project's own summary.json (agentic-analytics writes one with
`schema_version: "1.0"`, like EvalKit's) is never read as an EvalKit run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import evalkit.security as S
from evalkit.compare import CompareError, compare_runs, load_input
from evalkit.gates import run_gate
from evalkit.metrics import parse_metrics
from evalkit.onboarding import EXAMPLE_DIR
from evalkit.run import RunError, read_manifest
from evalkit.runfolder import file_problem, marker_problem

# Shaped like agentic-analytics' results/<run>/summary.json and run.json (top-level keys only)
FOREIGN_SUMMARY = {
    "schema_version": "1.0",
    "run_id": "run-20261117T114442Z-a016f9",
    "config_hash": "a016f91f",
    "snapshot_sha256": "96f93c64",
    "as_of": "2026-08-31",
    "backends": [{"backend": "claude_mcp", "accuracy": {"point": 0.76, "lo": 0.68, "hi": 0.84}}],
    "uplifts": [],
    "started_at": "2026-11-17T11:44:42Z",
    "finished_at": "2026-11-17T11:57:14Z",
}
FOREIGN_RUN = {
    "as_of": "2026-08-31",
    "called": 720,
    "config": {"trials": 3, "seed": 0},
    "config_hash": "a016f91f",
    "run_id": "run-20261117T114442Z-a016f9",
}
METRICS = {
    "schema_version": "metrics-1",
    "project": "agentic-analytics",
    "run_id": "r1",
    "created_at": "2026-11-17T12:00:00Z",
    "metrics": [{"name": "accuracy", "unit": "rate", "value": 0.5, "n": 10, "k": 5}],
}


def _write(folder: Path, name: str, data: object) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_evalkit_files_carry_the_marker_and_foreign_ones_do_not():
    assert file_problem(EXAMPLE_DIR / "summary.json") is None
    assert file_problem(EXAMPLE_DIR / "run.json") is None
    why = marker_problem(FOREIGN_SUMMARY, "summary.json")
    assert why is not None and "not EvalKit's summary.json keys" in why
    assert "judging_status" in why
    run_why = marker_problem(FOREIGN_RUN, "run.json")
    assert run_why is not None and "schema_version" in run_why
    assert marker_problem([1], "run.json") == "is not a JSON object"


def test_gate_refuses_a_foreign_summary_json_and_says_why(tmp_path):
    path = _write(tmp_path / "results", "summary.json", FOREIGN_SUMMARY)
    report = run_gate(path)
    assert report.exit_code == 2
    msg = report.errors[0]
    assert msg.startswith("summary.json is not an EvalKit run summary")
    assert "another project's summary.json" in msg and "metrics-1" in msg


def test_compare_reads_metrics_json_beside_a_foreign_summary_json(tmp_path):
    """agentic-analytics' results folder: its own summary.json and run.json next to the
    metrics-1 file it writes for EvalKit. The folder is a metrics-1 input, not a run."""
    for name in ("old", "new"):
        d = tmp_path / name
        _write(d, "summary.json", FOREIGN_SUMMARY)
        _write(d, "run.json", FOREIGN_RUN)
        _write(d, "metrics.json", METRICS)
    got = load_input(tmp_path / "old")
    assert got.kind == "metrics-1" and got.path.endswith("metrics.json")
    assert compare_runs(tmp_path / "old", tmp_path / "new").exit_code == 0


def test_compare_names_why_a_folder_is_not_a_run(tmp_path):
    _write(tmp_path / "r", "summary.json", FOREIGN_SUMMARY)
    with pytest.raises(CompareError) as exc:
        load_input(tmp_path / "r")
    msg = exc.value.problems[0]
    assert "is not an EvalKit run folder: its summary.json has schema_version '1.0'" in msg
    assert "no metrics.json" in msg
    with pytest.raises(CompareError, match="is not an EvalKit run summary"):
        load_input(tmp_path / "r" / "summary.json")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "summary.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CompareError, match=r"summary\.json is not valid JSON"):
        load_input(tmp_path / "bad")


def test_a_foreign_run_json_does_not_hide_the_per_case_sidecar(tmp_path):
    """Before 0.4.0 any run.json beside metrics.json made compare skip cases.jsonl as an
    EvalKit run's case definitions: a project's own run.json silently dropped its values."""
    d = tmp_path / "r"
    _write(d, "run.json", FOREIGN_RUN)
    _write(d, "metrics.json", METRICS)
    rows = [{"case_id": f"c{i}", "metric": "accuracy", "value": i % 2} for i in range(10)]
    (d / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    got = load_input(d)
    assert got.cases is not None and len(got.cases) == 10


def test_an_evalkit_run_folder_is_still_a_run(tmp_path):
    d = tmp_path / "run"
    shutil.copytree(EXAMPLE_DIR, d)
    assert load_input(d).kind != "metrics-1"
    assert read_manifest(d).run_id == json.loads((d / "run.json").read_text("utf-8"))["run_id"]


def test_read_manifest_names_why_a_run_json_is_not_evalkits(tmp_path):
    _write(tmp_path, "run.json", FOREIGN_RUN)
    with pytest.raises(RunError, match=r"is not an EvalKit run folder: its run\.json has no"):
        read_manifest(tmp_path)


# ---------------------------------------------------------------- security: clustered trials


def _trials(pattern: list[str]) -> list:
    """One public case per string; 'S' a succeeded trial, 'B' a blocked one."""
    rows = []
    for i, s in enumerate(pattern):
        for t, ch in enumerate(s, start=1):
            rows.append(
                S.TrialResult(
                    case=f"p{i}",
                    split="public",
                    owasp="LLM01",
                    trial=t,
                    seed=0,
                    k=len(s),
                    case_sha256=f"{i:016x}",
                    status="scored",
                    outcome="succeeded" if ch == "S" else "blocked",
                    layer="executed" if ch == "S" else "model",
                )
            )
    return rows


def _metric(m, name: str, split: str):
    return next(x for x in m.metrics if x.name == name and x.labels["split"] == split)


def test_security_trial_success_rate_is_unchanged_by_default():
    trials = _trials(["SSS", "BBB"] * 10)
    plain = S.summarise(trials, n_resamples=200)
    assert "cluster" not in plain["splits"]["public"]["trial_success_rate"]
    m = S.to_metrics(plain, project="p", run_id="r", created_at="2026-11-17T00:00:00Z")
    row = _metric(m, "attack_trial_success_rate", "public")
    assert (row.k, row.n, row.ci_low, row.ci_method) == (30, 60, None, None)


def test_security_clustered_trial_success_rate_reaches_metrics_1_and_the_gate():
    """Each case's 3 trials agree: 60 trials carry 20 cases' information. The clustered
    interval is wider than Clopper-Pearson over trials and is what the gate reads."""
    from evalkit.gates import _interval
    from evalkit.stats import clopper_pearson

    trials = _trials(["SSS", "BBB"] * 10)
    s = S.summarise(trials, n_resamples=2000, cluster_trials=True)
    c = s["splits"]["public"]["trial_success_rate"]["cluster"]
    assert c["method"].startswith("two-level bootstrap")
    m = S.to_metrics(s, project="p", run_id="r", created_at="2026-11-17T00:00:00Z")
    row = _metric(m, "attack_trial_success_rate", "public")
    assert (row.value, row.k, row.n) == (0.5, 30, 60)
    assert (row.ci_low, row.ci_high, row.ci_method) == (c["lo"], c["hi"], c["method"])
    exact = clopper_pearson(30, 60)
    assert row.ci_high is not None and row.ci_low is not None
    assert row.ci_high - row.ci_low > 1.3 * (exact.hi - exact.lo)
    shown = _interval(row)
    assert shown is not None and shown[:2] == (c["lo"], c["hi"])
    # the written file round-trips through metrics-1 validation
    parse_metrics(m.model_dump_json(exclude_none=True))


def test_security_clustered_rate_drops_k_when_cases_have_unequal_trials():
    trials = _trials(["SSS", "B", "SB", "BBB"])
    s = S.summarise(trials, n_resamples=200, cluster_trials=True)
    m = S.to_metrics(s, project="p", run_id="r", created_at="2026-11-17T00:00:00Z")
    row = _metric(m, "attack_trial_success_rate", "public")
    # mean of per-case rates (1 + 0 + 0.5 + 0) / 4 = 0.375, not 4 of 9
    assert row.value == pytest.approx(0.375) and row.k is None and row.n == 9
