"""Task-declared scorers (v0.2, D-59): every name in `TaskConfig.scorers` outside the benchmark
set is a scorer from the registry (built-in or plugin) that runs once per record inside
`evalkit run`, and whose results reach records, the summary and the report."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from evalkit.schemas import Case, Claim, ClaimVerdict, Completion, TaskConfig, TaskScore
from evalkit.schemas.benchmark import Scores
from evalkit.scorers.base import Scorer, ScoreResult, get_scorer

# Computed by the benchmark pipeline itself (Generate, Validate, extract, judge, aggregate).
BENCHMARK_SCORERS = frozenset(
    {"label_correct", "citation_valid", "groundedness", "coverage", "cost", "latency"}
)


class TaskScorerError(ValueError):
    """A task scorer is unknown, raised, or returned something other than a ScoreResult."""


def task_scorer_names(task: TaskConfig) -> tuple[str, ...]:
    return tuple(s for s in task.scorers if s not in BENCHMARK_SCORERS)


def resolve_task_scorers(task: TaskConfig) -> tuple[Scorer, ...]:
    """Look up every task scorer before anything is spent; an unknown name is an error."""
    out = []
    for name in task_scorer_names(task):
        try:
            out.append(get_scorer(name))
        except KeyError:
            raise TaskScorerError(
                f"task {task.name!r} lists unknown scorer {name!r}: register it with @scorer or "
                "the `evalkit.scorers` entry-point group (docs/plugins.md), or remove it"
            ) from None
    return tuple(out)


def score_task(
    scorers: Sequence[Scorer],
    case: Case,
    completion: Completion,
    *,
    model: str,
    claims: tuple[Claim, ...],
    verdicts: tuple[ClaimVerdict, ...],
    scores: Scores,
) -> dict[str, TaskScore]:
    """Every task scorer on one record. A scorer that raises, returns something other than a
    ScoreResult or a non-finite value is a TaskScorerError naming the scorer and the case."""
    out: dict[str, TaskScore] = {}
    ctx: dict[str, Any] = {"model": model, "claims": claims, "verdicts": verdicts, "scores": scores}
    for s in scorers:
        where = f"task scorer {s.name!r} on case {case.id!r} ({model})"
        try:
            result = s(case, completion, **ctx)
        except Exception as exc:
            raise TaskScorerError(f"{where} failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(result, ScoreResult):
            raise TaskScorerError(f"{where} returned {type(result).__name__}, not a ScoreResult")
        if not math.isfinite(result.value):  # pragma: no cover - ScoreResult refuses these
            raise TaskScorerError(f"{where} returned a non-finite value")
        out[s.name] = TaskScore(value=result.value, version=s.version, details=result.details)
    return out
