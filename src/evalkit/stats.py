"""Bootstrap statistics for run summaries (Tech Spec §6, OWNER D-36, D-37).

Pure Python so the core install stays small. One seeded resample matrix over the run's cases is
drawn once and reused for every model and metric, so per-model intervals and paired
differences come from the same resamples and the whole summary is reproducible.
"""

from __future__ import annotations

import math
import random
from collections.abc import Hashable, Mapping, Sequence
from typing import TypeVar

from evalkit.schemas import Interval

ALPHA = 0.05
K = TypeVar("K", bound=Hashable)


class Resamples:
    """`n_resamples` bootstrap draws (with replacement) of `n` case positions, seeded."""

    def __init__(self, n: int, n_resamples: int = 10_000, seed: int = 0) -> None:
        if n < 1:
            raise ValueError("cannot bootstrap zero cases")
        if n_resamples < 100:
            raise ValueError("use at least 100 resamples")
        rng = random.Random(f"evalkit-bootstrap:{seed}:{n}")  # str seeds are stable across runs
        population = range(n)
        self.n = n
        self.rows: tuple[tuple[int, ...], ...] = tuple(
            tuple(rng.choices(population, k=n)) for _ in range(n_resamples)
        )

    def __len__(self) -> int:
        return len(self.rows)

    def means(self, values: Sequence[float]) -> list[float]:
        if len(values) != self.n:
            raise ValueError(f"expected {self.n} values (one per case), got {len(values)}")
        get = values.__getitem__
        n = self.n
        return [math.fsum(map(get, row)) / n for row in self.rows]


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default) of an already sorted sequence."""
    pos = q * (len(sorted_xs) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def _interval_from(point: float, boot: Sequence[float], alpha: float) -> Interval:
    s = sorted(boot)
    lo, hi = _quantile(s, alpha / 2), _quantile(s, 1 - alpha / 2)
    # Percentile intervals can sit just off the point in very skewed samples; keep lo<=point<=hi.
    return Interval(point=point, lo=min(lo, point), hi=max(hi, point))


def interval_from(point: float, boot: Sequence[float], alpha: float = ALPHA) -> Interval:
    """Percentile interval for a point estimate from bootstrap draws made elsewhere (e.g. the
    agent track's two-level bootstrap)."""
    if not boot:
        raise ValueError("no bootstrap draws")
    return _interval_from(point, boot, alpha)


def _finite_mean(values: Sequence[float]) -> float:
    for v in values:
        if not math.isfinite(v):
            raise ValueError("values must be finite")
    return math.fsum(values) / len(values)


def interval(values: Sequence[float], resamples: Resamples, alpha: float = ALPHA) -> Interval:
    """Point mean with a percentile bootstrap (1 - alpha) interval."""
    point = _finite_mean(values)
    return _interval_from(point, resamples.means(values), alpha)


def paired(
    a: Sequence[float], b: Sequence[float], resamples: Resamples, alpha: float = ALPHA
) -> tuple[Interval, float]:
    """Unadjusted interval for mean(a - b) over shared cases, and a two-sided bootstrap p-value
    for a difference of zero, with the +1 correction so it is never exactly 0."""
    if len(a) != len(b):
        raise ValueError("paired comparison needs the same cases for both models")
    diff = [x - y for x, y in zip(a, b, strict=True)]
    point = _finite_mean(diff)
    boot = resamples.means(diff)
    big = len(boot) + 1
    le = (sum(d <= 0 for d in boot) + 1) / big
    ge = (sum(d >= 0 for d in boot) + 1) / big
    return _interval_from(point, boot, alpha), min(1.0, 2 * min(le, ge))


def holm(pvalues: Mapping[K, float], alpha: float = ALPHA) -> dict[K, bool]:
    """Holm step-down: which hypotheses are rejected at family-wise error `alpha`."""
    order = sorted(pvalues, key=lambda k: (pvalues[k], str(k)))
    m = len(order)
    out = dict.fromkeys(pvalues, False)
    for rank, key in enumerate(order):
        if pvalues[key] > alpha / (m - rank):
            break
        out[key] = True
    return out


def percentile_ms(values: Sequence[int], q: float) -> int:
    """Nearest-rank percentile of latencies in ms (0 for an empty list)."""
    if not values:
        return 0
    xs = sorted(values)
    rank = max(1, math.ceil(q * len(xs)))
    return xs[rank - 1]
