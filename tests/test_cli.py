import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import evalkit
from evalkit.cli import main

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_version_string_matches_pyproject():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert evalkit.__version__ == meta["version"] == VERSION


def test_cli_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"evalkit {VERSION}"


def test_cli_runs_as_module():
    out = subprocess.run(
        [sys.executable, "-m", "evalkit.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == f"evalkit {VERSION}"


@pytest.mark.contract
def test_pyproject_distribution_metadata():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["name"] == "evalkit-harness"
    assert meta["requires-python"] == ">=3.11"
    assert meta["license"] == "MIT"
    assert meta["scripts"]["evalkit"] == "evalkit.cli:main"
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert "ruff" in (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
