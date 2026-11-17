"""Clarification (CLR-01): does the agent ask back on ambiguous questions, and only there?

Ported from agentic-analytics `src/agentic_analytics/scoring/clarify.py` (SCR-03, MIT licence,
Copyright (c) 2026 Biplob Chakraborty, the same author). Asking back on an answer-expected
question is a false clarification, so asking back on everything cannot look good: the two rates
are reported side by side, each with an exact binomial (Clopper-Pearson) interval, which keeps
its coverage at the small counts these sets have (20 ambiguous questions) and does not collapse
to a point at 0/n or n/n as a bootstrap does.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Literal

from evalkit.schemas import Case, Completion, Interval
from evalkit.schemas._base import Model, NonNegInt
from evalkit.scorers.base import ScoreResult, normalise_label, scorer

CLARIFY_LABEL = "clarify"


class ClarificationRates(Model):
    """Ask rate on ambiguous questions and false clarification rate on clear ones."""

    n_ambiguous: NonNegInt
    n_clear: NonNegInt
    asked: NonNegInt  # ambiguous questions the agent asked back on
    false_clarifications: NonNegInt  # clear questions the agent asked back on
    ask_rate: Interval | None  # None when there are no ambiguous questions
    false_clarification_rate: Interval | None  # None when there are no clear questions
    method: Literal["clopper-pearson"] = "clopper-pearson"


def _log_pmf(i: int, n: int, p: float) -> float:
    return (
        math.lgamma(n + 1)
        - math.lgamma(i + 1)
        - math.lgamma(n - i + 1)
        + i * math.log(p)
        + (n - i) * math.log1p(-p)
    )


def _cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), 0 < p < 1, summed in log space."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return min(1.0, math.fsum(math.exp(_log_pmf(i, n, p)) for i in range(k + 1)))


def _solve(f: Any, target: float) -> float:
    """The p in (0, 1) where the decreasing function f(p) equals target, by bisection."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if mid in (lo, hi):
            break
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _clopper_pearson(k: int, n: int, alpha: float) -> Interval:
    point = k / n
    half = alpha / 2
    # P(X >= k | lo) = alpha/2 and P(X <= k | hi) = alpha/2; closed forms at the edges
    if k == 0:
        lo, hi = 0.0, 1 - half ** (1 / n)
    elif k == n:
        lo, hi = half ** (1 / n), 1.0
    else:
        lo = _solve(lambda p: _cdf(k - 1, n, p), 1 - half)
        hi = _solve(lambda p: _cdf(k, n, p), half)
    return Interval(point=point, lo=min(lo, point), hi=max(hi, point))


def clarification_rates(
    expected_clarify: Sequence[bool], asked: Sequence[bool], *, alpha: float = 0.05
) -> ClarificationRates:
    """`expected_clarify[i]` says question i is ambiguous (asking back is right); `asked[i]`
    says the agent asked back. Returns both rates with (1 - alpha) Clopper-Pearson intervals."""
    if len(expected_clarify) != len(asked):
        raise ValueError("expected_clarify and asked must have one entry per question")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")
    for v in (*expected_clarify, *asked):
        if not isinstance(v, bool):
            raise ValueError("expected_clarify and asked hold booleans")
    n_amb = sum(expected_clarify)
    n_clear = len(asked) - n_amb
    hit = sum(a for e, a in zip(expected_clarify, asked, strict=True) if e)
    false = sum(a for e, a in zip(expected_clarify, asked, strict=True) if not e)
    return ClarificationRates(
        n_ambiguous=n_amb,
        n_clear=n_clear,
        asked=hit,
        false_clarifications=false,
        ask_rate=_clopper_pearson(hit, n_amb, alpha) if n_amb else None,
        false_clarification_rate=_clopper_pearson(false, n_clear, alpha) if n_clear else None,
    )


@scorer("asked_back")
def _asked_back_task(case: Case, completion: Completion, **_: Any) -> ScoreResult:
    """Task scorer: 1.0 when the answer's label is the clarify label (`case.meta["clarify_label"]`,
    default "clarify"), else 0.0. Slice a run by `expected` ("clarify" or "answer", from
    `case.meta["expected"]`) to get the ask rate and the false clarification rate."""
    raw_label = case.meta.get("clarify_label", CLARIFY_LABEL)
    target = normalise_label(raw_label if isinstance(raw_label, str) else CLARIFY_LABEL)
    raw = case.meta.get("expected")
    if raw is None and case.reference_label is not None:
        raw = CLARIFY_LABEL if normalise_label(case.reference_label) == target else "answer"
    expected = raw if raw in (CLARIFY_LABEL, "answer") else None
    asked = completion.answer is not None and normalise_label(completion.answer.label) == target
    details: dict[str, Any] = {
        "asked": asked,
        "expected": expected,
        "false_clarification": asked and expected == "answer",
    }
    if completion.answer is None:
        details["reason"] = completion.error or "no_answer"
    return ScoreResult(value=1.0 if asked else 0.0, details=details)
