import re
from pathlib import Path

import pytest

import status
from harness_lib import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"


def test_decisions_seeded_d1_to_d14():
    text = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    ids = {int(n) for n in re.findall(r"^\| (?:OWNER )?D-(\d+) \|", text, re.M)}
    assert set(range(1, 15)) <= ids


def test_lessons_and_changelog_exist():
    assert "L-1" in (ROOT / "docs" / "lessons.md").read_text(encoding="utf-8")
    assert "[W0.1]" in (ROOT / "changelog.md").read_text(encoding="utf-8")


def test_status_prints_graph_and_node(capsys):
    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "W0.1" in out and "D3.2" in out and "capabilities green" in out
    assert status.main(["W0.1"]) == 0
    assert "HAR-07" in capsys.readouterr().out
    assert status.main(["Z9.9"]) == 1


@pytest.mark.contract
def _yaml_code(path: Path) -> str:
    """Workflow text with comment-only lines and trailing comments removed."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split(" #", 1)[0].rstrip()
        if code.strip() and not code.lstrip().startswith("#"):
            lines.append(code)
    return "\n".join(lines)


def test_ci_workflow_covers_gates_and_matrix():
    ci = _yaml_code(WORKFLOWS / "ci.yml")
    assert re.search(r"^on:\n  push:", ci, re.M)
    for step in ("ruff check", "pyright", "pytest", "uv build", "secret_scan.py"):
        assert step in ci
    for v in ('"3.11"', '"3.12"', '"3.13"'):
        assert v in ci
    for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert os_name in ci
    assert "pull_request" in ci


@pytest.mark.contract
def test_release_workflow_uses_trusted_publishing():
    rel = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "id-token: write" in rel
    assert "uv publish --trusted-publishing always" in rel
    assert "tags:" in rel
    assert "PYPI_TOKEN" not in rel and "password" not in rel


def test_portfolio_notes_stay_out_of_public_repo():
    assert "Portfolio notes.md" in Path(ROOT / ".gitignore").read_text(encoding="utf-8")


@pytest.mark.contract
def test_workflows_grant_least_privilege():
    ci = _yaml_code(WORKFLOWS / "ci.yml")
    rel = _yaml_code(WORKFLOWS / "release.yml")
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)
    assert "secrets." not in ci and "secrets." not in rel
    assert "environment: pypi" in rel
    assert re.search(r"permissions:\n\s+id-token: write\n\s+contents: read", rel)


def test_catalog_round_trip_is_byte_identical(tmp_path):
    from harness_lib import CATALOG_TSV, load_catalog, write_catalog

    out = tmp_path / "catalog.tsv"
    write_catalog(load_catalog(), out)
    assert out.read_bytes() == CATALOG_TSV.read_bytes()


def test_status_output_is_deterministic(capsys):
    status.main([])
    first = capsys.readouterr().out
    status.main([])
    assert capsys.readouterr().out == first


def test_gitattributes_force_lf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text
