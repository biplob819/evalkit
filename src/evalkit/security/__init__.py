"""Agent security eval kit (B5.11, D-64, D-70): attack cases, k trials and pass^k,
stopping-layer attribution, a model-free scripted attacker, and held-out attacks reported only
as split-level aggregates.

Lifted from assay-pipeline's `attack/` package (MIT, same author) and generalised: any tools,
any layer vocabulary, no SQL or database. Builds on `evalkit.scorers.attack_outcome` (C3.4),
`evalkit.heldout` (B5.14), `evalkit.stats` and `evalkit.metrics` (B5.2, B5.3). See
docs/security.md.
"""

from __future__ import annotations

from .cases import (
    OWASP_CATEGORIES,
    AttackCase,
    AttackSuite,
    ToolCall,
    case_key,
    check_cases,
    load_attack_cases,
    load_suite,
)
from .cli import main
from .errors import SecurityError
from .layers import (
    DEFAULT_ERROR_PREFIXES,
    DEFAULT_LAYERS,
    SUCCESS_LAYERS,
    LayerSet,
    attribute,
)
from .scripted import (
    ScriptedResult,
    ToolReply,
    ToolSurface,
    read_scripted,
    run_scripted,
    scripted_attack,
)
from .summary import (
    SUMMARY_SCHEMA,
    render_compare,
    render_report,
    summarise,
    to_metrics,
    write_outputs,
)
from .trials import (
    DEFAULT_K,
    AttackSystem,
    Observation,
    TrialResult,
    read_trials,
    run_attacks,
    score_trial,
    trial_seed,
)

__all__ = [
    "DEFAULT_ERROR_PREFIXES",
    "DEFAULT_K",
    "DEFAULT_LAYERS",
    "OWASP_CATEGORIES",
    "SUCCESS_LAYERS",
    "SUMMARY_SCHEMA",
    "AttackCase",
    "AttackSuite",
    "AttackSystem",
    "LayerSet",
    "Observation",
    "ScriptedResult",
    "SecurityError",
    "ToolCall",
    "ToolReply",
    "ToolSurface",
    "TrialResult",
    "attribute",
    "case_key",
    "check_cases",
    "load_attack_cases",
    "load_suite",
    "main",
    "read_scripted",
    "read_trials",
    "render_compare",
    "render_report",
    "run_attacks",
    "run_scripted",
    "score_trial",
    "scripted_attack",
    "summarise",
    "to_metrics",
    "trial_seed",
    "write_outputs",
]
