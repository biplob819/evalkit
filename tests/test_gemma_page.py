"""GEM-03, GEM-04 (B5.17): the /gemma/ page, Gemma Grounded Health's v2 main run rendered by
EvalKit's report renderer from GGH's committed summary, and its Pages deploy. No network."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GEMMA = ROOT / "docs" / "gemma"
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"

_spec = importlib.util.spec_from_file_location(
    "build_gemma_page", ROOT / "scripts" / "build_gemma_page.py"
)
assert _spec and _spec.loader
bgp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bgp)


def _source() -> dict:
    return json.loads((GEMMA / "source.json").read_text(encoding="utf-8"))


def _page() -> str:
    return (GEMMA / "index.html").read_text(encoding="utf-8")


# ---- GEM-03: the page ----------------------------------------------------------------------


def test_committed_page_passes_every_check():
    assert bgp.check(GEMMA) == []


def test_copied_files_match_recorded_sha256_and_no_per_case_files():
    src = _source()
    assert src["commit"] == "22579b32364a90ad632f2c2ed95a4a83df6a589e"
    for name in bgp.COPIED:
        assert src["files"][name]["from"] == f"docs/results/main_v2/{name}"
        assert bgp.sha256(GEMMA / name) == src["files"][name]["sha256"]
    names = sorted(p.name for p in GEMMA.iterdir())
    assert names == ["charts.vl.json", "index.html", "run.json", "source.json", "summary.json"]


def test_page_is_byte_identical_to_a_fresh_render():
    assert bgp.render(GEMMA) == _page()


def test_render_is_deterministic(tmp_path):
    copy = tmp_path / "gemma"
    shutil.copytree(GEMMA, copy)
    (copy / "index.html").unlink()
    bgp.build(copy)
    assert (copy / "index.html").read_bytes() == (GEMMA / "index.html").read_bytes()
    assert bgp.render(copy) == bgp.render(copy)


def test_page_states_judge_calibration_aim_rule_and_links():
    text = bgp._visible_text(_page())
    assert "gpt-6.1-sol alone is the judge, with gpt-6-luna extracting the claims" in text
    assert "a Claude Opus 5.5 reference labeller (a model's labels, not a human expert's" in text
    assert "v2-vs-base citation results depend on how the judge applies the citation" in text
    assert "v2-vs-v1 holds under both counts" in text
    page = _page()
    for url in (
        "https://github.com/biplob819/gemma-grounded-health/blob/main/docs/measurements/"
        "aim_rule_check.json",
        "https://huggingface.co/biplob123/gemma-4-e2b-grounded-health",
        "https://huggingface.co/biplob123/gemma-4-e2b-grounded-health-GGUF",
    ):
        assert f'href="{url}"' in page


def test_page_numbers_come_from_the_summary():
    summary = json.loads((GEMMA / "summary.json").read_text(encoding="utf-8"))
    run = json.loads((GEMMA / "run.json").read_text(encoding="utf-8"))
    text = bgp._visible_text(_page())
    assert summary["run_id"] == run["run_id"] == "r-715fc213a8a5" and run["n"] == 1000
    for m in summary["models"]:
        h = m["headline"]["gpt-6.1-sol"]
        assert f"{m['model']} by gpt-6.1-sol: {h['point']:.1%}" in text
    # the aim-rule figures GGH reports for the base model (published -> corrected)
    base = next(r for r in _source()["aim_rule"]["rates"] if r["model"] == "gemma-e2b-base-q4")
    assert round(base["published"]["citation_precision"], 3) == 0.747
    assert round(base["corrected"]["citation_precision"], 3) == 0.783
    assert round(base["published"]["aim_citation_rate"], 3) == 0.138
    assert round(base["corrected"]["aim_citation_rate"], 3) == 0.054
    assert "gemma-e2b-base-q4 0.747 0.783 0.138 0.054" in text


def test_page_has_no_case_ids_or_records():
    page = _page()
    assert "pqa-" not in page
    assert "<details" not in page
    assert not bgp.LONG_NUMBER_RE.findall(bgp._visible_text(page))
    assert "per-case records are not published here" in bgp._visible_text(page)
    for name in (*bgp.COPIED, "source.json"):
        assert "pqa-" not in (GEMMA / name).read_text(encoding="utf-8")


def test_self_test_passes():
    assert bgp.main(["--self-test"]) == 0


def test_check_mode_writes_nothing(tmp_path):
    copy = tmp_path / "gemma"
    shutil.copytree(GEMMA, copy)
    before = {p.name: p.read_bytes() for p in copy.iterdir()}
    assert bgp.main(["--check", "--gemma", str(copy)]) == 0
    assert {p.name: p.read_bytes() for p in copy.iterdir()} == before


def test_a_renderer_change_that_moves_an_anchor_is_an_error():
    with pytest.raises(bgp.PageError):
        bgp._replace_once("<h2 id=headline><h2 id=headline>", "<h2 id=headline>", "x")


def _fake_ggh(root: Path) -> Path:
    """A GGH checkout reduced to what cross_check reads, built from source.json."""
    src = _source()
    ggh = root / "ggh"
    for name in bgp.COPIED:
        dest = ggh / src["files"][name]["from"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(GEMMA / name, dest)
    cal = src["calibration"]
    bundle = {
        "calibration": [
            {
                "judge": cal["judge"],
                "wording": cal["wording"],
                "status": cal["status"],
                "reference_model": cal["reference_labeller"],
            }
        ]
    }
    (ggh / cal["source"]).parent.mkdir(parents=True, exist_ok=True)
    (ggh / cal["source"]).write_text(json.dumps(bundle), encoding="utf-8")
    aim = src["aim_rule"]
    readme = ggh / aim["text_source"]
    readme.write_text(
        " ".join([src["judging"]["text"], cal["run_stamp_note"], aim["text"]]), encoding="utf-8"
    )
    run = src["run"]["name"]
    measured = {
        "decision": aim["decision"],
        "post_hoc": aim["post_hoc"],
        "citation_rubric_hash": aim["citation_rubric_hash"],
        "note": aim["note"],
        "citation": {
            run: {
                r["model"]: {
                    aim["judge"]: {"published": r["published"], "corrected": r["corrected"]}
                }
                for r in aim["rates"]
            }
        },
        "family": {run: {"published": [], "corrected": []}},
    }
    for c in aim["comparisons"]:
        measured["family"][run][c["count"]].append(
            {k: c[k] for k in ("metric", "model_a", "model_b", "diff", "separated")}
        )
    (ggh / aim["source"]).parent.mkdir(parents=True, exist_ok=True)
    (ggh / aim["source"]).write_text(json.dumps(measured), encoding="utf-8")
    cfg = {
        "models": list(src["models"]["roles"]),
        "judges": [src["judging"]["judge"]],
        "extractor": src["judging"]["extractor"],
    }
    (ggh / src["models"]["source"]).parent.mkdir(parents=True, exist_ok=True)
    (ggh / src["models"]["source"]).write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return ggh


def test_cross_check_agrees_with_a_matching_ggh_tree_and_catches_a_changed_number(tmp_path):
    ggh = _fake_ggh(tmp_path)
    assert bgp.cross_check(GEMMA, ggh) == []
    aim_path = ggh / _source()["aim_rule"]["source"]
    measured = json.loads(aim_path.read_text(encoding="utf-8"))
    row = measured["citation"]["main_v2"]["gemma-e2b-base-q4"]["gpt-6.1-sol"]
    row["corrected"]["citation_precision"] = 0.9
    aim_path.write_text(json.dumps(measured), encoding="utf-8")
    assert any("gemma-e2b-base-q4 corrected" in p for p in bgp.cross_check(GEMMA, ggh))


# ---- GEM-04: Pages -------------------------------------------------------------------------


def _assemble() -> str:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = wf["jobs"]["deploy"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "Assemble the site")


def test_pages_workflow_copies_gemma_when_built():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)  # YAML 1.1 reads the key `on` as True
    assert "docs/gemma/**" in on["push"]["paths"]
    script = _assemble()
    guard = script.index("if [ -f docs/gemma/index.html ]; then")
    assert script.index("mkdir -p _site\n") < guard
    block = script[guard : script.index("fi", guard)]
    assert "mkdir -p _site/gemma" in block and block.rstrip().endswith("_site/gemma/")


def test_pages_workflow_copies_every_gemma_file():
    script = _assemble()
    guard = script.index("if [ -f docs/gemma/index.html ]; then")
    block = script[guard : script.index("fi", guard)]
    copy_line = next(line for line in block.splitlines() if line.strip().startswith("cp "))
    copied = {Path(a).name for a in copy_line.split()[1:-1]}
    assert copied == {p.name for p in GEMMA.iterdir()}
