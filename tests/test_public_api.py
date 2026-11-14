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
    from evalkit.scorers.base import ENTRY_POINT_GROUP as scorers

    assert (scorers, adapters) == ("evalkit.scorers", "evalkit.adapters")


def test_builtin_scorers_keep_their_names():
    from evalkit.scorers import discover

    assert {"label_correct", "citation_valid", "cost", "latency"} <= set(discover())


def test_results_format_is_versioned():
    from evalkit.schemas import SCHEMA_VERSION

    major, _minor = SCHEMA_VERSION.split(".")
    assert major == "1"


def test_exit_codes_are_documented_and_stable():
    from evalkit.cli import EXIT_CODES

    for code in ("0", "1", "2", "3", "4"):
        assert f"  {code}  " in EXIT_CODES


def test_the_cli_keeps_its_subcommands_and_options():
    import argparse

    from evalkit.cli import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == set(CLI)
    for name, options in CLI.items():
        have = {s for a in sub.choices[name]._actions for s in a.option_strings}
        assert options <= have, f"evalkit {name} lost {sorted(options - have)}"


def test_the_check_script_passes_on_the_source_tree():
    assert _api.missing() == []
