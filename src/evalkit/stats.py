"""Bootstrap statistics for run summaries (Tech Spec §6, OWNER D-36, D-37).

Pure Python so the core install stays small. One seeded resample matrix over the run's cases is
drawn once and reused for every model and metric, so per-model intervals and paired
differences come from the same resamples and the whole summary is reproducible.
"""

from __future__ import annotations

import math
import random
from collections.abc import Hashable, Mapping, Sequence
from typing import Literal, TypeVar

from evalkit.schemas import Interval

__all__ = [  # public since 0.3.0
    "Resamples",
    "clopper_pearson",  # 0.3.0 (B5.3, GAT-07)
    "holm",
    "interval",
    "paired",
    "percentile_ms",
    "rate_wording",  # 0.3.0 (B5.3, GAT-07)
]

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


def ratio_interval(
    num: Sequence[float], den: Sequence[float], resamples: Resamples, alpha: float = ALPHA
) -> Interval:
    """Ratio of sums Σnum / Σden over cases (B5.12), with a percentile bootstrap that resamples
    whole cases, so each case's numerator and denominator move together. Draws whose
    denominator is zero have no ratio and are left out. Needs a positive total denominator."""
    if len(num) != resamples.n or len(den) != resamples.n:
        raise ValueError(f"expected {resamples.n} values (one per case)")
    for v in (*num, *den):
        if not math.isfinite(v):
            raise ValueError("values must be finite")
    total = math.fsum(den)
    if total <= 0:
        raise ValueError("a ratio needs a positive denominator")
    point = math.fsum(num) / total
    boot = []
    for row in resamples.rows:
        d = math.fsum(den[i] for i in row)
        if d > 0:
            boot.append(math.fsum(num[i] for i in row) / d)
    return _interval_from(point, boot or [point], alpha)


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


# ---- exact binomial intervals (B5.3, GAT-07, OWNER D-64) ---------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        step = d * c
        h *= step
        if abs(step - 1.0) < 1e-16:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) for a, b > 0."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(q: float, a: float, b: float) -> float:
    """The x in [0, 1] with I_x(a, b) = q, by bisection (I_x is increasing in x)."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if mid in (lo, hi):
            break
        if _betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _check_counts(k: int, n: int, alpha: float) -> None:
    for name, v in (("k", k), ("n", n)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"{name} must be an integer, got {type(v).__name__}")
    if n < 1:
        raise ValueError("an exact interval needs n >= 1")
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")


Sided = Literal["two", "upper", "lower"]


def clopper_pearson(k: int, n: int, alpha: float = ALPHA, sided: Sided = "two") -> Interval:
    """Exact (Clopper-Pearson) 1 - alpha interval for k successes in n trials, point k / n.

    `sided="two"` (the default, for displayed intervals; OWNER D-70) is the standard two-sided
    interval with alpha/2 in each tail for every k, k = 0 and k = n included:
    lower = B(alpha/2; k, n-k+1) (0 at k = 0), upper = B(1-alpha/2; k+1, n-k) (1 at k = n);
    at k = 0 the upper bound is 1 - (alpha/2)**(1/n) (n = 150: 0.0243).

    `sided="upper"` and `sided="lower"` are exact one-sided 1 - alpha bounds, the whole alpha in
    one tail, for every k (gates' `bound: upper|lower`, D-70): upper = B(1-alpha; k+1, n-k),
    which is 1 - alpha**(1/n) at k = 0 (n = 150: 0.0198; k = 1: 0.0312), with lower = 0; and
    lower = B(alpha; k, n-k+1), alpha**(1/n) at k = n, with upper = 1. The beta quantile is
    found by bisection on the regularized incomplete beta; k = 0 and k = n use closed forms."""
    _check_counts(k, n, alpha)
    if sided not in ("two", "upper", "lower"):
        raise ValueError(f"sided must be 'two', 'upper' or 'lower', got {sided!r}")
    tail = alpha / 2 if sided == "two" else alpha
    lo, hi = 0.0, 1.0
    if sided in ("two", "upper") and k < n:
        hi = 1.0 - tail ** (1.0 / n) if k == 0 else _beta_quantile(1 - tail, k + 1, n - k)
    if sided in ("two", "lower") and k > 0:
        lo = tail ** (1.0 / n) if k == n else _beta_quantile(tail, k, n - k + 1)
    point = k / n
    return Interval(point=point, lo=min(lo, point), hi=max(hi, point))


def _pct(x: float) -> str:
    return f"{x * 100:.3g}%"


def rate_wording(k: int, n: int, alpha: float = ALPHA) -> str:
    """How a report states k of n with its exact interval. Zero is never written as "0%".
    "Below X% at 95%" is a one-sided statement, so k = 0 uses the one-sided upper bound
    (`0 of 150, below 1.98% at 95%`) and k = n the one-sided lower bound (`150 of 150, above
    98% at 95%`); otherwise the two-sided interval: `1 of 150 (0.667%, 95% CI 0.0169%–3.65%)`."""
    level = f"{(1 - alpha) * 100:g}%"
    if k == 0:
        return f"0 of {n}, below {_pct(clopper_pearson(k, n, alpha, 'upper').hi)} at {level}"
    if k == n:
        return f"{n} of {n}, above {_pct(clopper_pearson(k, n, alpha, 'lower').lo)} at {level}"
    iv = clopper_pearson(k, n, alpha)
    return f"{k} of {n} ({_pct(iv.point)}, {level} CI {_pct(iv.lo)}–{_pct(iv.hi)})"
