"""Scores that need no model (DET-01, DET-02, DET-03). Pure functions of (case, completion)."""

from __future__ import annotations

from typing import Any

from evalkit.schemas import Case, Completion
from evalkit.scorers.base import ScoreResult, normalise_label, scorer


@scorer("label_correct")
def label_correct(case: Case, completion: Completion, **_: Any) -> ScoreResult:
    """1.0 when the answer's label matches the reference label (NFKC, trimmed, case-folded).
    Errors, missing answers and unlabelled cases score 0.0: errors count as wrong (NFR5)."""
    if completion.error is not None or completion.answer is None:
        return ScoreResult(value=0.0, details={"reason": completion.error or "no_answer"})
    if case.reference_label is None:
        return ScoreResult(value=0.0, details={"reason": "unlabelled_case"})
    ok = normalise_label(completion.answer.label) == normalise_label(case.reference_label)
    return ScoreResult(
        value=1.0 if ok else 0.0,
        details={"predicted": completion.answer.label, "reference": case.reference_label},
    )


@scorer("citation_valid")
def citation_valid(case: Case, completion: Completion, **_: Any) -> ScoreResult:
    """Share of distinct cited sentence numbers that exist (1..len(context)). Citing nothing
    scores 0.0, because an answer with no citations cannot be checked against its source."""
    if completion.error is not None or completion.answer is None:
        return ScoreResult(value=0.0, details={"reason": completion.error or "no_answer"})
    cited = sorted(set(completion.answer.citations))
    if not cited:
        return ScoreResult(value=0.0, details={"reason": "no_citations"})
    n = len(case.context)
    invalid = [c for c in cited if not 1 <= c <= n]
    return ScoreResult(
        value=(len(cited) - len(invalid)) / len(cited),
        details={"invalid": invalid, "n_sentences": n, "n_cited": len(cited)},
    )


@scorer("cost")
def cost(case: Case, completion: Completion, **_: Any) -> ScoreResult:
    """What the call cost in USD, errored or not."""
    return ScoreResult(value=completion.cost_usd)


@scorer("latency")
def latency(case: Case, completion: Completion, **_: Any) -> ScoreResult:
    """Wall-clock milliseconds for the call, errored or not."""
    return ScoreResult(value=float(completion.latency_ms))
