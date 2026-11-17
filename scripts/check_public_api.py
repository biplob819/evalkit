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
        "SystemAdapter",  # v0.2
        "discover",
        "discover_systems",  # v0.2
        "get_adapter",
        "get_system",  # v0.2
        "register",
        "register_system",  # v0.2
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
        "SliceSummary",  # v0.2
        "Step",
        "TaskConfig",
        "TaskScore",  # v0.2
        "Threshold",
        "Trajectory",
    ],
    # v0.3 (B5.2, D-61 rule 2): internals two or more downstream projects import, and the names
    # that are part of their contract (specs/B5.2-public-api/spec.md).
    "evalkit.calibration": [
        "AgreementTable",
        "CalibrationPair",
        "agreement_table",
        "cluster_kappa",
        "kappa_from",
    ],
    "evalkit.cost": ["Price", "call_cost", "estimate_tokens", "load_prices"],
    "evalkit.errors": ["SchemaError"],
    "evalkit.identity": ["EXTRACTOR_VERSION", "JUDGE_PROMPT_HASH"],
    "evalkit.retry": ["with_retries"],
    "evalkit.scorers.faithfulness": [
        "FaithfulnessItem",
        "FaithfulnessResult",
        "JudgedClaim",
        "faithfulness",
    ],
    "evalkit.stats": ["Resamples", "holm", "interval", "paired", "percentile_ms"],
}

# v0.3 (B5.2, PUB-02/03): internal paths downstream projects import whose names now have a
# public home (old path -> public path). Paths in MOVED raise a DeprecationWarning naming the
# new path until 0.4.0; the others are where EvalKit defines the name and stay silent. The
# import audit reports every one as public-elsewhere.
PROMOTED: dict[str, str] = {
    "evalkit.pipeline.answer.SchemaError": "evalkit.errors.SchemaError",
    "evalkit.pipeline.calibrate._Pair": "evalkit.calibration.CalibrationPair",
    "evalkit.pipeline.calibrate.cluster_kappa": "evalkit.calibration.cluster_kappa",
    "evalkit.pipeline.calibrate.kappa_from": "evalkit.calibration.kappa_from",
    "evalkit.pipeline.extract.EXTRACTOR_VERSION": "evalkit.identity.EXTRACTOR_VERSION",
    "evalkit.pipeline.judge.JUDGE_PROMPT_HASH": "evalkit.identity.JUDGE_PROMPT_HASH",
}
MOVED: frozenset[str] = frozenset(
    {
        "evalkit.pipeline.calibrate._Pair",
        "evalkit.pipeline.calibrate.cluster_kappa",
        "evalkit.pipeline.calibrate.kappa_from",
    }
)

# CLI (docs/versioning.md): every subcommand, its options and positionals, and the exit codes.
CLI: dict[str, set[str]] = {
    "run": {
        "config",
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
    "agent-run": {
        "--agents",
        "--concurrency",
        "--dry-run",
        "--heldout",
        "--json",
        "--k",
        "--max-cost",
        "--no-heldout",
        "--out",
        "--seed",
        "--simulators",
        "--store",
        "--tasks",
    },
    "demo-cache": {"--samples", "--out", "--config", "--max-cost", "--dry-run", "--json"},
    "calibrate export": {"run_dir", "--force", "--json"},
    "calibrate label": {"run_dir", "--json", "--port", "--relabel"},
    "calibrate import": {"run_dir", "csv", "--json"},
    "calibrate score": {"run_dir", "--json"},
    "report": {"run_dir", "--example", "--out", "--publish", "--json"},
    "init": {"dir", "--force", "--json"},
    "doctor": {"--offline", "--store", "--config", "--json"},
}
EXIT_CODES = {"0", "1", "2", "3", "4", "130"}


def _subparsers(parser):
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def cli_missing() -> list[str]:
    """Contract entries the installed CLI no longer has."""
    import argparse

    from evalkit.cli import EXIT_CODES as HELP
    from evalkit.cli import build_parser

    out = []
    top = _subparsers(build_parser())
    for path, wanted in CLI.items():
        parser = None
        choices = top
        for word in path.split():
            parser = choices.get(word)
            if parser is None:
                break
            choices = _subparsers(parser)
        if parser is None:
            out.append(f"evalkit {path}")
            continue
        have = {
            s
            for a in parser._actions
            for s in (a.option_strings or [a.dest])
            if not isinstance(a, argparse._SubParsersAction)
        }
        out += [f"evalkit {path} {w}" for w in sorted(wanted - have)]
    out += [f"exit code {c}" for c in sorted(EXIT_CODES) if c != "130" and f"  {c}  " not in HELP]
    return out


def missing() -> list[str]:
    out = []
    for module, names in PUBLIC.items():
        mod = importlib.import_module(module)
        out += [f"{module}.{n}" for n in names if not hasattr(mod, n)]
    for new in PROMOTED.values():
        module, name = new.rsplit(".", 1)
        if name not in PUBLIC.get(module, ()):
            out.append(f"{new} (promoted, not public)")
    return out


if __name__ == "__main__":
    import evalkit

    gone = missing() + cli_missing()
    print(f"evalkit {evalkit.__version__} from {evalkit.__file__}")
    print("public API: OK" if not gone else f"public API: MISSING {gone}")
    sys.exit(1 if gone else 0)
