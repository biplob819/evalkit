"""OSS-08: the public API contract (docs/versioning.md). Removing or renaming any of these is
a breaking change; this test makes it a deliberate one."""

from __future__ import annotations

import importlib

import pytest

PUBLIC = {
    "evalkit": ["__version__"],
    "evalkit.scorers": [
        "ScoreResult",
        "Scorer",
        "attack_outcome",
        "citation_valid",
        "cost",
        "discover",
        "get_scorer",
        "label_correct",
        "latency",
        "mrr",
        "ndcg_at_k",
        "recall_at_k",
        "scorer",
        "sql_match",
    ],
    "evalkit.adapters": [
        "ALLOWED_PARAMS",
        "BatchRequest",
        "BatchResult",
        "CompletionRequest",
        "ModelAdapter",
        "RawCompletion",
        "discover",
        "get_adapter",
        "register",
    ],
    "evalkit.schemas": [
        "SCHEMA_VERSION",
        "AgentScore",
        "AgentTask",
        "Answer",
        "AttackBlocked",
        "CalibrationLabel",
        "CalibrationReport",
        "Case",
        "Claim",
        "ClaimVerdict",
        "Completion",
        "GoldOutcome",
        "Interval",
        "JudgeAgreement",
        "ModelSummary",
        "PairComparison",
        "Record",
        "ResultSet",
        "RunConfig",
        "RunSummary",
        "Scores",
        "Step",
        "TaskConfig",
        "Threshold",
        "Trajectory",
    ],
}


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
