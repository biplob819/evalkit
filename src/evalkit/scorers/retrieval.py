"""Ranking metrics for retrieval (RET-01..03). Plain functions over ids."""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import Any

from evalkit.scorers.base import ScoreResult


def _credited(retrieved: Sequence[Hashable]) -> list[Hashable | None]:
    """Positions as the system returned them; a repeated id earns nothing (None) after its
    first occurrence, so duplicates waste slots instead of earning double credit."""
    seen: set[Hashable] = set()
    out: list[Hashable | None] = []
    for doc in retrieved:
        out.append(None if doc in seen else doc)
        seen.add(doc)
    return out


def _check_ids(retrieved: object, relevant: object) -> None:
    if isinstance(retrieved, str | bytes) or isinstance(relevant, str | bytes):
        raise TypeError("pass sequences of ids, not a single string")


def _check_k(k: int) -> None:
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError(f"k must be an integer >= 1, got {k!r}")


def recall_at_k(retrieved: Sequence[Hashable], relevant: Iterable[Hashable], k: int) -> ScoreResult:
    """Share of the relevant ids that appear in the first k returned positions."""
    _check_ids(retrieved, relevant)
    _check_k(k)
    rel = set(relevant)
    if not rel:
        raise ValueError("recall is undefined with no relevant ids")
    hits = [d for d in _credited(retrieved)[:k] if d is not None and d in rel]
    return ScoreResult(
        value=len(hits) / len(rel), details={"hits": len(hits), "relevant": len(rel), "k": k}
    )


def mrr(retrieved: Sequence[Hashable], relevant: Iterable[Hashable]) -> ScoreResult:
    """Reciprocal rank (1-based) of the first relevant id; 0.0 if none is retrieved."""
    _check_ids(retrieved, relevant)
    rel = set(relevant)
    for rank, doc in enumerate(_credited(retrieved), start=1):
        if doc is not None and doc in rel:
            return ScoreResult(value=1.0 / rank, details={"first_relevant_rank": rank})
    return ScoreResult(value=0.0, details={"first_relevant_rank": None})


def ndcg_at_k(
    retrieved: Sequence[Hashable],
    relevant: Iterable[Hashable],
    k: int,
    gains: Mapping[Any, float] | None = None,
) -> ScoreResult:
    """nDCG@k with log2 discount: DCG = sum(gain_i / log2(i + 1)) for ranks i = 1..k."""
    _check_ids(retrieved, relevant)
    _check_k(k)
    rel = set(relevant)
    if not rel:
        raise ValueError("nDCG is undefined with no relevant ids")
    if gains is not None and any(
        not math.isfinite(float(v)) or float(v) < 0 for v in gains.values()
    ):
        raise ValueError("gains must be finite and non-negative")
    # Gains for ids outside `relevant` are ignored: relevance is decided by `relevant`.
    g = {d: 1.0 for d in rel} if gains is None else {d: float(gains.get(d, 0.0)) for d in rel}
    if not any(g.values()):
        raise ValueError("nDCG is undefined when every relevant gain is zero")

    def dcg(values: list[float]) -> float:
        return sum(v / math.log2(i + 1) for i, v in enumerate(values, start=1))

    actual = dcg([0.0 if d is None else g.get(d, 0.0) for d in _credited(retrieved)[:k]])
    ideal = dcg(sorted(g.values(), reverse=True)[:k])
    value = actual / ideal if ideal > 0 else 0.0
    return ScoreResult(value=value, details={"dcg": actual, "idcg": ideal, "k": k})
