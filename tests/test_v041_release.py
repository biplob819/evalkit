"""v0.4.1: an additive patch on 0.4.0 (owner-approved 2026-11-18): the eval index, two fixes."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

import evalkit

ROOT = Path(__file__).resolve().parents[1]
MEASURE = ROOT / "docs" / "measurements"
VERSION = "0.4.1"


def test_version_is_0_4_1_in_pyproject_package_and_lock():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["version"] == evalkit.__version__ == VERSION
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f'name = "evalkit-harness"\nversion = "{VERSION}"' in lock


def test_changelog_opens_with_the_0_4_1_section():
    text = (ROOT / "changelog.md").read_text(encoding="utf-8")
    headings = [
        h for h in re.findall(r"^## (.+)$", text, re.M) if re.fullmatch(r"\d+\.\d+\.\d+", h)
    ]
    assert headings[:2] == [VERSION, "0.4.0"], headings[:3]
    section = text.split(f"## {VERSION}\n", 1)[1].split("\n## ", 1)[0]
    for needed in ("evalkit index", "simulated", "not recorded", "docs/guide.md"):
        assert needed in section, needed


def test_the_index_command_is_public_and_documented():
    import evalkit.index as idx

    assert hasattr(idx, "build_index") and hasattr(idx, "snapshot")
    assert (ROOT / "docs" / "index.md").is_file()


def test_the_v0_4_1_release_record_matches_the_tag_and_pypi_files():
    found = sorted(MEASURE.glob(f"release-v{VERSION}-*.json"))
    if not found:
        pytest.skip("v0.4.1 not released yet")
    rec = json.loads(found[-1].read_text(encoding="utf-8"))
    assert rec["tag"] == f"v{VERSION}" == f"v{rec['version']}"
    assert re.fullmatch(r"[0-9a-f]{40}", rec["commit"])
    assert "owner" in rec["approved_by"]
    names = sorted(f["filename"] for f in rec["files"])
    assert names == [
        f"evalkit_harness-{VERSION}-py3-none-any.whl",
        f"evalkit_harness-{VERSION}.tar.gz",
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in rec["files"])
