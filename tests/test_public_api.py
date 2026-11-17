"""OSS-08: the public API contract (docs/versioning.md). Removing or renaming any of these is
a breaking change; this test makes it a deliberate one."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_public_api", Path(__file__).resolve().parents[1] / "scripts" / "check_public_api.py"
)
assert _spec and _spec.loader
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)
PUBLIC: dict[str, list[str]] = _api.PUBLIC
CLI: dict[str, set[str]] = _api.CLI


@pytest.mark.parametrize("module", sorted(PUBLIC))
def test_every_public_name_imports(module):
    mod = importlib.import_module(module)
    for name in PUBLIC[module]:
        assert hasattr(mod, name), f"{module}.{name} is public (docs/versioning.md)"


@pytest.mark.parametrize("module", sorted(set(PUBLIC) - {"evalkit"}))
def test_all_lists_exactly_the_public_names(module):
    mod = importlib.import_module(module)
    assert sorted(mod.__all__) == sorted(PUBLIC[module]), (
        f"{module}.__all__ changed: update PUBLIC here, docs/versioning.md and the changelog"
    )


def test_entry_point_groups_are_stable():
    from evalkit.adapters.base import ENTRY_POINT_GROUP as adapters
    from evalkit.adapters.base import SYSTEM_ENTRY_POINT_GROUP as systems
    from evalkit.scorers.base import ENTRY_POINT_GROUP as scorers

    assert (scorers, adapters) == ("evalkit.scorers", "evalkit.adapters")
    assert systems == "evalkit.systems"  # v0.2


def test_builtin_scorers_keep_their_names():
    from evalkit.scorers import discover

    assert {"label_correct", "citation_valid", "cost", "latency"} <= set(discover())


def test_agent_scorers_are_registered_builtins():  # v0.3 (B5.13)
    from evalkit.scorers import discover
    from evalkit.scorers.base import _BUILTIN

    assert {"answer_from_rows", "asked_back"} <= set(discover())
    assert {"answer_from_rows", "asked_back"} <= set(_BUILTIN)


def test_results_format_is_versioned():
    from evalkit.schemas import SCHEMA_VERSION

    major, _minor = SCHEMA_VERSION.split(".")
    assert major == "1"


def test_exit_codes_are_documented_and_stable():
    from evalkit.cli import EXIT_CODES

    for code in ("0", "1", "2", "3", "4"):
        assert f"  {code}  " in EXIT_CODES


def test_the_cli_keeps_its_subcommands_options_and_exit_codes():
    assert _api.cli_missing() == []


def test_the_cli_contract_lists_every_subcommand():
    from evalkit.cli import build_parser

    top = set(_api._subparsers(build_parser()))
    assert top == {path.split()[0] for path in CLI}


def test_the_cli_contract_would_catch_a_removed_option(monkeypatch):
    monkeypatch.setitem(_api.CLI, "report", {*CLI["report"], "--no-such-flag"})
    assert _api.cli_missing() == ["evalkit report --no-such-flag"]


def test_ctrl_c_exits_130(monkeypatch, capsys):
    from evalkit import cli

    def interrupt(args):
        raise KeyboardInterrupt

    parser = cli.build_parser()
    _api._subparsers(parser)["init"].set_defaults(func=interrupt)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    assert cli.main(["init"]) == 130
    assert "rerun the same command" in capsys.readouterr().err


def test_the_check_script_passes_on_the_source_tree():
    assert _api.missing() == [] and _api.cli_missing() == []


def test_heldout_module_is_public_with_its_command_line():
    """B5.14: `evalkit.heldout` is public and `python -m evalkit.heldout` is its entry."""
    import subprocess
    import sys

    assert "evalkit.heldout" in PUBLIC
    done = subprocess.run(
        [sys.executable, "-m", "evalkit.heldout", "--help"], capture_output=True, text=True
    )
    assert done.returncode == 0
    for command in ("manifest", "verify", "scan", "verify-log"):
        assert command in done.stdout
