"""The public API contract (docs/versioning.md), checked against whichever `evalkit` is
importable: the source tree in tests, the published package in the release smoke job.

    python scripts/check_public_api.py      # exit 0 when every public name is present
"""

from __future__ import annotations

import importlib
import sys

PUBLIC: dict[str, list[str]] = {
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

# CLI: every subcommand and the options the docs promise (docs/versioning.md).
CLI: dict[str, set[str]] = {
    "run": {
        "--task",
        "--n",
        "--out",
        "--store",
        "--max-cost",
        "--dry-run",
        "--wait",
        "--live-judges",
        "--json",
    },
    "agent-run": {"--json"},
    "demo-cache": {"--samples", "--out", "--config", "--max-cost", "--dry-run", "--json"},
    "calibrate": set(),
    "report": {"--example", "--out"},
    "init": {"--force"},
    "doctor": {"--offline", "--config", "--json"},
}


def missing() -> list[str]:
    out = []
    for module, names in PUBLIC.items():
        mod = importlib.import_module(module)
        out += [f"{module}.{n}" for n in names if not hasattr(mod, n)]
    return out


if __name__ == "__main__":
    import evalkit

    gone = missing()
    print(f"evalkit {evalkit.__version__} from {evalkit.__file__}")
    print("public API: OK" if not gone else f"public API: MISSING {gone}")
    sys.exit(1 if gone else 0)
