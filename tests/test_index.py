"""B5.5: the cross-project eval index (IDX-01, IDX-02, EFF-04).

Fixtures in tests/fixtures/index are synthetic snapshots shaped like each project's real output
(tests/fixtures/index/build_fixtures.py). The snapshot step is tested against local git
repositories made in tmp_path; nothing here touches the network.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evalkit import cli
from evalkit import index as ix
from evalkit.metrics import EFFICIENCY_NAMES
from evalkit.stats import clopper_pearson

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "index"
REGISTRY = FIX / "downstream.tsv"
CONFIG = FIX / "index.yaml"
SNAPS = FIX / "snapshots"
EXPECTED = FIX / "expected"
ORDER = [
    "agentic-analytics",
    "ai-visibility",
    "assay-pipeline",
    "course-assistant",
    "fraud-review-bedrock",
    "gemma-grounded-health",
    "respectful-offers",
    "evalkit",
]


def build(snaps: Path = SNAPS, config: Path | ix.IndexConfig = CONFIG) -> ix.Index:
    return ix.build_index(REGISTRY, snaps, config)


def entry(index: ix.Index, project: str) -> ix.ProjectEntry:
    return next(p for p in index.projects if p.project == project)


def copy_snaps(tmp_path: Path) -> Path:
    dest = tmp_path / "snapshots"
    shutil.copytree(SNAPS, dest)
    return dest


def reseal(folder: Path, role: str = "summary") -> None:
    """Rewrite the snapshot's sha256 for an edited file (the edit is then 'at the commit')."""
    snap = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
    data = (folder / ix.ROLE_FILES[role]).read_bytes()
    snap["files"][role]["sha256"] = hashlib.sha256(data).hexdigest()
    (folder / "snapshot.json").write_text(
        json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_cli(*argv: str, capsys: Any) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


# ---- IDX-01 -----------------------------------------------------------------------------------


def test_each_project_shows_headline_ci_calibration_gate_version_and_date():
    index = build()
    assay = entry(index, "assay-pipeline")
    assert assay.status == "ok" and assay.kind == "metrics-1"
    assert assay.headline is not None and assay.headline.problem is None
    assert assay.headline.text == "60 of 60, above 95.1% at 95%"  # exact wording, GAT-07
    assert assay.calibration == {
        "status": "stated",
        "text": "stated: judge_hand_label_agreement 90.0% [78.2%, 96.7%]",
        "judges": [],
        "figure": assay.calibration["figure"],  # type: ignore[index]
    }
    assert assay.gate is not None and assay.gate.status == "pass" and assay.gate.warned == 1
    assert (assay.evalkit_version, assay.evalkit_version_source) == ("0.3.0", "recorded in the run")
    assert assay.run_date == "2026-11-18" and assay.run_id == "main-fixture"
    assert assay.commit and len(assay.commit) == 40

    own = entry(index, "evalkit")
    assert own.headline is not None
    assert (own.headline.value, own.headline.ci_low, own.headline.ci_high) == (0.5, 0.25, 0.75)
    assert own.headline.ci_method == "percentile bootstrap over cases (seeded)"
    assert own.calibration is not None and own.calibration["status"] == "calibrated"
    assert "claude-opus-5-5 kappa 0.78 [0.70, 0.85] on 120 held-out claims" in str(
        own.calibration["text"]
    )
    assert own.gate is not None and own.gate.status == "fail" and own.gate.exit_code == 1
    assert own.gate.source == "EvalKit default gates: every model passes its thresholds"
    # the run records no EvalKit version: the repository's version at the commit is not the
    # build that made the run, so it is never shown as the run's version
    assert (own.evalkit_version, own.evalkit_version_source) == (None, "not recorded")
    assert own.evalkit_pin == "evalkit-harness 0.4.0 (the repository at the commit)"

    course = entry(index, "course-assistant")
    assert course.calibration is not None and course.calibration["status"] == "uncalibrated"
    assert course.evalkit_version is None and course.evalkit_pin == "evalkit-harness>=0.2,<0.3"
    assert "judge not calibrated for this task" in course.stamps[0]

    offers = entry(index, "respectful-offers")
    assert offers.simulated is True and offers.stamps[0].startswith("SIMULATED")
    assert offers.calibration is not None and offers.calibration["status"] == "not stated"
    html = ix.render_html(index)
    assert html.count('<span class="badge sim">SIMULATED</span>') == 2  # overview and section


def test_headline_is_never_picked_by_the_index():
    cfg = ix.parse_config(CONFIG.read_text(encoding="utf-8"))
    projects = dict(cfg.projects)
    projects.pop("gemma-grounded-health")
    wide = ix.Selector(metric="headline", labels={"model": "*", "judge": "*"})
    projects["evalkit"] = projects["evalkit"].model_copy(update={"headline": wide})
    index = build(config=cfg.model_copy(update={"projects": projects}))
    assert entry(index, "gemma-grounded-health").headline is None
    assert entry(index, "gemma-grounded-health").story["headline"]["value"] == "not designated"
    own = entry(index, "evalkit").headline
    assert own is not None and own.problem == "matches 6 rows (name every label)"
    assert own.value is None and entry(index, "evalkit").story["headline"]["value"] == "not shown"


def test_public_api_and_cli_contract(capsys):
    for name in ix.__all__:
        assert hasattr(ix, name), name
    assert (ix.EXIT_OK, ix.EXIT_MISSING, ix.EXIT_INPUT) == (0, 1, 2)
    assert (ix.INDEX_SCHEMA, ix.SNAPSHOT_SCHEMA, ix.CONFIG_SCHEMA) == (
        "index-1",
        "index-snapshot-1",
        "index-config-1",
    )
    for code in ("0", "1", "2"):
        assert f"  {code}  " in cli.INDEX_EXIT_CODES

    def choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        return next(
            dict(a.choices) for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )

    actions = choices(choices(cli.build_parser())["index"])
    assert set(actions) == {"snapshot", "build"}
    assert {"--source", "--commit", "--out"} <= set(actions["snapshot"]._option_string_actions)
    assert {"--snapshots", "--require-all"} <= set(actions["build"]._option_string_actions)
    with pytest.raises(SystemExit):
        cli.main(["index"])  # a subcommand is required
    capsys.readouterr()


def test_index_json_carries_the_portfolio_story_shapes():
    """specs/B5.5-eval-index/inputs.md: headline, evals, improvements, learnings, monitoring."""
    data = json.loads(ix.index_json(build()))
    assert data["schema_version"] == "index-1" and data["n_projects"] == 8
    statuses = {"pass", "final", "provisional", "fail", "pending"}
    kinds = {"north-star", "adoption", "quality", "guardrail", "cost"}
    for p in data["projects"]:
        story = p["story"]
        assert set(story) == {"headline", "evals", "improvements", "learnings", "monitoring"}
        assert set(story["headline"]) == {"value", "label", "note"}
        for e in story["evals"]:
            assert set(e) == {"name", "data", "method", "result", "status", "url"}
            assert e["status"] in statuses
        for i in story["improvements"]:
            assert set(i) == {"metric", "before", "after", "change", "url"}
        for lesson in story["learnings"]:
            assert set(lesson) == {"lesson", "now", "url"}
        for m in story["monitoring"]:
            assert set(m) == {"metric", "kind", "target", "current", "how"}
            assert m["kind"] in kinds
    assay = next(p for p in data["projects"] if p["project"] == "assay-pipeline")["story"]
    assert "run main-fixture" in assay["headline"]["note"]
    assert assay["improvements"] == [
        {
            "metric": "normal_answer_correct_rate (model=gpt-6.1-sol, split=public)",
            "before": "70.0%",
            "after": "78.3%",
            "change": "8.3% [1.7%, 15.0%], supported",
            "url": assay["improvements"][0]["url"],
        }
    ]
    assert assay["improvements"][0]["url"].endswith("/ledger/experiments.jsonl")
    assert assay["learnings"][0]["url"].endswith("/docs/lessons.md")
    kinds_seen = {m["metric"].split(" ")[0]: m["kind"] for m in assay["monitoring"]}
    assert kinds_seen["attack_successes"] == "guardrail"  # from the config
    assert kinds_seen["cost_per_correct"] == "cost"
    assert kinds_seen["normal_answer_correct_rate"] == "quality"
    offers = next(p for p in data["projects"] if p["project"] == "respectful-offers")["story"]
    # a simulated pass is provisional, never final
    assert {e["status"] for e in offers["evals"]} == {"provisional"}


def test_fixture_index_matches_the_golden_html_and_json_byte_for_byte(tmp_path, capsys):
    code, _ = run_cli(
        "index",
        "build",
        "--registry",
        str(REGISTRY),
        "--config",
        str(CONFIG),
        "--snapshots",
        str(SNAPS),
        "--out",
        str(tmp_path),
        capsys=capsys,
    )
    assert code == 0
    for name in ("index.html", "index.json"):
        assert (tmp_path / name).read_bytes() == (EXPECTED / name).read_bytes(), (
            f"{name} differs from the golden; regenerate with `evalkit index build --registry "
            "tests/fixtures/index/downstream.tsv --config tests/fixtures/index/index.yaml "
            "--snapshots tests/fixtures/index/snapshots --out tests/fixtures/index/expected`"
        )


def test_build_is_byte_reproducible_and_reads_no_clock(tmp_path, monkeypatch):
    first = (ix.render_html(build()), ix.index_json(build()))
    import datetime as dt

    class Later(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Later:  # type: ignore[override]
            return cls(2031, 1, 1, tzinfo=tz)

    monkeypatch.setattr(ix, "datetime", Later)
    second = (ix.render_html(build()), ix.index_json(build()))
    assert first == second
    ix.write_index(build(), tmp_path / "a")
    ix.write_index(build(), tmp_path / "b")
    for name in ("index.html", "index.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert b"\r\n" not in (tmp_path / "a" / "index.html").read_bytes()


def test_build_makes_no_network_model_or_git_call(monkeypatch):
    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("the build must not open a socket or run a process")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    index = build()
    assert [p.status for p in index.projects].count("ok") == 6
    tree = ast.parse((ROOT / "src" / "evalkit" / "index.py").read_text(encoding="utf-8"))
    names = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    names |= {
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    }
    assert not names & {"socket", "urllib", "http", "anthropic", "openai", "requests", "httpx"}
    for mod in ("evalkit.providers", "evalkit.adapters", "evalkit.pipeline.generate"):
        assert mod not in {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}


# ---- IDX-02 -----------------------------------------------------------------------------------


def test_every_registered_project_is_listed_and_unreadable_ones_are_missing_with_the_reason():
    index = build()
    assert [p.project for p in index.projects] == ORDER
    reasons = {p.project: p.reason for p in index.missing}
    assert reasons == {
        "ai-visibility": "no summary registered in the registry",
        "fraud-review-bedrock": "git fetch failed (exit 128)",
    }
    # another project's own summary format is missing unless the config says how to read it
    cfg = ix.parse_config(CONFIG.read_text(encoding="utf-8"))
    projects = {k: v for k, v in cfg.projects.items() if k != "agentic-analytics"}
    other = entry(build(config=cfg.model_copy(update={"projects": projects})), "agentic-analytics")
    assert other.status == "missing" and other.reason == (
        "not metrics-1 or an EvalKit summary.json: it has schema_version '…' but not "
        "EvalKit's summary.json keys (task, judge_mode, calibration_status, judging_status, "
        "models): another project's summary.json, not an EvalKit run"
    )
    for p in index.missing:
        assert p.headline is None and p.gate is None and p.calibration is None
        assert p.models == [] and p.run_id is None and p.evalkit_version is None
        assert p.story["headline"] == {"value": "missing", "label": p.project, "note": p.reason}
        assert p.story["evals"] == [] and p.story["monitoring"] == []
    html = ix.render_html(index)
    for p in index.missing:
        assert f'<h2 id="p-{p.project}">{p.project} <span class="badge fail">missing</span>' in html


def test_require_all_exits_1_when_a_project_is_missing_and_2_on_a_bad_registry(tmp_path, capsys):
    base = ["--registry", str(REGISTRY), "--config", str(CONFIG), "--snapshots", str(SNAPS)]
    code, out = run_cli(
        "index", "build", *base, "--out", str(tmp_path), "--require-all", "--json", capsys=capsys
    )
    assert code == 1
    assert json.loads(out)["missing"] == ["ai-visibility", "fraud-review-bedrock"]
    assert (tmp_path / "index.html").is_file()  # written even when missing projects exit 1
    bad = tmp_path / "bad.tsv"
    bad.write_text("project\trepo\nx\ty/z\n", encoding="utf-8")
    code, out = run_cli(
        "index", "build", "--registry", str(bad), "--out", str(tmp_path), "--json", capsys=capsys
    )
    assert code == 2 and "no column(s) branch, pin, summary" in out
    dup = tmp_path / "dup.tsv"
    dup.write_text(
        REGISTRY.read_text(encoding="utf-8")
        + REGISTRY.read_text(encoding="utf-8").splitlines()[1]
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ix.IndexInputError, match="listed twice"):
        ix.read_registry(dup)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("schema_version: index-config-1\nprojects: {nobody: {}}\n", encoding="utf-8")
    with pytest.raises(ix.IndexInputError, match="unregistered project"):
        ix.build_index(REGISTRY, SNAPS, cfg)
    cfg.write_text(
        "schema_version: index-config-1\nprojects: {evalkit: {headlin: {}}}\n", encoding="utf-8"
    )
    with pytest.raises(ix.IndexInputError, match="headlin"):
        ix.load_config(cfg)


def _corrupt_cases() -> list[tuple[str, str, str]]:
    return [
        ("bad-json", "gemma-grounded-health", "summary is not valid JSON"),
        ("not-utf8", "gemma-grounded-health", "summary file is not UTF-8"),
        ("changed", "gemma-grounded-health", "summary file changed since the snapshot"),
        ("no-file", "gemma-grounded-health", "summary file cannot be read (FileNotFoundError)"),
        ("bad-snapshot", "gemma-grounded-health", "snapshot.json is not valid JSON"),
        ("other-project", "gemma-grounded-health", "snapshot.json belongs to another project"),
        ("no-snapshot", "gemma-grounded-health", "not fetched: no snapshot"),
        ("bad-metrics", "respectful-offers", "invalid metrics-1: metrics[0].unit"),
        ("bad-summary", "course-assistant", "invalid EvalKit summary: "),
        ("dup-key", "respectful-offers", "summary is not valid JSON: a duplicate key"),
    ]


def _corrupt(folder: Path, case: str) -> None:
    summary = folder / "summary.json"
    if case == "bad-json":
        summary.write_text('{"schema_version": "1.0", ', encoding="utf-8")
        reseal(folder)
    elif case == "not-utf8":
        summary.write_bytes(b'{"x": "\xff\xfe"}')
        reseal(folder)
    elif case == "changed":
        summary.write_text(
            summary.read_text(encoding="utf-8").replace("pubmedqa", "pubmedqb", 1), encoding="utf-8"
        )
    elif case == "no-file":
        summary.unlink()
    elif case == "bad-snapshot":
        (folder / "snapshot.json").write_text("{not json", encoding="utf-8")
    elif case == "other-project":
        snap = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
        snap["project"] = "someone-else"
        (folder / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    elif case == "no-snapshot":
        (folder / "snapshot.json").unlink()
    elif case == "bad-metrics":
        data = json.loads(summary.read_text(encoding="utf-8"))
        data["metrics"][0]["unit"] = "percent"
        summary.write_text(json.dumps(data), encoding="utf-8")
        reseal(folder)
    elif case == "bad-summary":
        data = json.loads(summary.read_text(encoding="utf-8"))
        data["models"][0]["n"] = -1
        summary.write_text(json.dumps(data), encoding="utf-8")
        reseal(folder)
    elif case == "dup-key":
        text = summary.read_text(encoding="utf-8")
        summary.write_text(
            text.replace('"run_id": "kkbox-fixture"', '"run_id": "a", "run_id": "b"'),
            encoding="utf-8",
        )
        reseal(folder)


@pytest.mark.parametrize(("case", "project", "reason"), _corrupt_cases())
def test_each_unreadable_case_gives_its_own_stable_reason_and_no_figure(
    tmp_path, case, project, reason
):
    snaps = copy_snaps(tmp_path)
    _corrupt(snaps / project, case)
    first, second = build(snaps), build(snaps)
    e = entry(first, project)
    assert e.status == "missing" and e.reason.startswith(reason), e.reason
    assert e.reason == entry(second, project).reason
    assert e.headline is None and e.models == [] and e.gate is None
    assert [p.project for p in first.projects] == ORDER  # never dropped
    assert ix.render_html(first) == ix.render_html(second)


def test_an_unreadable_file_never_crashes_the_page(tmp_path, monkeypatch):
    snaps = copy_snaps(tmp_path)
    # a folder where the summary file should be
    (snaps / "gemma-grounded-health" / "summary.json").unlink()
    (snaps / "gemma-grounded-health" / "summary.json").mkdir()
    # broken companions show their own reason without hiding the summary
    (snaps / "assay-pipeline" / "gates.yaml").write_text("gates: [", encoding="utf-8")
    reseal(snaps / "assay-pipeline", "gates")
    (snaps / "assay-pipeline" / "experiments.jsonl").write_text("{oops\n", encoding="utf-8")
    reseal(snaps / "assay-pipeline", "experiments")
    (snaps / "course-assistant" / "run.json").write_text(
        '{"run_id": "someone else"}', encoding="utf-8"
    )
    reseal(snaps / "course-assistant", "run")
    index = build(snaps)
    gemma = entry(index, "gemma-grounded-health")  # IsADirectoryError, PermissionError on Windows
    assert gemma.status == "missing" and gemma.reason.startswith("summary file cannot be read (")
    assay = entry(index, "assay-pipeline")
    assert assay.status == "ok" and assay.headline is not None and assay.headline.problem is None
    assert assay.gate is not None and assay.gate.status == "error"
    assert assay.gate.errors == ["gates file: not valid YAML (line 1)"]
    assert assay.problems == ["experiments line 1 is not valid JSON"]
    course = entry(index, "course-assistant")
    assert course.status == "ok"
    assert course.problems == [
        "run.json does not belong to this summary's run; cost-cap state unknown"
    ]

    def boom(text: str) -> Any:
        raise RuntimeError("surprise")

    monkeypatch.setattr(ix, "_parse_summary", boom)
    index = build(snaps)
    assert {p.reason for p in index.projects if p.project in ("assay-pipeline", "evalkit")} == {
        "unexpected RuntimeError"
    }
    assert "<html" in ix.render_html(index)


# ---- EFF-04 -----------------------------------------------------------------------------------


def test_models_are_ordered_by_cost_per_correct_with_quality_beside_efficiency():
    index = build()
    assay = entry(index, "assay-pipeline")
    assert [m.model for m in assay.models] == ["gpt-6.1-sol", "claude-sonnet-5-5"]
    best, other = assay.models
    assert (best.rank, best.lowest, other.rank, other.lowest) == (1, True, 2, False)
    assert assay.comparison == (
        "lowest cost per correct: gpt-6.1-sol (clear: its interval is below every other model's)"
    )
    assert best.quality is not None and best.quality.text == "78.3% [68.3%, 86.3%]"
    cpc = best.efficiency["cost_per_correct"]
    assert (cpc.value, cpc.ci_low, cpc.ci_high, cpc.unit) == (0.0019, 0.001615, 0.00228, "USD")
    assert (best.mode, best.source) == ("live", "efficiency block")
    assert set(best.efficiency) == {
        "cost_per_case",
        "cost_per_correct",
        "error_rate",
        "tokens_in_per_case",
        "tokens_out_per_case",
        "tokens_reasoning_per_case",
        "tokens_cached_per_case",
        "cache_hit_share",
        "latency_p50",
        "latency_p95",
        "latency_p99",
    }
    own = entry(index, "evalkit")
    assert own.comparison == "lowest cost per correct: gpt-6.1-sol (intervals overlap)"
    assert [m.model for m in own.models][-1] == "gpt-6-luna"  # nothing correct: ranked last
    assert own.models[-1].rank is None
    gemma = entry(index, "gemma-grounded-health")
    assert gemma.comparison == (
        "lowest cost per correct: tied between gemma-e2b-base-q4, gemma-e2b-grounded-q4"
    )
    assert [m.lowest for m in gemma.models] == [True, True, False, False]
    offers = entry(index, "respectful-offers")
    assert offers.models == []
    assert offers.efficiency_note == "no per-model efficiency in this summary (no model label)"
    html = ix.render_html(index)
    assert "ordered by cost per correct (the comparison metric)" in html


def test_efficiency_uses_the_standard_names_falls_back_honestly_and_never_picks_one_of_many(
    tmp_path,
):
    index = build()
    for p in index.projects:
        for m in p.models:
            assert set(m.efficiency) <= set(EFFICIENCY_NAMES), p.project
    gemma = entry(index, "gemma-grounded-health")
    first = gemma.models[0]
    assert first.source == "from summary (no interval)"
    assert first.efficiency["cost_per_correct"].note == "from summary (no interval)"
    assert first.efficiency["cost_per_correct"].ci_low is None
    haiku = next(m for m in gemma.models if m.model == "claude-haiku-5-5")
    assert haiku.efficiency["cost_per_case"].value == pytest.approx(0.0005)  # 0.05 per 100
    # two cost rows for one model (another label) without a narrowing label: ambiguous
    snaps = copy_snaps(tmp_path)
    folder = snaps / "assay-pipeline"
    data = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    extra = next(m for m in data["metrics"] if m["name"] == "cost_per_correct")
    data["metrics"].append({**extra, "labels": {**extra["labels"], "price_date": "2026-11-01"}})
    (folder / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(folder)
    a = entry(build(snaps), "assay-pipeline")
    sonnet = next(m for m in a.models if m.model == extra["labels"]["model"])
    assert sonnet.efficiency["cost_per_correct"].problem == "ambiguous (2 rows)"
    assert sonnet.efficiency["cost_per_correct"].value is None and sonnet.rank is None


def test_cost_per_correct_ranking_does_not_depend_on_row_order(tmp_path):
    snaps = copy_snaps(tmp_path)
    folder = snaps / "assay-pipeline"
    data = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    data["metrics"].reverse()
    (folder / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(folder)
    before = entry(build(), "assay-pipeline")
    after = entry(build(snaps), "assay-pipeline")
    assert [m.model for m in after.models] == [m.model for m in before.models]
    assert [m.as_dict() for m in after.models] == [m.as_dict() for m in before.models]
    assert after.comparison == before.comparison


def test_heldout_rows_show_only_at_split_level(tmp_path):
    snaps = copy_snaps(tmp_path)
    cfg = ix.parse_config(CONFIG.read_text(encoding="utf-8"))
    assay_cfg = cfg.projects["assay-pipeline"]
    below = ix.Selector(
        metric="attack_pass_k",
        labels={"model": "pipeline", "split": "heldout", "category": "poisoning"},
    )
    split_level = ix.Selector(
        metric="attack_successes", labels={"model": "gpt-6.1-sol", "split": "heldout"}
    )
    for sel, problem in (
        (below, "a held-out row below split level is not shown"),
        (split_level, None),
    ):
        projects = {
            **cfg.projects,
            "assay-pipeline": assay_cfg.model_copy(update={"headline": sel}),
        }
        e = entry(build(snaps, cfg.model_copy(update={"projects": projects})), "assay-pipeline")
        assert e.headline is not None and e.headline.problem == problem
        if problem is None:
            assert e.headline.heldout and e.headline.note == "held-out split aggregate"
            assert set(e.headline.labels) <= {"split", "model", "judge"}
    blob = ix.index_json(build()) + ix.render_html(build())
    assert "poisoning" not in blob  # the held-out category label is never shown
    held = [
        r
        for r in entry(build(), "assay-pipeline").gate.rows  # type: ignore[union-attr]
        if r["labels"].get("split") == "heldout"
    ]
    assert held and all(
        r["heldout_rows"] >= 1 and set(r["labels"]) <= {"split", "model", "judge"} for r in held
    )


def test_secrets_long_values_and_markup_are_never_shown(tmp_path):
    snaps = copy_snaps(tmp_path)
    folder = snaps / "respectful-offers"
    data = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    key = "sk-ant-api03-" + "Ab1" * 10
    data["run_id"] = key
    data["provenance"]["evalkit"] = "<script>alert(1)</script>"
    data["metrics"][0]["labels"]["policy"] = "x" * 200
    aws = "AKIA" + "ABCDEFGHIJKLMNOP"  # built at run time: never a key in the source
    data["metrics"][0]["ci_method"] = aws
    (folder / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(folder)
    index = build(snaps)
    blob = ix.index_json(index) + ix.render_html(index)
    assert key not in blob and aws not in blob
    assert "x" * 200 not in blob
    html = ix.render_html(index)
    assert "<script>" not in html and "&lt;script&gt;" in html  # JSON keeps the text, as data
    assert entry(index, "respectful-offers").run_id == "<hidden>"
    # no case ids or claim text reach the index: RunSummary carries none, metrics-1 refuses ids
    data["metrics"][0]["labels"] = {"case_id": "c-1"}
    (folder / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(folder)
    e = entry(build(snaps), "respectful-offers")
    assert e.status == "missing" and "names an id" in e.reason and "c-1" not in e.reason


# ---- the snapshot step (local git repositories only) -------------------------------------------


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def make_repo(path: Path, files: dict[str, bytes]) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for name, data in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_bytes(data)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "fixture")
    return git(path, "rev-parse", "HEAD")


def one_row_registry(tmp_path: Path, summary: str) -> Path:
    reg = tmp_path / "reg.tsv"
    reg.write_text(
        "project\trepo\tbranch\tpin\tsummary\n"
        f"respectful-offers\tbiplob819/respectful-offers\tmain\tevalkit-harness>=0.3,<0.4\t{summary}\n",
        encoding="utf-8",
    )
    return reg


def test_snapshot_fetches_the_registered_files_at_a_commit_and_the_build_reads_them(tmp_path):
    offers = (SNAPS / "respectful-offers" / "summary.json").read_bytes()
    gates = (SNAPS / "respectful-offers" / "gates.yaml").read_bytes()
    repo = tmp_path / "offers"
    sha = make_repo(
        repo,
        {
            "results/kkbox/metrics.json": offers,
            "configs/gates.yaml": gates,
            ".env": b"ANTHROPIC_API_KEY=fake-docs-NEVER-PRINT\n",
        },
    )
    own = tmp_path / "self"
    self_sha = make_repo(
        own,
        {
            "docs/results/summary.json": (SNAPS / "evalkit" / "summary.json").read_bytes(),
            "docs/results/run.json": (SNAPS / "evalkit" / "run.json").read_bytes(),
            "pyproject.toml": b'[project]\nname = "evalkit-harness"\nversion = "9.9.9"\n',
        },
    )
    reg = one_row_registry(tmp_path, "results/kkbox/metrics.json")
    cfg = ix.parse_config(
        "schema_version: index-config-1\nprojects:\n  respectful-offers:\n"
        "    gates: configs/gates.yaml\n    lessons: docs/lessons.md\n"
    )
    out = tmp_path / "snaps"
    results = ix.snapshot(
        reg,
        out,
        config=cfg,
        sources={"respectful-offers": str(repo)},
        root=own,
        now="2026-11-18T10:00:00Z",
    )
    assert [(r.project, r.status, r.commit) for r in results] == [
        ("respectful-offers", "ok", sha),
        ("evalkit", "ok", self_sha),
    ]
    snap = json.loads((out / "respectful-offers" / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["commit"] == sha and snap["source"] == "local" and snap["lessons"] is None
    assert snap["files"]["summary"]["sha256"] == hashlib.sha256(offers).hexdigest()
    assert sorted(p.name for p in (out / "respectful-offers").iterdir()) == [
        "gates.yaml",
        "snapshot.json",
        "summary.json",
    ]
    assert b"NEVER-PRINT" not in b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    own_snap = json.loads((out / "evalkit" / "snapshot.json").read_text(encoding="utf-8"))
    assert own_snap["evalkit_version"] == "9.9.9" and "run" in own_snap["files"]
    index = ix.build_index(reg, out, cfg)
    e = entry(index, "respectful-offers")
    assert e.status == "ok" and e.commit == sha and e.gate is not None and e.gate.status == "pass"
    assert (
        e.url
        == f"https://github.com/biplob819/respectful-offers/blob/{sha}/results/kkbox/metrics.json"
    )
    own_entry = entry(index, "evalkit")  # the repository's version, never the run's
    assert own_entry.evalkit_version is None
    assert own_entry.evalkit_pin == "evalkit-harness 9.9.9 (the repository at the commit)"


def test_snapshot_reads_the_commit_asked_for_not_the_branch_head(tmp_path):
    repo = tmp_path / "offers"
    first = make_repo(repo, {"m.json": (SNAPS / "respectful-offers" / "summary.json").read_bytes()})
    (repo / "m.json").write_text("{}", encoding="utf-8")
    git(repo, "commit", "-qam", "second")
    reg = one_row_registry(tmp_path, "m.json")
    out = tmp_path / "snaps"
    own = tmp_path / "self"
    make_repo(own, {"README": b"x"})
    res = ix.snapshot(
        reg,
        out,
        sources={"respectful-offers": str(repo)},
        commits={"respectful-offers": first},
        root=own,
    )
    assert res[0].status == "ok" and res[0].commit == first
    assert entry(ix.build_index(reg, out), "respectful-offers").status == "ok"
    assert res[1].status == "missing" and res[1].reason.startswith("summary file not found at")


def test_snapshot_refuses_env_and_escaping_paths_and_records_failed_fetches(tmp_path):
    with pytest.raises(ix.IndexInputError, match=r"must not name a \.env file"):
        ix.parse_config(
            "schema_version: index-config-1\nprojects: {respectful-offers: {summary: .env}}\n"
        )
    own = tmp_path / "self"
    make_repo(own, {"README": b"x"})
    out = tmp_path / "snaps"
    for summary, reason in (
        ("../outside.json", "summary path refused: must not contain empty, . or .. parts"),
        ("config/.env.local", "summary path refused: must not name a .env file"),
    ):
        res = ix.snapshot(one_row_registry(tmp_path, summary), out, root=own)
        assert (res[0].status, res[0].reason) == ("missing", reason)
    res = ix.snapshot(
        one_row_registry(tmp_path, "m.json"),
        out,
        sources={"respectful-offers": str(tmp_path / "no-such-repo")},
        root=own,
    )
    assert res[0].status == "missing" and res[0].reason.startswith("git fetch failed (exit ")
    assert "fatal" not in res[0].reason  # git's own output is never recorded
    e = entry(ix.build_index(one_row_registry(tmp_path, "m.json"), out), "respectful-offers")
    assert e.status == "missing" and e.reason == res[0].reason
    with pytest.raises(ix.IndexInputError, match="unknown project"):
        ix.snapshot(one_row_registry(tmp_path, "m.json"), out, sources={"nobody": "x"}, root=own)


# ---- the confirmed headlines: selectors for each project's real shape ---------------------------
#
# Each project's session confirmed its headline (file, selector, label) on 2026-11-18. The real
# config (configs/index.yaml) is applied here to synthetic files shaped like each project's real
# one (tests/fixtures/index/shapes and snapshots; structure only, made-up values).

REAL_CONFIG = ROOT / "configs" / "index.yaml"
SHAPES = FIX / "shapes"


def real_cfg(project: str) -> ix.ProjectConfig:
    return ix.load_config(REAL_CONFIG).projects[project]


def own_snapshot(tmp_path: Path, project: str, data: bytes, path: str) -> Path:
    """A snapshot folder holding one summary file, as `evalkit index snapshot` writes it."""
    folder = tmp_path / project
    folder.mkdir(parents=True)
    (folder / "summary.json").write_bytes(data)
    snap = {
        "schema_version": "index-snapshot-1",
        "project": project,
        "repo": f"biplob819/{project}",
        "source": "local",
        "ref": "main",
        "commit": "a" * 40,
        "fetched_at": "2026-11-18T09:00:00Z",
        "status": "ok",
        "reason": "",
        "evalkit_pin": "evalkit-harness==0.3.0",
        "evalkit_version": None,
        "lessons": None,
        "files": {
            "summary": {
                "path": path,
                "name": "summary.json",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        },
    }
    (folder / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    return folder


def registry_row(project: str) -> ix.RegistryRow:
    return ix.RegistryRow(project, f"biplob819/{project}", "main", "evalkit-harness==0.3.0", None)


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("leakage", "a path starts with $"),
        ("$..leakage", "expected a field name after ."),
        ("$.rows[0]", "unexpected '['"),
        ("$.rows[*].x", "unexpected '['"),
        ("$.rows[?(@.x == 1)]", "expected a field name in the filter"),
        ("$.rows[?x != 1]", "a filter compares a field with == only"),
        ("$.rows[?x == undefined]", "a filter value is a string in double quotes"),
        ("$.rows[?x == 'one']", "a filter value is a string in double quotes"),
        ("$.rows[?x == 1 || y == 2]", "expected && or ]"),
        ("$.__import__('os')", "unexpected '('"),
        ("$." + "a" * 300, "a path is at most 200 characters"),
    ],
)
def test_path_selector_refuses_anything_beyond_fields_and_equality_filters(path, problem):
    with pytest.raises(ValueError, match=re.escape(problem)):
        ix.parse_path(path)
    text = (
        "schema_version: index-config-1\nprojects:\n  ai-visibility:\n"
        f"    own_format: {{}}\n    headline: {{path: {json.dumps(path)}}}\n"
    )
    with pytest.raises(ix.IndexInputError, match=re.escape(problem)):
        ix.parse_config(text)


def test_path_selector_filters_by_json_equality_and_keeps_the_item_for_fan_out():
    data = {
        "rows": [
            {"name": "a", "run": 1, "intent": None, "fixes": [], "ok": True},
            {"name": "b", "run": "1", "intent": "x", "fixes": ["f"], "ok": 1},
            {"name": "c", "run": 1.0, "fixes": [], "ok": False},
        ],
        "one": {"point": 0.5},
    }

    def names(path: str) -> list[Any]:
        return [v for v, _ in ix.eval_path(path, data)]

    assert names("$.rows[?run == 1].name") == ["a", "c"]  # JSON numbers; "1" is a string
    assert names('$.rows[?run == "1"].name') == ["b"]
    assert names("$.rows[?intent == null].name") == ["a"]  # an absent field is not null
    assert names("$.rows[?fixes == []].name") == ["a", "c"]
    assert names("$.rows[?ok == true].name") == ["a"]  # true is not 1
    assert names("$.rows[?ok == 1].name") == ["b"]
    assert names("$.rows[?fixes == [] && ok == false].name") == ["c"]
    assert names("$.rows[?run==1&&ok==true].name") == ["a"]  # spaces are optional
    assert names("$.rows.name") == []  # a field step never reaches into a list
    assert names("$.missing") == [] and names("$.one") == [{"point": 0.5}]
    kept = ix.eval_path("$.rows[?fixes == []].name", data)
    assert [item["name"] for _, item in kept if item is not None] == ["a", "c"]
    assert ix.eval_path("$.one.point", data) == [(0.5, None)]


def test_agentic_fans_out_per_backend_with_the_strict_score_beside():
    """agentic-analytics (own summary.json): accuracy on the rows with no fixes, one per
    backend in file order, its strict score beside; rows with fixes are never read."""
    a = entry(build(), "agentic-analytics")
    assert (a.status, a.kind, a.run_id, a.run_date) == (
        "ok",
        "own-format",
        "rescore-fixture",
        "2026-11-17",
    )
    h = a.headline
    assert h is not None and h.problem is None and h.value is None
    assert [r.labels for r in h.rows] == [
        {"backend": "claude_mcp"},
        {"backend": "gpt_mcp"},
        {"backend": "cortex_analyst"},
    ]
    pairs = [(r.value, r.companion.value if r.companion else None) for r in h.rows]
    assert pairs == [(0.7, 0.7), (0.9, 0.85), (0.8, 0.6)]
    assert h.rows[0].ci_method == "two-level bootstrap" and h.rows[0].companion_label == "strict"
    assert h.shown.startswith(
        "claude_mcp: 70.0% [63.0%, 76.0%] (strict 70.0% [62.0%, 77.0%]); gpt_mcp: 90.0%"
    )
    assert a.gate is not None and a.gate.status == "not gated"
    assert a.calibration is not None and a.calibration["status"] == "not stated"
    assert a.simulated is None and a.models == []  # not stated: never invented
    assert a.story["evals"][0]["status"] == "provisional"  # never final unless stated real
    assert a.story["headline"]["value"] == h.shown
    html = ix.render_html(build())
    assert "(strict <span" in html and "cortex_analyst" in html
    # a filter that keeps two rows for one backend, or several values without `per`: a problem,
    # never one of them
    cfg = ix.parse_config(CONFIG.read_text(encoding="utf-8"))
    head = cfg.projects["agentic-analytics"].headline
    assert head is not None
    for update, problem in (
        ({"path": "$.backends[?trials == 3].accuracy"}, "matches 5 values, not one per backend"),
        ({"per": None, "companion": None}, "matches 3 values, not one value"),
        ({"path": "$.backends[?fixes == [] && trials == 9].accuracy"}, "matches no value"),
        ({"path": "$.backends[?fixes == []].by_category"}, "the selected value is not a figure"),
        ({"per": "trials_x"}, "an item has no trials_x"),
    ):
        projects = {
            **cfg.projects,
            "agentic-analytics": cfg.projects["agentic-analytics"].model_copy(
                update={"headline": head.model_copy(update=update)}
            ),
        }
        got = entry(
            build(config=cfg.model_copy(update={"projects": projects})), "agentic-analytics"
        )
        assert got.headline is not None and got.headline.problem is not None
        assert got.headline.problem.startswith(problem), got.headline.problem
        assert got.headline.rows == [] and got.headline.value is None


def test_fraud_leakage_reads_its_own_figure_lower_is_better_and_simulated(tmp_path):
    """fraud-review-bedrock (summary-eval2.json): $.leakage {k, n, point, lo, hi}, lower is
    better, `data_label: simulated` shown as simulated (real config)."""
    data = (SHAPES / "fraud-review-bedrock.json").read_bytes()
    cfg = real_cfg("fraud-review-bedrock")
    path = "docs/measurements/summary-eval2.json"
    assert cfg.summary == path
    folder = own_snapshot(tmp_path / "a", "fraud-review-bedrock", data, path)
    e = ix.read_project(folder, registry_row("fraud-review-bedrock"), cfg)
    assert e.status == "ok" and e.kind == "own-format" and e.run_id == "eval2-fixture"
    h = e.headline
    assert h is not None and h.problem is None
    assert (h.value, h.k, h.n, h.ci_low, h.unit) == (0.0, 0, 40, 0.0, "rate")
    # the file's exact one-sided 95% upper bound is the one the wording states
    assert h.ci_high == pytest.approx(clopper_pearson(0, 40, 0.05, "upper").hi, abs=1e-12)
    assert h.text == "0 of 40, below 7.22% at 95%"
    assert h.direction == "lower is better" and h.ci_method == "exact 95% upper bound"
    assert e.headline_label == "Fraud leakage (frauds auto-cleared, lower is better)"
    assert e.simulated is True and e.stamps[0].startswith("SIMULATED")
    # the label already says "lower is better": not repeated beside the value or in the note
    assert "lower is better" in e.story["headline"]["label"]
    assert "lower is better" not in e.story["headline"]["note"]
    assert "simulated" in e.story["headline"]["note"]
    assert {x["status"] for x in e.story["evals"]} == {"provisional"}
    html = ix.render_html(ix.Index([e]))
    assert html.count('<span class="badge sim">SIMULATED</span>') == 2
    assert html.count("lower is better") == 2  # the label, in the overview and the section
    assert h.direction_in_label
    assert e.evalkit_pin == "evalkit-harness==0.3.0" and "pin evalkit-harness==0.3.0" in html
    # another data_label is false; no data_label is not stated (never invented)
    for i, (label, want) in enumerate((("real", False), (None, None))):
        doc = json.loads(data)
        if label is None:
            doc.pop("data_label")
        else:
            doc["data_label"] = label
        folder = own_snapshot(
            tmp_path / f"b{i}", "fraud-review-bedrock", json.dumps(doc).encode(), path
        )
        got = ix.read_project(folder, registry_row("fraud-review-bedrock"), cfg)
        assert got.simulated is want and not any(s.startswith("SIMULATED") for s in got.stamps)


def test_ai_visibility_selects_one_share_row_and_an_optional_comparison(tmp_path):
    """ai-visibility (studies/uk-health/results/summary.json): the presence share of nhs.uk
    for one model, run 1, all intents (intent null), and the same row for another model."""
    data = (SHAPES / "ai-visibility.json").read_bytes()
    cfg = real_cfg("ai-visibility")
    folder = own_snapshot(tmp_path, "ai-visibility", data, str(cfg.summary))
    e = ix.read_project(folder, registry_row("ai-visibility"), cfg)
    assert e.status == "ok" and e.run_id is None and e.run_date == "2026-11-16"
    h, c = e.headline, e.headline_comparison
    assert h is not None and c is not None and h.problem is None and c.problem is None
    assert (h.value, h.k, h.n, h.text) == (0.7, 70, 100, "70.0% [63.0%, 78.0%]")
    assert (c.value, c.k, c.n, c.text) == (0.12, 12, 100, "12.0% [5.0%, 20.0%]")
    assert e.headline_label == "Answers citing nhs.uk, GPT-6.1 Sol (run 1)"
    assert [x["name"] for x in e.story["evals"]] == [e.headline_label, e.comparison_label]
    assert e.simulated is None  # the file does not say: never invented
    html = ix.render_html(ix.Index([e]))
    assert "Comparison, <strong>same selector, model claude-sonnet-5-5</strong>" in html
    head = cfg.headline
    assert head is not None and head.path is not None
    for path, problem in (
        (head.path.replace(" && intent == null", ""), "matches 2 values, not one value"),
        (head.path.replace("run == 1", 'run == "1"'), "matches no value"),
    ):
        one = cfg.model_copy(update={"headline": head.model_copy(update={"path": path})})
        got = ix.read_project(folder, registry_row("ai-visibility"), one)
        assert got.headline is not None and got.headline.problem == problem


def test_respectful_offers_two_arm_row_is_the_headline_and_stays_simulated():
    """respectful-offers (metrics-1): the diff.* row with labels {a, b, variant}."""
    o = entry(build(), "respectful-offers")
    h = o.headline
    assert h is not None and h.problem is None
    assert h.metric == "diff.incremental_retained_per_1000_ntd"
    assert h.labels == {"a": "bandit", "b": "rule_following_baseline", "variant": "central"}
    assert h.text == "0.011 [0.004, 0.018]"  # the selector's three decimals
    assert o.simulated is True and "simulated" in o.story["headline"]["note"]
    real = real_cfg("respectful-offers").headline
    assert real is not None and real.metric == "diff.incremental_retained_per_1000_ntd"
    assert real.labels == {"a": "bandit", "b": "rule_following_baseline", "variant": "central"}
    assert real.decimals == 3 and real.label is not None and "simulated" in real.label


def test_the_real_index_config_names_each_confirmed_headline_and_nothing_else():
    cfg = ix.load_config(REAL_CONFIG)
    registered = {r.project for r in ix.read_registry(ROOT / "docs" / "downstream.tsv")}
    assert set(cfg.projects) == registered | {"evalkit"}
    assert cfg.projects["course-assistant"].headline is None  # no confirmation: not designated
    assay = cfg.projects["assay-pipeline"]
    # the owner named one model's row; the other model's quality stays not designated
    assert assay.quality is not None and assay.quality.labels == {
        "model": "claude-sonnet-5-5",
        "split": "public",
    }
    assert assay.quality.caption == (
        "strict query match; the answers state the gold value 100% of the time"
    )
    assert assay.pointer is not None and (assay.pointer.path, assay.pointer.file) == (
        "docs/results/LATEST",
        "metrics.json",
    )
    assert assay.headline is not None and assay.headline.metric == "scripted_attacker_stopped"
    gemma = cfg.projects["gemma-grounded-health"].headline
    assert gemma is not None and gemma.label == "Correct and fully grounded (fine-tuned v1)"
    assert gemma.labels == {"model": "gemma-e2b-grounded-q4", "judge": "claude-opus-5-5"}
    # each own-format selector reads its project's fixture shape without a problem
    agentic = cfg.projects["agentic-analytics"].headline
    assert agentic is not None and agentic.per == "backend"
    data = json.loads((SNAPS / "agentic-analytics" / "summary.json").read_text(encoding="utf-8"))
    fig = ix._select_path(data, agentic, frozenset({"heldout"}))
    assert fig.problem is None and len(fig.rows) == 3


def test_a_model_without_a_designated_quality_says_not_designated():
    cfg = ix.parse_config(CONFIG.read_text(encoding="utf-8"))
    assay = cfg.projects["assay-pipeline"].model_copy(update={"quality": None})
    e = entry(
        build(
            config=cfg.model_copy(update={"projects": {**cfg.projects, "assay-pipeline": assay}})
        ),
        "assay-pipeline",
    )
    assert e.models and all(m.quality is None for m in e.models)
    html = ix.render_html(ix.Index([e]))
    assert html.count("<td><span class=muted>not designated</span></td>") == len(e.models)


def test_a_quality_selector_that_names_a_model_designates_that_model_only():
    """assay-pipeline: quality `normal_answer_correct_rate {model: claude-sonnet-5-5, split:
    public}` with its owner's caption; the other model's cell is not designated, never filled
    from its own row."""
    cfg = ix.parse_config(CONFIG.read_text(encoding="utf-8"))
    q = ix.Selector(
        metric="normal_answer_correct_rate",
        labels={"model": "claude-sonnet-5-5", "split": "public"},
        caption="strict query match",
    )
    assay = cfg.projects["assay-pipeline"].model_copy(update={"quality": q})
    projects = {**cfg.projects, "assay-pipeline": assay}
    e = entry(build(config=cfg.model_copy(update={"projects": projects})), "assay-pipeline")
    by = {m.model: m for m in e.models}
    assert set(by) == {"gpt-6.1-sol", "claude-sonnet-5-5"}  # efficiency rows still listed
    sonnet = by["claude-sonnet-5-5"].quality
    assert sonnet is not None and sonnet.problem is None
    assert sonnet.labels == {"model": "claude-sonnet-5-5", "split": "public"}
    assert sonnet.note == "strict query match"
    assert by["gpt-6.1-sol"].quality is None
    html = ix.render_html(ix.Index([e]))
    assert "<span class=muted>(strict query match)</span>" in html
    assert html.count("<td><span class=muted>not designated</span></td>") == 1


@pytest.mark.parametrize(
    ("project", "problem"),
    [
        ("{summary: a.json, pointer: {path: LATEST, file: m.json}}", "give summary or pointer"),
        ("{headline: {path: $.x}}", "a path selector needs own_format"),
        ("{own_format: {}, headline: {metric: x}}", "headline and comparison use path"),
        ("{own_format: {}, headline: {path: $.x, per: name}}", "per needs a [?...] filter"),
        ("{own_format: {}, headline: {path: $.x, labels: {a: 1}}}", "a path selector has no"),
        (
            "{own_format: {}, headline: {path: '$.a[?k == 1].x', per: k, "
            "companion: {path: '$.a[?k == 1].y'}}}",
            "with the same per",
        ),
        ("{own_format: {}, gates: g.yaml}", "no quality, calibration"),
        ("{headline: {metric: x, unit: rate}}", "belong to a path selector"),
        ("{headline: {metric: x, decimals: 9}}", "decimals"),
        ("{headline: {metric: x, path: $.x}}", "or a path, not both"),
        ("{pointer: {path: ../LATEST, file: m.json}}", "must not contain empty, . or .. parts"),
        ("{pointer: {path: LATEST, file: .env}}", "must not name a .env file"),
        ("{own_format: {simulated: {path: data_label}}}", "a path starts with $"),
    ],
)
def test_config_refuses_mixed_or_unsafe_selectors(project, problem):
    text = f"schema_version: index-config-1\nprojects:\n  ai-visibility: {project}\n"
    with pytest.raises(ix.IndexInputError, match=re.escape(problem)):
        ix.parse_config(text)


def test_snapshot_follows_a_pointer_file_to_the_current_bundle(tmp_path):
    """assay-pipeline: docs/results/LATEST names the current bundle; metrics.json sits beside
    it. The snapshot records the pointer (path, target, sha256) and the summary path it gave."""
    metrics = (SNAPS / "assay-pipeline" / "summary.json").read_bytes()
    repo = tmp_path / "assay"
    make_repo(
        repo,
        {
            "docs/results/LATEST": b"main-b/bundle.json\n",
            "docs/results/main-a/metrics.json": b"{}",
            "docs/results/main-b/bundle.json": b"{}",
            "docs/results/main-b/metrics.json": metrics,
        },
    )
    reg = tmp_path / "reg.tsv"
    reg.write_text(
        "project\trepo\tbranch\tpin\tsummary\n"
        "assay-pipeline\tbiplob819/assay-pipeline\tmain\tevalkit-harness>=0.3,<0.4\t-\n",
        encoding="utf-8",
    )
    cfg = ix.parse_config(
        "schema_version: index-config-1\nprojects:\n  assay-pipeline:\n"
        "    pointer: {path: docs/results/LATEST, file: metrics.json}\n"
        "    headline: {metric: scripted_attacker_stopped}\n"
    )
    own = tmp_path / "self"
    make_repo(own, {"README": b"x"})
    out = tmp_path / "snaps"
    res = ix.snapshot(reg, out, config=cfg, sources={"assay-pipeline": str(repo)}, root=own)
    assert res[0].status == "ok" and res[0].files == {"summary": "docs/results/main-b/metrics.json"}
    snap = json.loads((out / "assay-pipeline" / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["pointer"] == {
        "path": "docs/results/LATEST",
        "target": "main-b/bundle.json",
        "sha256": hashlib.sha256(b"main-b/bundle.json\n").hexdigest(),
    }
    e = entry(ix.build_index(reg, out, cfg), "assay-pipeline")
    assert e.status == "ok" and e.summary_path == "docs/results/main-b/metrics.json"
    assert e.pointer is not None and e.pointer["path"] == "docs/results/LATEST"
    assert e.headline is not None and e.headline.problem is None
    assert "(named by <a href=" in ix.render_html(ix.Index([e]))
    for text, want in (
        (b"main-a\n", "ok"),  # a folder: the summary is metrics.json inside it
        (b"../../outside\n", "pointer names a refused path"),
        (b"main-a\nmain-b\n", "pointer file must hold exactly one path"),
        (b"main-z\n", "pointer names nothing at commit"),
        (b"\xff\n", "pointer file is not UTF-8"),
    ):
        (repo / "docs/results/LATEST").write_bytes(text)
        git(repo, "commit", "-qam", "move the pointer")
        res = ix.snapshot(reg, out, config=cfg, sources={"assay-pipeline": str(repo)}, root=own)
        if want == "ok":
            assert res[0].files["summary"] == "docs/results/main-a/metrics.json"
        else:
            assert res[0].status == "missing" and res[0].reason.startswith(want), res[0].reason
    (repo / "docs/results/LATEST").unlink()
    git(repo, "commit", "-qam", "drop the pointer")
    res = ix.snapshot(reg, out, config=cfg, sources={"assay-pipeline": str(repo)}, root=own)
    assert res[0].reason.startswith("pointer docs/results/LATEST not found at commit")


# ---- show only what the source states (verifier round 1, public page) --------------------------


PYPROJECT = b"""[project]
name = "assay"
dependencies = ["duckdb>=1", "evalkit-harness>=0.3,<0.4"]

[project.optional-dependencies]
eval = ["Evalkit_Harness >= 0.3, < 0.4"]

[dependency-groups]
dev = ["evalkit-harness>=0.3,<0.4", "pytest"]
"""

LOCK = b"""version = 1
[[package]]
name = "duckdb"
version = "1.1.0"

[[package]]
name = "evalkit-harness"
version = "0.3.0"
source = { registry = "https://pypi.org/simple" }
"""


def test_the_pin_is_read_from_the_projects_own_pyproject_and_a_stale_registry_is_flagged(
    tmp_path,
):
    """The registry's pin cell can go stale; the snapshot reads the EvalKit requirement from the
    project's pyproject.toml (and the version uv.lock resolved) at the commit, the index shows
    that, and a registry cell that disagrees is flagged, never shown as the pin."""
    metrics = (SNAPS / "respectful-offers" / "summary.json").read_bytes()
    repo = tmp_path / "offers"
    make_repo(
        repo,
        {"results/kkbox/metrics.json": metrics, "pyproject.toml": PYPROJECT, "uv.lock": LOCK},
    )
    reg = tmp_path / "reg.tsv"
    reg.write_text(
        "project\trepo\tbranch\tpin\tsummary\n"
        "respectful-offers\tbiplob819/respectful-offers\tmain\tevalkit-harness==0.1.0\t"
        "results/kkbox/metrics.json\n",
        encoding="utf-8",
    )
    own = tmp_path / "self"
    make_repo(own, {"README": b"x"})
    out = tmp_path / "snaps"
    ix.snapshot(reg, out, sources={"respectful-offers": str(repo)}, root=own)
    snap = json.loads((out / "respectful-offers" / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["evalkit_declared"] == ["evalkit-harness>=0.3,<0.4"]  # one, normalised
    assert snap["evalkit_locked"] == "0.3.0"
    e = entry(ix.build_index(reg, out), "respectful-offers")
    assert e.evalkit_pin == "evalkit-harness>=0.3,<0.4"
    assert e.evalkit_pin_source == "pyproject.toml at the commit (uv.lock: 0.3.0)"
    assert e.registry_pin == "evalkit-harness==0.1.0"
    assert e.pin_note == "the registry's pin (evalkit-harness==0.1.0) differs"
    html = ix.render_html(ix.Index([e]))
    assert "evalkit-harness&gt;=0.3,&lt;0.4" in html and "differs" in html
    # a registry cell that agrees is not flagged; credentials in a URL are never shown
    (repo / "pyproject.toml").write_bytes(
        b'[project]\nname = "x"\ndependencies = ['
        b'"evalkit-harness @ git+https://user:s3cret@github.com/biplob819/evalkit@'
        + b"a" * 40
        + b'"]\n'
    )
    git(repo, "commit", "-qam", "pin by url")
    ix.snapshot(reg, out, sources={"respectful-offers": str(repo)}, root=own)
    e = entry(ix.build_index(reg, out), "respectful-offers")
    assert e.evalkit_pin is not None and "s3cret" not in e.evalkit_pin
    assert "s3cret" not in ix.index_json(ix.Index([e]))
    # no pyproject requirement at the commit: the registry cell, labelled as such
    (repo / "pyproject.toml").unlink()
    git(repo, "commit", "-qam", "no pyproject")
    ix.snapshot(reg, out, sources={"respectful-offers": str(repo)}, root=own)
    e = entry(ix.build_index(reg, out), "respectful-offers")
    assert e.evalkit_pin == "evalkit-harness==0.1.0"
    assert e.evalkit_pin_source == "the registry (no EvalKit requirement in pyproject.toml)"
    assert e.pin_note == ""


def test_registry_pins_match_the_projects_pyprojects():
    """docs/downstream.tsv against each project's pyproject.toml at the commit the eval index
    snapshot read on 2026-11-18 (assay-pipeline and agentic-analytics were stale, verifier;
    respectful-offers had moved to 0.4; fraud-review-bedrock's extras were left out), as updated
    when a project reports a pin change (fraud-review-bedrock and respectful-offers moved to 0.4.1
    on 2026-11-18)."""
    pins = {r.project: r.pin for r in ix.read_registry(ROOT / "docs" / "downstream.tsv")}
    sha = "@git+https://github.com/biplob819/evalkit@"
    assert {k: ix._norm_req(v) for k, v in pins.items()} == {
        "agentic-analytics": "evalkit-harness==0.3.0",
        "ai-visibility": "evalkit-harness" + sha + "218192132ad601f02976d486d839e712e39bc4e2",
        "assay-pipeline": "evalkit-harness>=0.3,<0.4",
        "course-assistant": "evalkit-harness>=0.2,<0.3",
        "fraud-review-bedrock": "evalkit-harness[anthropic,openai]==0.4.1",  # its 0d87ead
        "gemma-grounded-health": "evalkit-harness[openai,anthropic]"
        + sha
        + "06591f8de9b178078c971940a83019a6530fd119",
        "respectful-offers": "evalkit-harness>=0.4.1,<0.5",  # its 24e518a
    }


def test_a_run_mode_or_simulated_flag_the_summary_does_not_record_is_not_recorded():
    """A summary written before 0.3 records no run mode, and an EvalKit summary never says
    whether it is simulated: the index says `not recorded`, never `live` or `false`."""
    from evalkit.metrics import from_run_summary
    from evalkit.schemas import RunSummary

    text = (SNAPS / "gemma-grounded-health" / "summary.json").read_text(encoding="utf-8")
    m = from_run_summary(RunSummary.model_validate_json(text))
    assert all("mode" not in x.labels for x in m.metrics if x.name == "error_rate")
    index = build()
    for name in ("gemma-grounded-health", "course-assistant"):
        e = entry(index, name)
        assert e.models and all(row.mode is None for row in e.models), name
        assert e.simulated is None, name
    html = ix.render_html(ix.Index([entry(index, "course-assistant")]))
    assert "<td>live</td>" not in html and "<td>not recorded</td>" in html
    # metrics-1 without a `simulated` key: not stated (metrics-1's default is not a statement)
    assay = entry(index, "assay-pipeline")
    assert assay.simulated is False  # its file states false
    assert {m.mode for m in assay.models} == {"live"}  # its file states the mode


def test_a_metrics1_file_that_omits_simulated_is_not_stated(tmp_path):
    snaps = copy_snaps(tmp_path)
    folder = snaps / "respectful-offers"
    data = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    data.pop("simulated")
    (folder / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(folder)
    e = entry(build(snaps), "respectful-offers")
    assert e.simulated is None and {x["status"] for x in e.story["evals"]} == {"provisional"}


def test_a_long_interval_method_fits_and_a_direction_in_the_label_is_shown_once(tmp_path):
    snaps = copy_snaps(tmp_path)
    folder = snaps / "respectful-offers"
    data = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    method = "paired block-bootstrap percentile 95%; Holm per metric and variant (D-16)"
    assert len(method) == 73
    for m in data["metrics"]:
        if m["name"] == "diff.incremental_retained_per_1000_ntd":
            m["ci_method"] = method
    (folder / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(folder)
    cfg = ix.parse_config(CONFIG.read_text(encoding="utf-8"))
    head = cfg.projects["respectful-offers"].headline
    assert head is not None
    labelled = head.model_copy(update={"direction": "higher", "label": "Kept (higher is better)"})
    projects = {
        **cfg.projects,
        "respectful-offers": cfg.projects["respectful-offers"].model_copy(
            update={"headline": labelled}
        ),
    }
    e = entry(build(snaps, cfg.model_copy(update={"projects": projects})), "respectful-offers")
    assert e.headline is not None and e.headline.ci_method == method
    assert method in e.story["headline"]["note"]
    html = ix.render_html(ix.Index([e]))
    assert html.count("higher is better") == 2  # once per place the label appears, no more
    assert "higher is better; " not in e.story["headline"]["note"]
    # still capped: a method longer than 128 characters is not shown
    for m in data["metrics"]:
        if m["name"] == "diff.incremental_retained_per_1000_ntd":
            m["ci_method"] = "x" * 129
    (folder / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(folder)
    e = entry(build(snaps), "respectful-offers")
    assert e.headline is not None and e.headline.ci_method == "<129 characters, not shown>"


def test_reasons_never_echo_a_key_or_value_from_the_source(tmp_path):
    snaps = copy_snaps(tmp_path)
    folder = snaps / "gemma-grounded-health"
    (folder / "summary.json").write_text(
        '{"a-0123456789abcdef": 1, "a-0123456789abcdef": 2}', encoding="utf-8"
    )
    reseal(folder)
    offers = snaps / "respectful-offers"
    data = json.loads((offers / "summary.json").read_text(encoding="utf-8"))
    data["metrics"][0]["labels"] = {"alert_id": "x"}
    (offers / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    reseal(offers)
    index = build(snaps)
    gemma, ro = entry(index, "gemma-grounded-health"), entry(index, "respectful-offers")
    assert gemma.reason == "summary is not valid JSON: a duplicate key in a JSON object"
    assert ro.status == "missing" and "names an id" in ro.reason
    blob = ix.index_json(index) + ix.render_html(index)
    assert "a-0123456789abcdef" not in blob and "alert_id" not in blob


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("a-0123456789abcdef", False),
        ("case-0042", False),
        ("member_00123", False),
        ("run-1a2b3c4d", False),
        ("claude_mcp", True),
        ("gpt-6.1-sol", True),
        ("claude-sonnet-5-5", True),
        ("gemma-e2b-grounded-q4", True),
        ("2026-11-01", True),
        ("B6", True),
    ],
)
def test_id_shaped_fan_out_labels_are_refused(value, shown):
    """A hyphenated or underscored prefix plus 8 or more hex characters, or 3 or more digits,
    is id-shaped: a figure labelled with one is not shown."""
    data = {"rows": [{"name": value, "fixes": [], "v": {"point": 0.5}}]}
    sel = ix.Selector(path="$.rows[?fixes == []].v", per="name")
    fig = ix._select_path(data, sel, frozenset({"heldout"}))
    if shown:
        assert fig.problem is None and fig.rows[0].labels == {"name": value}
    else:
        assert fig.problem == "a label value is shaped like an id: not shown"
        assert value not in json.dumps(fig.as_dict())
