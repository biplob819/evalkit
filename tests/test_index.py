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
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evalkit import cli
from evalkit import index as ix
from evalkit.metrics import EFFICIENCY_NAMES

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
    assert (own.evalkit_version, own.evalkit_version_source) == (
        "0.4.0",
        "pyproject.toml at the commit",
    )

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
    assert [p.status for p in index.projects].count("ok") == 5
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
        "agentic-analytics": (
            "not metrics-1 or an EvalKit summary.json: it has schema_version '1.0' but not "
            "EvalKit's summary.json keys (task, judge_mode, calibration_status, judging_status, "
            "models): another project's summary.json, not an EvalKit run"
        ),
        "ai-visibility": "no summary registered in the registry",
        "fraud-review-bedrock": "git fetch failed (exit 128)",
    }
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
    assert json.loads(out)["missing"] == [
        "agentic-analytics",
        "ai-visibility",
        "fraud-review-bedrock",
    ]
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
        ("dup-key", "respectful-offers", "summary is not valid JSON: duplicate key 'run_id'"),
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
    assert entry(index, "evalkit").evalkit_version == "9.9.9"


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
