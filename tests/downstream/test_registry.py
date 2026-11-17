"""DSC-01: the downstream registry names every project that builds on EvalKit, its pin and how
the canary runs its tests (D-61)."""

from __future__ import annotations

import os
import re
import tomllib
import urllib.request

import pytest

import downstream_canary as dc

PROJECTS = {
    "course-assistant",
    "agentic-analytics",
    "gemma-grounded-health",
    "assay-pipeline",
    "ai-visibility",
    "respectful-offers",
    "fraud-review-bedrock",
}


def test_registry_lists_the_seven_projects_once():
    rows = dc.load_registry()
    assert [r.project for r in rows] == sorted(r.project for r in rows), "keep rows sorted"
    assert {r.project for r in rows} == PROJECTS
    assert len(rows) == len(PROJECTS)


def test_registry_header_is_the_documented_columns():
    header = dc.REGISTRY.read_text(encoding="utf-8").splitlines()[0].split("\t")
    assert tuple(header) == dc.COLUMNS


def test_every_row_is_well_formed():
    for r in dc.load_registry():
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", r.repo), r
        assert r.branch and " " not in r.branch, r
        assert r.tests in {"auto", "none"}, r
        toks = r.install.split()
        assert len(toks) % 2 == 0 and all(t in {"--extra", "--group"} for t in toks[::2]), r
        assert (r.tests == "none") == (r.pin == "-"), "a project with no pin has nothing to test"
        assert r.summary == "-" or r.summary.endswith(".json"), r
        assert all(re.fullmatch(r"tests/[\w/.-]+\.py::\w+", d) for d in r.deselected()), r


def test_respectful_offers_imports_nothing_so_is_not_run():
    (row,) = [r for r in dc.load_registry() if r.project == "respectful-offers"]
    assert row.tests == "none" and row.pin == "-"


def test_a_malformed_registry_is_refused(tmp_path):
    bad = tmp_path / "downstream.tsv"
    bad.write_text("\t".join(dc.COLUMNS) + "\nonly\tthree\tcells\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        dc.load_registry(bad)
    bad.write_text("project\trepo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        dc.load_registry(bad)


def _evalkit_requirement(pyproject: dict) -> str:
    project = pyproject["project"]
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs += extra
    found = [r for r in reqs if re.match(r"evalkit-harness\b", r)]
    return found[0] if found else "-"


@pytest.mark.skipif(not os.environ.get("EVALKIT_CHECK_DOWNSTREAM"), reason="online check")
def test_recorded_pins_match_each_projects_pyproject():
    for r in dc.load_registry():
        url = f"https://raw.githubusercontent.com/{r.repo}/{r.branch}/pyproject.toml"
        with urllib.request.urlopen(url, timeout=30) as resp:
            pyproject = tomllib.loads(resp.read().decode("utf-8"))
        assert _evalkit_requirement(pyproject) == r.pin, r.project
