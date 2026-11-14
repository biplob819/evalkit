import asyncio
import re
from pathlib import Path

import pytest

from evalkit.pipeline.aggregate import stamp_lines
from evalkit.report.readme import END, START, readme_block, readme_chart, readme_sentences, splice
from evalkit.schemas import RunSummary
from tests.golden.test_golden_run import _run, reply
from tests.pipeline.fakes import ScriptedBatchAdapter

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def summary(tmp_path_factory):
    s, *_ = asyncio.run(_run(tmp_path_factory.mktemp("store"), ScriptedBatchAdapter(reply)))
    return s


def test_chart_marks_match_the_summary_and_carry_every_caveat(summary):
    from html import escape

    svg = readme_chart(summary)
    assert svg == readme_chart(summary)  # deterministic
    assert svg.startswith("<svg") and "<script" not in svg and "http" not in svg.split(">", 1)[1]
    tips = re.findall(r"<g><title>([^<]+)</title>", svg)
    expected = {
        f"{m.model} by {j}: {iv.point:.1%} ({iv.lo:.1%} to {iv.hi:.1%})"
        for m in summary.models
        for j, iv in m.headline.items()
    }
    assert set(tips) == expected
    for line in stamp_lines(summary):
        assert escape(line) in svg
    assert "prefers-color-scheme:dark" in svg and 'role="img"' in svg


def test_sentences_quote_the_summary(summary):
    s1, s2, s3 = readme_sentences(summary, 12)
    assert "12 PubMedQA questions" in s1 and "two judges" in s1
    best = max(summary.models, key=lambda m: min(iv.point for iv in m.headline.values()))
    assert best.model in s2
    for iv in best.headline.values():
        assert f"{iv.point:.1%} (95% CI {iv.lo:.1%}–{iv.hi:.1%})" in s2
    assert "provisional" in s3 and "not calibrated" in s3


def test_pending_summary_says_so(summary):
    pending = summary.model_copy(
        update={
            "judging_status": "pending",
            "finished_at": None,
            "self_preference": {},
            "comparisons": (),
            "models": tuple(
                m.model_copy(update={"headline": {}, "groundedness_mean": {}})
                for m in summary.models
            ),
        }
    )
    assert "pending" in readme_sentences(pending, 12)[1]
    assert "<svg" in readme_chart(pending)


def test_block_is_spliced_idempotently(summary):
    block = readme_block(summary, 12, "https://biplob819.github.io/evalkit/")
    assert block.startswith(START) and block.endswith(END)
    assert "pip install 'evalkit-harness[anthropic,openai]'" in block and block.count("```") == 2
    once = splice("# EvalKit\n\nIntro.\n", block)
    assert splice(once, block) == once and once.startswith("# EvalKit\n\n" + START)


def test_published_readme_has_no_drift():
    """REL-02: the README block equals what the committed summary regenerates."""
    results = ROOT / "docs" / "results" / "summary.json"
    readme_text = (ROOT / "README.md").read_text("utf-8")
    if not results.is_file():
        # A headline block with no published run behind it would be unverifiable: fail.
        assert START not in readme_text, "README has a headline block but no published run"
        pytest.skip("no published run yet")
    import json

    s = RunSummary.model_validate_json(results.read_text("utf-8"))
    n = json.loads((ROOT / "docs" / "results" / "run.json").read_text("utf-8"))["n"]
    readme = (ROOT / "README.md").read_text("utf-8")
    block = readme_block(s, n, "https://biplob819.github.io/evalkit/")
    assert block in readme
    assert (ROOT / "docs" / "assets" / "headline.svg").read_text("utf-8") == readme_chart(s)


def test_pages_workflow_deploys_the_published_report():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "pages.yml").read_text("utf-8"))
    on = wf.get("on") or wf.get(True)  # YAML 1.1 reads the key `on` as True
    assert "docs/results/**" in on["push"]["paths"]
    perms = wf["permissions"]
    assert (
        perms["pages"] == "write" and perms["id-token"] == "write" and perms["contents"] == "read"
    )
    steps = wf["jobs"]["deploy"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Assemble the site")
    assert "cp docs/results/report.html _site/index.html" in script
    uses = [s.get("uses", "") for s in steps]
    assert any(u.startswith("actions/deploy-pages@") for u in uses)
    assert any(u.startswith("actions/upload-pages-artifact@") for u in uses)


def test_publish_script_refuses_pending_runs(tmp_path, summary):
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("pub", ROOT / "scripts" / "publish_results.py")
    assert spec and spec.loader
    pub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pub)
    pending = summary.model_copy(
        update={
            "judging_status": "pending",
            "finished_at": None,
            "self_preference": {},
            "comparisons": (),
            "models": tuple(
                m.model_copy(update={"headline": {}, "groundedness_mean": {}})
                for m in summary.models
            ),
        }
    )
    (tmp_path / "summary.json").write_text(pending.model_dump_json(), encoding="utf-8")
    (tmp_path / "run.json").write_text(json.dumps({"n": 12}), encoding="utf-8")
    assert pub.main([str(tmp_path)]) == 2


def test_publish_writes_the_run_and_regenerates_the_readme(tmp_path, summary):
    """REL-03 happy path: publishing a finished run copies it, draws the chart and splices
    the README; publishing again changes nothing (byte-identical)."""
    import importlib.util
    import json

    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    (run / "run.json").write_text(json.dumps({"n": 12}), encoding="utf-8")
    (run / "report.html").write_text("<html>report</html>", encoding="utf-8")
    (run / "charts.vl.json").write_text("{}", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# EvalKit\n\nIntro.\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("pub", ROOT / "scripts" / "publish_results.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([str(run), "--root", str(root)]) == 0
    first = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    assert mod.main([str(run), "--root", str(root)]) == 0
    assert first == {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    readme = (root / "README.md").read_text("utf-8")
    assert readme_block(summary, 12, mod.REPORT_URL) in readme
    assert (root / "docs" / "results" / "report.html").read_text("utf-8") == "<html>report</html>"
    assert (root / "docs" / "assets" / "headline.svg").read_text("utf-8") == readme_chart(summary)
    assert "evalkit init" in readme and "PubMedQA" in readme and "Jin et al." in readme
