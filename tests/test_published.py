"""HAR-08, OSS-01, OSS-08: what was actually published as v0.1.0.

The release record in docs/measurements is written from the release workflow run and PyPI's
own JSON. These tests hold it to the source tree, so a later version bump or workflow change
that no longer matches what was shipped is caught. Set EVALKIT_CHECK_PYPI=1 to also compare
the record with PyPI over the network (never in CI).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "measurements" / "release-v0.1.0-2026-11-15.json"
OSES = ("ubuntu-latest", "macos-latest", "windows-latest")


def _record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_name_is_ours_on_pypi():
    rec, proj = _record(), _project()
    assert rec["project"] == proj["name"] == "evalkit-harness"
    assert rec["pypi_url"] == f"https://pypi.org/project/{proj['name']}/{rec['version']}/"
    assert rec["requires_python"] == proj["requires-python"]
    assert proj["scripts"]["evalkit"].startswith("evalkit.")


def test_the_release_matches_the_tag_and_the_package_version():
    import evalkit

    rec = _record()
    assert rec["tag"] == f"v{rec['version']}"
    assert re.fullmatch(r"[0-9a-f]{40}", rec["commit"])
    assert _project()["version"] == evalkit.__version__

    def parts(v: str) -> tuple[int, ...]:
        return tuple(map(int, v.split(".")))

    # equal at release; after a later bump the shipped version stays older than the tree
    assert parts(rec["version"]) <= parts(evalkit.__version__)


def test_it_was_published_by_trusted_publishing_from_the_release_workflow():
    rec = _record()
    assert rec["workflow"] == ".github/workflows/release.yml"
    assert (ROOT / rec["workflow"]).exists()
    assert rec["publish_method"].startswith("trusted publishing")
    assert rec["workflow_run"].startswith("https://github.com/biplob819/evalkit/actions/runs/")
    assert rec["jobs"]["checks"] == rec["jobs"]["publish"] == "success"


def test_the_published_package_passed_the_install_smoke_on_every_os():
    rec = _record()
    for os_name in OSES:
        assert rec["jobs"][f"smoke ({os_name})"] == "success"
    steps = " ".join(rec["smoke_steps"])
    for step in ("from PyPI", "doctor --offline", "report --example", "check_public_api.py"):
        assert step in steps
    workflow = (ROOT / rec["workflow"]).read_text(encoding="utf-8")
    assert "scripts/check_public_api.py" in workflow and all(o in workflow for o in OSES)


def test_the_published_files_are_pinned_by_digest():
    rec = _record()
    names = sorted(f["filename"] for f in rec["files"])
    v = rec["version"]
    assert names == [f"evalkit_harness-{v}-py3-none-any.whl", f"evalkit_harness-{v}.tar.gz"]
    for f in rec["files"]:
        assert re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) and f["size"] > 10_000


@pytest.mark.skipif(not os.environ.get("EVALKIT_CHECK_PYPI"), reason="network check, opt-in")
def test_pypi_still_serves_exactly_the_recorded_files():
    rec = _record()
    url = f"https://pypi.org/pypi/{rec['project']}/{rec['version']}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:
        live = json.load(resp)
    served = {f["filename"]: f["digests"]["sha256"] for f in live["urls"]}
    assert served == {f["filename"]: f["sha256"] for f in rec["files"]}
