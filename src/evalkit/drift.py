"""Drift and calibration monitor (B5.7 LED-04, LED-05 defaults; spec §4; OWNER D-73).

`freeze_reference` profiles a reference batch into a hashed `drift-ref-1` file; `check` compares
a new batch with it and returns `drift-1`. Rows never leave the process: outputs carry edges,
category names, shares and aggregates only.

- PSI per feature, by fraud-review-bedrock's rule so its numbers reproduce (its MON-03): numeric
  edges are the reference's deciles (linear interpolation, numpy's default; repeated edges
  merged), bins `[e_i, e_{i+1})` with open ends; a constant reference gets 3 bins (below, equal,
  above); nulls and non-finite values are dropped (and reported as `missing_share`); each share
  is floored at 1e-4 (not renormalised); PSI = Σ (q − p)·ln(q / p). Categorical features get one
  bin per reference category plus `other`; pre-binned shares (a metrics-1 `*.feature_share`
  row per feature and bin) are compared as given. Levels: ok below 0.1, warn 0.1 to 0.25,
  alarm above 0.25, overridable per feature.
- Calibration (binary outcomes): Brier, ECE over 10 equal-width bins (`floor(p·10 + 1e-12)`,
  p = 1 in the last bin) and reliability bins, with 95% intervals; deltas new − reference
  (Brier by MOVER, ECE by independent parametric bootstraps of each side's bins, each bin's
  observed rate by MOVER of Clopper-Pearson intervals), Holm-adjusted across the block: a delta
  is `changed` when its adjusted interval excludes 0, `warn` when changed in the worsening
  direction, `alarm` when also its whole interval lies beyond the tolerance (Brier 0.01, ECE
  0.02, bin 0.10; OWNER D-73). Bins that do not align (quantile bins) are not compared.
"""

from __future__ import annotations

import bisect
import csv
import datetime as dt
import hashlib
import json
import math
import os
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "CAL_BINS",
    "DRIFT_SCHEMA",
    "PSI_ALARM",
    "PSI_EPSILON",
    "PSI_WARN",
    "REF_SCHEMA",
    "TOLERANCES",
    "DriftError",
    "DriftReport",
    "calibration",
    "calibration_drift",
    "check",
    "freeze_reference",
    "load_config",
    "load_reference",
    "psi",
    "psi_from_shares",
    "psi_level",
    "to_metrics",
]

PSI_EPSILON = 1e-4  # fraud-review-bedrock's PSI_EPSILON
PSI_WARN = 0.1  # OWNER defaults via fraud-review-bedrock MON-03 (D-65)
PSI_ALARM = 0.25
PSI_BINS = 10
CAL_BINS = 10
SMALL_BATCH = 0.05  # half the warn level: the no-drift PSI expectation that marks a small batch
TOLERANCES: dict[str, float] = {"brier": 0.01, "ece": 0.02, "bin": 0.10}  # OWNER D-73
REF_SCHEMA = "drift-ref-1"
DRIFT_SCHEMA = "drift-1"
DEFAULT_SEED = 7
DEFAULT_RESAMPLES = 10_000
ALPHA = 0.05
Z975 = 1.959963984540054
OTHER = "other"
_MISSING_TEXT = frozenset({"", "na", "n/a", "nan", "null", "none"})
RATE_REL_TOL, RATE_ABS_TOL = 1e-6, 5e-7 + 1e-12  # metrics-1's rate tolerance

Level = Literal["ok", "warn", "alarm"]


class DriftError(ValueError):
    """Bad drift input (exit 2). Messages name files, columns and row numbers, never values."""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stamp(at: str | None = None) -> str:
    if at is not None:
        try:
            when = dt.datetime.fromisoformat(at)
        except ValueError:
            raise DriftError("created_at must be an ISO 8601 time with a zone") from None
        if when.tzinfo is None:
            raise DriftError("created_at must carry a zone")
    elif os.environ.get("SOURCE_DATE_EPOCH", "").strip():
        try:
            when = dt.datetime.fromtimestamp(int(os.environ["SOURCE_DATE_EPOCH"]), dt.UTC)
        except (ValueError, OverflowError, OSError):
            raise DriftError("SOURCE_DATE_EPOCH must be whole seconds since 1970") from None
    else:
        when = dt.datetime.now(dt.UTC)
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- PSI ----------------------------------------------------------------------------------------


def _lerp(a: float, b: float, t: float) -> float:
    """numpy's `_lerp`: a + (b − a)·t, computed from b for t >= 0.5 (bit-identical to it)."""
    diff = b - a
    return b - diff * (1 - t) if t >= 0.5 else a + diff * t


def _quantile(xs: Sequence[float], q: float) -> float:
    """numpy's default (linear) quantile of a sorted sequence."""
    n = len(xs)
    vi = (n - 1) * q
    lo = math.floor(vi)
    if vi >= n - 1:
        return xs[-1]
    return _lerp(xs[lo], xs[lo + 1], vi - lo)


def _finite(values: Sequence[Any]) -> list[float]:
    out = []
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        x = float(v)
        if math.isfinite(x):
            out.append(x)
    return out


def numeric_edges(reference: Sequence[float], bins: int = PSI_BINS) -> list[float]:
    """Inner bin edges from a reference sample (finite values): its quantiles at 1/bins ...
    (bins−1)/bins, repeated edges merged; a constant reference gets edges [v, next float above
    v], so 3 bins: below, equal, above."""
    if bins < 2:
        raise DriftError("PSI needs at least 2 bins")
    xs = sorted(reference)
    if not xs:
        raise DriftError("PSI needs values in the reference")
    if xs[0] == xs[-1]:
        return [xs[0], math.nextafter(xs[0], math.inf)]
    step = 1.0 / bins
    return sorted(set(_quantile(xs, i * step) for i in range(1, bins)))


def _numeric_shares(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    counts = [0] * (len(edges) + 1)
    for x in values:
        counts[bisect.bisect_right(edges, x)] += 1
    return [c / len(values) for c in counts]


def psi_from_shares(
    reference: Sequence[float], current: Sequence[float], *, epsilon: float = PSI_EPSILON
) -> float:
    """Σ (q − p)·ln(q / p) over bins, each share floored at `epsilon` (not renormalised)."""
    if len(reference) != len(current) or not reference:
        raise DriftError("PSI needs the same non-empty bins on both sides")
    terms = []
    for p, q in zip(reference, current, strict=True):
        p, q = max(p, epsilon), max(q, epsilon)
        terms.append((q - p) * math.log(q / p))
    return _pairwise_sum(terms)


def _pairwise_sum(a: Sequence[float]) -> float:
    """numpy's pairwise summation (`np.sum` of a float64 array), so a PSI computed here and by
    a numpy implementation of the same rule agree to the last bit."""
    n = len(a)
    if n < 8:
        res = -0.0
        for x in a:
            res += x
        return res
    if n <= 128:
        r = list(a[:8])
        i = 8
        while i < n - (n % 8):
            for j in range(8):
                r[j] += a[i + j]
            i += 8
        res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]))
        while i < n:
            res += a[i]
            i += 1
        return res
    half = n // 2
    half -= half % 8
    return _pairwise_sum(a[:half]) + _pairwise_sum(a[half:])


def psi(
    reference: Sequence[Any],
    current: Sequence[Any],
    *,
    bins: int = PSI_BINS,
    epsilon: float = PSI_EPSILON,
) -> float:
    """PSI of `current` against `reference` on the reference's decile bins: fraud-review-
    bedrock's `psi` (its MON-03), so its numbers reproduce. None and non-finite values are
    dropped; an empty sample raises DriftError."""
    ref, cur = _finite(reference), _finite(current)
    if not ref or not cur:
        raise DriftError("PSI needs values in both samples")
    edges = numeric_edges(ref, bins)
    return psi_from_shares(
        _numeric_shares(ref, edges), _numeric_shares(cur, edges), epsilon=epsilon
    )


def psi_level(value: float, *, warn: float = PSI_WARN, alarm: float = PSI_ALARM) -> Level:
    """ok below `warn`, warn for warn <= PSI <= alarm, alarm above `alarm` (unrounded)."""
    if value < warn:
        return "ok"
    return "warn" if value <= alarm else "alarm"


# ---- reading batches ----------------------------------------------------------------------------


def _read_rows(path: Path) -> tuple[list[Mapping[str, Any]], bool]:
    """(rows, from_csv) of a CSV (header row) or JSON Lines file (or a JSON list of objects)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise DriftError(f"cannot read {path.name}: {type(exc).__name__}") from None
    if path.suffix.lower() == ".csv":
        return list(csv.DictReader(text.splitlines())), True
    rows: list[Mapping[str, Any]] = []
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            raise DriftError(f"{path.name} is not valid JSON") from None
        items = data if isinstance(data, list) else []
        for n, row in enumerate(items, start=1):
            if not isinstance(row, Mapping):
                raise DriftError(f"{path.name}: item {n} is not an object")
            rows.append(row)
        return rows, False
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise DriftError(f"{path.name}: line {n} is not JSON") from None
        if not isinstance(row, Mapping):
            raise DriftError(f"{path.name}: line {n} is not an object")
        rows.append(row)
    return rows, False


def _is_missing(v: Any, from_csv: bool) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() in _MISSING_TEXT if from_csv else not v.strip()
    if isinstance(v, float):
        return not math.isfinite(v)
    return False


def _as_number(v: Any, from_csv: bool) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if from_csv and isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _as_category(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v if isinstance(v, str) else json.dumps(v)


def _column(rows: Sequence[Mapping[str, Any]], name: str, where: str) -> list[Any]:
    if rows and any(name not in r for r in rows):
        raise DriftError(f"{where}: column {name!r} is missing from some rows")
    return [r.get(name) for r in rows]


def _profile_feature(
    values: Sequence[Any], from_csv: bool, *, categorical: bool, name: str, where: str
) -> dict[str, Any]:
    present = [v for v in values if not _is_missing(v, from_csv)]
    missing_share = (len(values) - len(present)) / len(values) if values else 0.0
    if not present:
        raise DriftError(f"{where}: feature {name!r} has no values")
    nums = [_as_number(v, from_csv) for v in present]
    if not categorical and all(x is not None for x in nums):
        xs = [x for x in nums if x is not None and math.isfinite(x)]
        edges = numeric_edges(xs)
        return {
            "kind": "numeric",
            "edges": edges,
            "shares": _numeric_shares(xs, edges),
            "n": len(xs),
            "missing_share": missing_share,
        }
    cats = [_as_category(v) for v in present]
    names = sorted(set(cats))
    counts = {c: 0 for c in names}
    for c in cats:
        counts[c] += 1
    return {
        "kind": "categorical",
        "categories": names,
        "shares": [counts[c] / len(cats) for c in names] + [0.0],
        "n": len(cats),
        "missing_share": missing_share,
    }


def _new_shares(
    profile: Mapping[str, Any], values: Sequence[Any], from_csv: bool, name: str, where: str
) -> tuple[list[float], int, float]:
    present = [v for v in values if not _is_missing(v, from_csv)]
    missing_share = (len(values) - len(present)) / len(values) if values else 0.0
    if profile["kind"] == "numeric":
        xs = []
        for n, v in enumerate(present, start=1):
            x = _as_number(v, from_csv)
            if x is None:
                raise DriftError(f"{where}: feature {name!r} has a non-numeric value ({n})")
            if math.isfinite(x):
                xs.append(x)
        if not xs:
            raise DriftError(f"{where}: feature {name!r} has no values")
        return _numeric_shares(xs, profile["edges"]), len(xs), missing_share
    cats = profile["categories"]
    index = {c: i for i, c in enumerate(cats)}
    counts = [0] * (len(cats) + 1)
    for v in present:
        counts[index.get(_as_category(v), len(cats))] += 1
    if not present:
        raise DriftError(f"{where}: feature {name!r} has no values")
    return [c / len(present) for c in counts], len(present), missing_share


# ---- calibration --------------------------------------------------------------------------------


def _cal_bin(p: float) -> int:
    """respectful-offers' `ece` rule: floor(p·10 + 1e-12), clipped (p = 1 in the last bin)."""
    return min(CAL_BINS - 1, max(0, math.floor(p * CAL_BINS + 1e-12)))


def _label01(v: Any, from_csv: bool, where: str) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int | float) and v in (0, 1):
        return int(v)
    if isinstance(v, str) and v.strip().lower() in ("0", "1", "0.0", "1.0", "true", "false"):
        return 1 if v.strip().lower() in ("1", "1.0", "true") else 0
    raise DriftError(f"{where}: an outcome must be 0 or 1 (true or false)")


def _percentile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _ci(point: float, boot: Sequence[float]) -> tuple[float, float]:
    lo, hi = _percentile(boot, ALPHA / 2), _percentile(boot, 1 - ALPHA / 2)
    return min(lo, point), max(hi, point)


def calibration(
    p: Sequence[float],
    y: Sequence[int],
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> dict[str, Any]:
    """Brier and ECE with seeded percentile bootstrap intervals over rows, and the 10
    equal-width reliability bins with each observed rate's Clopper-Pearson interval."""
    from evalkit.stats import clopper_pearson

    if len(p) != len(y) or not p:
        raise DriftError("calibration needs as many outcomes as probabilities, at least one")
    if resamples < 100:
        raise DriftError("use at least 100 resamples")
    ps = [float(v) for v in p]
    if any(not (0.0 <= v <= 1.0) for v in ps):
        raise DriftError("a probability must lie in [0, 1]")
    ys = [int(v) for v in y]
    n = len(ps)
    sq = [(a - b) ** 2 for a, b in zip(ps, ys, strict=True)]
    bins_ = [_cal_bin(v) for v in ps]
    cnt, sp, sy = [0] * CAL_BINS, [0.0] * CAL_BINS, [0] * CAL_BINS
    for b, a, k in zip(bins_, ps, ys, strict=True):
        cnt[b] += 1
        sp[b] += a
        sy[b] += k
    brier = math.fsum(sq) / n
    ece = math.fsum(abs(sy[b] - sp[b]) for b in range(CAL_BINS) if cnt[b]) / n
    rng = random.Random(f"evalkit-drift-calibration:{seed}:{n}")
    population = range(n)
    boot_b, boot_e = [], []
    for _ in range(resamples):
        c2, p2, y2 = [0] * CAL_BINS, [0.0] * CAL_BINS, [0] * CAL_BINS
        s = 0.0
        for i in rng.choices(population, k=n):
            b = bins_[i]
            c2[b] += 1
            p2[b] += ps[i]
            y2[b] += ys[i]
            s += sq[i]
        boot_b.append(s / n)
        boot_e.append(sum(abs(y2[b] - p2[b]) for b in range(CAL_BINS) if c2[b]) / n)
    b_lo, b_hi = _ci(brier, boot_b)
    e_lo, e_hi = _ci(ece, boot_e)
    method = f"percentile bootstrap over rows (seed {seed}, {resamples} resamples)"
    bins = []
    for b in range(CAL_BINS):
        row: dict[str, Any] = {
            "bin": b,
            "lo": b / CAL_BINS,
            "hi": (b + 1) / CAL_BINS,
            "n": cnt[b],
            "k": sy[b],
            "mean_pred": sp[b] / cnt[b] if cnt[b] else None,
            "obs_rate": sy[b] / cnt[b] if cnt[b] else None,
        }
        if cnt[b]:
            iv = clopper_pearson(sy[b], cnt[b])
            row["obs_low"], row["obs_high"] = iv.lo, iv.hi
        bins.append(row)
    return {
        "source": "rows",
        "n": n,
        "binning": "equal-width",
        "brier": {"value": brier, "ci_low": b_lo, "ci_high": b_hi, "ci_method": method},
        "ece": {"value": ece, "ci_low": e_lo, "ci_high": e_hi, "ci_method": method},
        "bins": bins,
    }


def _calibration_from_metrics(summary: Any, prefix: str) -> dict[str, Any]:
    from evalkit.gates import canonical

    rows = {(m.name, tuple(sorted(m.labels))): m for m in summary.metrics}
    brier = rows.get((f"{prefix}.brier", ()))
    ece = rows.get((f"{prefix}.ece", ()))
    if brier is None:
        raise DriftError(f"the metrics file has no {prefix}.brier row")
    out: dict[str, Any] = {"source": "metrics", "binning": None, "prefix": prefix}

    def fig(m: Any) -> dict[str, Any] | None:
        if m is None:
            return None
        return {
            "value": float(m.value),
            "ci_low": None if m.ci_low is None else float(m.ci_low),
            "ci_high": None if m.ci_high is None else float(m.ci_high),
            "ci_method": m.ci_method,
        }

    out["brier"], out["ece"] = fig(brier), fig(ece)
    out["n"] = brier.n
    pred = {
        canonical(m.labels["bin"]): m
        for m in summary.metrics
        if m.name == f"{prefix}.reliability.predicted" and set(m.labels) == {"bin"}
    }
    obs = {
        canonical(m.labels["bin"]): m
        for m in summary.metrics
        if m.name == f"{prefix}.reliability.observed" and set(m.labels) == {"bin"}
    }
    bins = []
    for key in sorted(set(pred) & set(obs), key=lambda k: (not k.lstrip("-").isdigit(), _k(k))):
        o, pr = obs[key], pred[key]
        n = o.n if o.n is not None else pr.n
        if n is None or n < 1:
            raise DriftError(f"{prefix}.reliability bin {key}: n is required")
        bins.append({"bin": key, "n": n, "mean_pred": float(pr.value), "obs_rate": float(o.value)})
    if set(pred) ^ set(obs):
        raise DriftError(f"{prefix}.reliability: predicted and observed name different bins")
    out["bins"] = bins
    return out


def _k(key: str) -> Any:
    try:
        return int(key)
    except ValueError:
        return key


def _mover(
    d: float, lo_new: float, hi_new: float, new: float, lo_old: float, hi_old: float, old: float
) -> tuple[float, float]:
    lo = d - math.sqrt((new - lo_new) ** 2 + (hi_old - old) ** 2)
    hi = d + math.sqrt((hi_new - new) ** 2 + (old - lo_old) ** 2)
    return min(lo, d), max(hi, d)


def _normal_p(d: float, lo: float, hi: float) -> float:
    if d == 0:
        return 1.0
    half = d - lo if d > 0 else hi - d
    if half <= 0:
        return 0.0
    return min(1.0, math.erfc(abs(d) / (half / Z975) / math.sqrt(2)))


def _holm(ps: Mapping[str, float]) -> dict[str, float]:
    order = sorted(ps, key=lambda k: (ps[k], k))
    m, running, out = len(order), 0.0, {}
    for rank, k in enumerate(order):
        running = max(running, min(1.0, (m - rank) * ps[k]))
        out[k] = running
    return out


def _binomial(rng: random.Random, n: int, p: float) -> int:
    """A Binomial(n, p) draw: inversion (BINV) when n·min(p, 1−p) < 30, else the rounded normal
    approximation, clamped to [0, n]. Deterministic under the seeded generator."""
    if n <= 0 or p <= 0:
        return 0
    if p >= 1:
        return n
    flip = p > 0.5
    q_ = 1 - p if flip else p
    if n * q_ >= 30:
        k = round(rng.gauss(n * q_, math.sqrt(n * q_ * (1 - q_))))
        k = min(max(k, 0), n)
    else:
        q = 1 - q_
        s = q_ / q
        a = (n + 1) * s
        r = q**n
        u = rng.random()
        k = 0
        while u > r and k < n:
            u -= r
            k += 1
            r *= a / k - s
    return n - k if flip else k


def _ece_from_bins(bins: Sequence[Mapping[str, Any]], obs: Sequence[float]) -> float:
    total = sum(int(b["n"]) for b in bins)
    return math.fsum(
        int(b["n"]) / total * abs(o - float(b["mean_pred"])) for b, o in zip(bins, obs, strict=True)
    )


def _ece_deviations(
    bins: Sequence[Mapping[str, Any]], rng: random.Random, resamples: int
) -> list[float]:
    """Parametric bootstrap of one side's ECE from its bins (observed ~ Binomial(n_b, obs_b) /
    n_b, predicted fixed), as deviations from the ECE of the bins themselves."""
    used = [b for b in bins if int(b["n"]) > 0 and b.get("obs_rate") is not None]
    base = _ece_from_bins(used, [float(b["obs_rate"]) for b in used])
    out = []
    for _ in range(resamples):
        obs = [_binomial(rng, int(b["n"]), float(b["obs_rate"])) / int(b["n"]) for b in used]
        out.append(_ece_from_bins(used, obs) - base)
    return out


def _counts(b: Mapping[str, Any], where: str) -> tuple[int, int]:
    n = int(b["n"])
    if "k" in b and b["k"] is not None:
        return int(b["k"]), n
    obs = float(b["obs_rate"])
    k = round(obs * n)
    tol = min(RATE_ABS_TOL, 0.25 / n)
    if not math.isclose(obs, k / n, rel_tol=RATE_REL_TOL, abs_tol=tol):
        raise DriftError(f"{where}: bin {b['bin']}: observed rate is not a count over n")
    return k, n


def _delta_row(
    name: str, d: float, lo: float | None, hi: float | None, *, method: str, tol: float
) -> dict[str, Any]:
    return {
        "name": name,
        "delta": d,
        "ci_low": lo,
        "ci_high": hi,
        "method": method,
        "tolerance": tol,
        "p_value": None if lo is None or hi is None else _normal_p(d, lo, hi),
    }


def calibration_drift(
    reference: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    tolerances: Mapping[str, float] | None = None,
    binning: str | None = None,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> dict[str, Any]:
    """Brier, ECE and reliability-bin deltas new − reference, each with a 95% interval and a
    status (`ok`, `changed`, `warn`, `alarm`, or `no_interval`); see the module docstring.
    `binning` declares the bins of a side read from aggregates (`equal-width` aligns them);
    otherwise bins are reported `bins_not_aligned` and only Brier and ECE are compared."""
    from evalkit.stats import clopper_pearson

    tol = {**TOLERANCES, **(tolerances or {})}
    rows: list[dict[str, Any]] = []
    rb, nb = reference.get("brier"), new.get("brier")
    if rb and nb:
        d = nb["value"] - rb["value"]
        if None not in (rb["ci_low"], rb["ci_high"], nb["ci_low"], nb["ci_high"]):
            lo, hi = _mover(
                d,
                nb["ci_low"],
                nb["ci_high"],
                nb["value"],
                rb["ci_low"],
                rb["ci_high"],
                rb["value"],
            )
            rows.append(_delta_row("brier", d, lo, hi, method="MOVER (unpaired)", tol=tol["brier"]))
        else:
            rows.append(_delta_row("brier", d, None, None, method="no interval", tol=tol["brier"]))
    re_, ne_ = reference.get("ece"), new.get("ece")
    r_bins = [b for b in reference.get("bins") or [] if int(b["n"]) > 0]
    n_bins = [b for b in new.get("bins") or [] if int(b["n"]) > 0]
    if re_ and ne_:
        d = ne_["value"] - re_["value"]
        if r_bins and n_bins:
            rng = random.Random(f"evalkit-drift-ece:{seed}")
            dev_r = _ece_deviations(r_bins, rng, resamples)
            dev_n = _ece_deviations(n_bins, rng, resamples)
            diffs = [a - b for a, b in zip(dev_n, dev_r, strict=True)]
            lo, hi = d + _percentile(diffs, ALPHA / 2), d + _percentile(diffs, 1 - ALPHA / 2)
            lo, hi = min(lo, d), max(hi, d)
            method = f"parametric bootstrap of each side's bins (seed {seed}, {resamples})"
            rows.append(_delta_row("ece", d, lo, hi, method=method, tol=tol["ece"]))
        else:
            rows.append(_delta_row("ece", d, None, None, method="no bins", tol=tol["ece"]))

    def aligned(side: Mapping[str, Any]) -> bool:
        declared = side.get("binning") or binning
        return declared == "equal-width"

    r_keys = [str(b["bin"]) for b in r_bins]
    n_keys = [str(b["bin"]) for b in n_bins]
    bins_status = "compared"
    if not (r_bins and n_bins):
        bins_status = "no_bins"
    elif not (aligned(reference) and aligned(new)):
        bins_status = "bins_not_aligned"
    else:
        by_r = {str(b["bin"]): b for b in r_bins}
        by_n = {str(b["bin"]): b for b in n_bins}
        for key in [k for k in n_keys if k in by_r]:
            rk, rn = _counts(by_r[key], "reference")
            nk, nn = _counts(by_n[key], "new batch")
            ro, no = rk / rn, nk / nn
            ri, ni = clopper_pearson(rk, rn), clopper_pearson(nk, nn)
            d = no - ro
            lo, hi = _mover(d, ni.lo, ni.hi, no, ri.lo, ri.hi, ro)
            row = _delta_row(
                f"bin {key}", d, lo, hi, method="MOVER of Clopper-Pearson", tol=tol["bin"]
            )
            gap_r = ro - float(by_r[key]["mean_pred"])
            gap_n = no - float(by_n[key]["mean_pred"])
            row.update(bin=key, gap_ref=gap_r, gap_new=gap_n, n_ref=rn, n_new=nn)
            rows.append(row)
        missing = sorted(set(r_keys) ^ set(n_keys))
        if missing:
            bins_status = "compared (bins on one side only: " + ", ".join(missing) + ")"
    ps = {r["name"]: r["p_value"] for r in rows if r["p_value"] is not None}
    adjusted = _holm(ps)
    worst: Level = "ok"
    for r in rows:
        if r["p_value"] is None:
            r.update(p_holm=None, changed=False, worse=None, status="no_interval")
            continue
        r["p_holm"] = adjusted[r["name"]]
        r["changed"] = r["p_holm"] < ALPHA
        if "bin" in r:
            worse = abs(r["gap_new"]) > abs(r["gap_ref"])
            sign = 1 if r["gap_new"] > 0 else -1
        else:
            worse = r["delta"] > 0
            sign = 1
        r["worse"] = worse
        beyond = r["ci_low"] > r["tolerance"] if sign > 0 else r["ci_high"] < -r["tolerance"]
        if not r["changed"]:
            r["status"] = "ok"
        elif not worse:
            r["status"] = "changed"
        elif beyond:
            r["status"] = "alarm"
        else:
            r["status"] = "warn"
        if r["status"] == "alarm" or (r["status"] == "warn" and worst == "ok"):
            worst = r["status"]
    return {
        "deltas": rows,
        "bins": bins_status if bins_status != "bins_not_aligned" else "not available",
        "bins_status": bins_status,
        "tolerances": {k: tol[k] for k in sorted(tol)},
        "holm_family": len(ps),
        "level": worst,
    }


# ---- the reference profile ----------------------------------------------------------------------


def _with_hash(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "profile_sha256": hashlib.sha256(_canonical(body).encode()).hexdigest()}


def _shares_from_metrics(summary: Any, where: str) -> dict[str, dict[str, Any]]:
    from evalkit.gates import canonical

    out: dict[str, dict[str, Any]] = {}
    for m in summary.metrics:
        if not m.name.endswith(".feature_share") or set(m.labels) != {"feature", "bin"}:
            continue
        if m.unit != "rate":
            raise DriftError(f"{where}: {m.name} must be a rate")
        f = canonical(m.labels["feature"])
        entry = out.setdefault(f, {"metric": m.name, "bins": {}, "n": None})
        entry["bins"][canonical(m.labels["bin"])] = float(m.value)
        if m.n is not None:
            entry["n"] = max(entry["n"] or 0, m.n)
    feats: dict[str, dict[str, Any]] = {}
    for f, e in sorted(out.items()):
        keys = sorted(e["bins"], key=lambda k: (not k.lstrip("-").isdigit(), _k(k)))
        shares = [e["bins"][k] for k in keys]
        if abs(math.fsum(shares) - 1.0) > 0.01:
            raise DriftError(f"{where}: the shares of feature {f!r} do not sum to 1")
        feats[f] = {
            "kind": "binned",
            "metric": e["metric"],
            "bins": keys,
            "shares": shares,
            "n": e["n"],
            "missing_share": None,
        }
    return feats


def _is_metrics_file(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("schema_version") == "metrics-1"


def _load_summary(path: Path) -> Any:
    from evalkit.metrics import MetricsError, load_metrics

    try:
        return load_metrics(path)
    except MetricsError as exc:
        raise DriftError("; ".join(exc.problems)) from None


def _guess_prefix(summary: Any) -> str | None:
    found = sorted({m.name[: -len(".brier")] for m in summary.metrics if m.name.endswith(".brier")})
    return found[0] if len(found) == 1 else None


def freeze_reference(
    source: str | Path,
    *,
    features: Sequence[str] = (),
    prob: str | None = None,
    label: str | None = None,
    categorical: Sequence[str] = (),
    calibration_prefix: str | None = None,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    created_at: str | None = None,
) -> dict[str, Any]:
    """A frozen, hashed reference (`drift-ref-1`) from a CSV / JSON Lines file of rows (the
    named `features`, and `prob` with `label` for calibration) or from a metrics-1 file of
    aggregates (`*.feature_share` rows, and the `<prefix>.brier`, `.ece` and
    `.reliability.*` rows). Holds edges, categories, shares and aggregates, never rows."""
    p = Path(source)
    if not p.is_file():
        raise DriftError(f"{p}: no such file")
    body: dict[str, Any] = {
        "schema": REF_SCHEMA,
        "created_at": _stamp(created_at),
        "source_sha256": _sha_bytes(p.read_bytes()),
    }
    if _is_metrics_file(p):
        summary = _load_summary(p)
        body["source_kind"] = "metrics"
        feats = _shares_from_metrics(summary, p.name)
        if features:
            missing = [f for f in features if f not in feats]
            if missing:
                raise DriftError(f"{p.name}: no feature_share rows for {missing[0]!r}")
            feats = {f: feats[f] for f in features}
        body["features"] = feats
        prefix = calibration_prefix or _guess_prefix(summary)
        if prefix is not None:
            body["calibration"] = _calibration_from_metrics(summary, prefix)
        if not feats and "calibration" not in body:
            raise DriftError(f"{p.name}: no feature_share or calibration rows")
        return _with_hash(body)
    rows, from_csv = _read_rows(p)
    if not rows:
        raise DriftError(f"{p.name}: the reference batch is empty")
    body["source_kind"] = "rows"
    body["n_rows"] = len(rows)
    feats = {}
    for f in features:
        feats[f] = _profile_feature(
            _column(rows, f, p.name), from_csv, categorical=f in categorical, name=f, where=p.name
        )
    body["features"] = feats
    if (prob is None) != (label is None):
        raise DriftError("calibration needs both --prob and --label")
    if prob is not None and label is not None:
        cal = _calibration_rows(rows, prob, label, from_csv, p.name, seed, resamples)
        body["calibration"] = cal
    if not feats and "calibration" not in body:
        raise DriftError("name features to profile (and/or --prob with --label)")
    return _with_hash(body)


def _calibration_rows(
    rows: Sequence[Mapping[str, Any]],
    prob: str,
    label: str,
    from_csv: bool,
    where: str,
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    ps, ys, skipped = [], [], 0
    for pv, yv in zip(_column(rows, prob, where), _column(rows, label, where), strict=True):
        if _is_missing(pv, from_csv) or _is_missing(yv, from_csv):
            skipped += 1
            continue
        x = _as_number(pv, from_csv)
        if x is None:
            raise DriftError(f"{where}: column {prob!r} holds a non-numeric value")
        ps.append(x)
        ys.append(_label01(yv, from_csv, where))
    if not ps:
        raise DriftError(f"{where}: no rows with both {prob!r} and {label!r}")
    cal = calibration(ps, ys, seed=seed, resamples=resamples)
    cal["columns"] = {"prob": prob, "label": label}
    cal["missing"] = skipped
    return cal


def load_reference(reference: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """A `drift-ref-1` profile, refused (DriftError) when it does not match its own hash."""
    if isinstance(reference, Mapping):
        data: Any = dict(reference)
    else:
        try:
            data = json.loads(Path(reference).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise DriftError(f"{Path(reference).name} is unreadable or not JSON") from None
    if not isinstance(data, dict) or data.get("schema") != REF_SCHEMA:
        raise DriftError(f"not a {REF_SCHEMA} reference")
    body = {k: v for k, v in data.items() if k != "profile_sha256"}
    if _with_hash(body)["profile_sha256"] != data.get("profile_sha256"):
        raise DriftError("the reference does not match its profile_sha256 (edited after freezing)")
    return data


# ---- configuration ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftConfig:
    psi_warn: float = PSI_WARN
    psi_alarm: float = PSI_ALARM
    features: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    tolerances: Mapping[str, float] = field(default_factory=lambda: dict(TOLERANCES))
    binning: str | None = None

    def levels(self, feature: str) -> tuple[float, float]:
        return self.features.get(feature, (self.psi_warn, self.psi_alarm))


def _pos(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v <= 0:
        raise DriftError(f"drift config: {name} must be a positive number")
    return float(v)


def load_config(path: str | Path | Mapping[str, Any] | None) -> DriftConfig:
    """A drift config (YAML or JSON): `psi_warn`, `psi_alarm`, per-feature overrides under
    `features: {name: {psi_warn, psi_alarm}}`, and `calibration: {brier_tolerance,
    ece_tolerance, bin_tolerance, binning}`. Unknown keys are refused."""
    if path is None:
        return DriftConfig()
    if isinstance(path, Mapping):
        data: Any = path
    else:
        import yaml

        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            raise DriftError(f"drift config {Path(path).name} is unreadable") from None
    if not isinstance(data, Mapping):
        raise DriftError("drift config: a mapping")
    extra = set(data) - {"psi_warn", "psi_alarm", "features", "calibration"}
    if extra:
        raise DriftError(f"drift config: unknown key {sorted(extra)[0]!r}")
    warn = _pos(data.get("psi_warn", PSI_WARN), "psi_warn")
    alarm = _pos(data.get("psi_alarm", PSI_ALARM), "psi_alarm")
    if warn > alarm:
        raise DriftError("drift config: psi_warn must not exceed psi_alarm")
    feats: dict[str, tuple[float, float]] = {}
    for name, spec in (data.get("features") or {}).items():
        if not isinstance(spec, Mapping) or set(spec) - {"psi_warn", "psi_alarm"}:
            raise DriftError(f"drift config: features.{name} takes psi_warn and psi_alarm")
        w = _pos(spec.get("psi_warn", warn), f"features.{name}.psi_warn")
        a = _pos(spec.get("psi_alarm", alarm), f"features.{name}.psi_alarm")
        if w > a:
            raise DriftError(f"drift config: features.{name}: psi_warn exceeds psi_alarm")
        feats[str(name)] = (w, a)
    cal = data.get("calibration") or {}
    if not isinstance(cal, Mapping):
        raise DriftError("drift config: calibration is a mapping")
    allowed = {"brier_tolerance", "ece_tolerance", "bin_tolerance", "binning"}
    if set(cal) - allowed:
        raise DriftError(f"drift config: unknown calibration key {sorted(set(cal) - allowed)[0]!r}")
    tol = dict(TOLERANCES)
    for key, short in (
        ("brier_tolerance", "brier"),
        ("ece_tolerance", "ece"),
        ("bin_tolerance", "bin"),
    ):
        if key in cal:
            tol[short] = _pos(cal[key], f"calibration.{key}")
    binning = cal.get("binning")
    if binning not in (None, "equal-width", "quantile", "unknown"):
        raise DriftError("drift config: calibration.binning is equal-width, quantile or unknown")
    return DriftConfig(warn, alarm, feats, tol, binning)


# ---- the check ----------------------------------------------------------------------------------


@dataclass
class DriftReport:
    """`drift-1`: aggregates only. Exit 1 on any alarm (PSI or calibration), else 0."""

    reference: dict[str, Any]
    new: dict[str, Any]
    settings: dict[str, Any]
    features: list[dict[str, Any]]
    calibration: dict[str, Any] | None
    exit_code: int

    def as_dict(self) -> dict[str, Any]:
        levels = [f["level"] for f in self.features]
        cal = [d["status"] for d in (self.calibration or {}).get("deltas", [])]
        alarms = levels.count("alarm") + cal.count("alarm")
        warns = levels.count("warn") + cal.count("warn")
        return {
            "schema": DRIFT_SCHEMA,
            "exit_code": self.exit_code,
            "verdict": "alarm" if alarms else "warn" if warns else "ok",
            "alarms": alarms,
            "warns": warns,
            "reference": self.reference,
            "new": self.new,
            "settings": self.settings,
            "features": self.features,
            "calibration": self.calibration,
        }


def _feature_row(
    name: str,
    ref: Mapping[str, Any],
    shares: Sequence[float],
    n_new: int | None,
    missing_new: float | None,
    cfg: DriftConfig,
) -> dict[str, Any]:
    value = psi_from_shares(ref["shares"], shares)
    warn, alarm = cfg.levels(name)
    bins = len(ref["shares"])
    n_ref = ref.get("n")
    expected = (bins - 1) * (1 / n_ref + 1 / n_new) if n_ref and n_new else None
    return {
        "feature": name,
        "kind": ref["kind"],
        "psi": value,
        "psi_shown": f"{value:.4f}",
        "level": psi_level(value, warn=warn, alarm=alarm),
        "psi_warn": warn,
        "psi_alarm": alarm,
        "bins": bins,
        "n_ref": n_ref,
        "n_new": n_new,
        "expected_psi": expected,
        "small_batch": bool(expected is not None and expected >= SMALL_BATCH),
        "missing_share_ref": ref.get("missing_share"),
        "missing_share_new": missing_new,
    }


def check(
    reference: str | Path | Mapping[str, Any],
    new: str | Path,
    *,
    config: str | Path | Mapping[str, Any] | DriftConfig | None = None,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> DriftReport:
    """Compare a new batch (rows, or a metrics-1 file of aggregates) with a frozen reference.
    Raises DriftError (exit 2) on bad input: an edited reference, an empty batch, a missing
    feature or column, bins that differ for a pre-binned feature."""
    ref = load_reference(reference)
    cfg = config if isinstance(config, DriftConfig) else load_config(config)
    p = Path(new)
    if not p.is_file():
        raise DriftError(f"{p}: no such file")
    new_meta: dict[str, Any] = {"source_sha256": _sha_bytes(p.read_bytes())}
    features: list[dict[str, Any]] = []
    new_cal: dict[str, Any] | None = None
    ref_cal = ref.get("calibration")
    if _is_metrics_file(p):
        summary = _load_summary(p)
        new_meta["kind"] = "metrics"
        got = _shares_from_metrics(summary, p.name)
        for name, prof in sorted(ref["features"].items()):
            if name not in got:
                raise DriftError(f"{p.name}: no feature_share rows for feature {name!r}")
            if prof["kind"] != "binned" or got[name]["bins"] != prof["bins"]:
                raise DriftError(f"feature {name!r}: the bins differ from the reference's")
            features.append(
                _feature_row(name, prof, got[name]["shares"], got[name]["n"], None, cfg)
            )
        if ref_cal is not None:
            prefix = ref_cal.get("prefix") or _guess_prefix(summary)
            if prefix is None:
                raise DriftError(f"{p.name}: no calibration rows to compare")
            new_cal = _calibration_from_metrics(summary, prefix)
    else:
        rows, from_csv = _read_rows(p)
        if not rows:
            raise DriftError(f"{p.name}: the new batch is empty")
        new_meta["kind"] = "rows"
        new_meta["n_rows"] = len(rows)
        for name, prof in sorted(ref["features"].items()):
            if prof["kind"] == "binned":
                raise DriftError(f"feature {name!r} was pre-binned: give a metrics-1 file")
            shares, n_new, missing = _new_shares(
                prof, _column(rows, name, p.name), from_csv, name, p.name
            )
            features.append(_feature_row(name, prof, shares, n_new, missing, cfg))
        if ref_cal is not None:
            cols = ref_cal.get("columns")
            if not cols:
                raise DriftError(
                    "the reference's calibration came from aggregates: give a metrics-1 file"
                )
            new_cal = _calibration_rows(
                rows, cols["prob"], cols["label"], from_csv, p.name, seed, resamples
            )
    cal_out = None
    if ref_cal is not None and new_cal is not None:
        drift = calibration_drift(
            ref_cal,
            new_cal,
            tolerances=cfg.tolerances,
            binning=cfg.binning,
            seed=seed,
            resamples=resamples,
        )
        cal_out = {
            "reference": _cal_summary(ref_cal),
            "new": _cal_summary(new_cal),
            **drift,
        }
    alarm = any(f["level"] == "alarm" for f in features) or (
        cal_out is not None and any(d["status"] == "alarm" for d in cal_out["deltas"])
    )
    return DriftReport(
        reference={
            "profile_sha256": ref["profile_sha256"],
            "source_sha256": ref["source_sha256"],
            "created_at": ref["created_at"],
        },
        new=new_meta,
        settings={
            "psi_warn": cfg.psi_warn,
            "psi_alarm": cfg.psi_alarm,
            "psi_epsilon": PSI_EPSILON,
            "feature_levels": {k: list(v) for k, v in sorted(cfg.features.items())},
            "seed": seed,
            "resamples": resamples,
        },
        features=features,
        calibration=cal_out,
        exit_code=1 if alarm else 0,
    )


def _cal_summary(cal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": cal.get("source"),
        "n": cal.get("n"),
        "binning": cal.get("binning"),
        "brier": cal.get("brier"),
        "ece": cal.get("ece"),
        "bins": cal.get("bins"),
    }


def to_metrics(
    report: DriftReport,
    *,
    project: str,
    run_id: str,
    prefix: str = "drift",
    created_at: str | None = None,
) -> dict[str, Any]:
    """The check's figures as a metrics-1 document, so they can be gated and added to the run
    history: `<prefix>.psi` per feature, and the new batch's Brier and ECE with their deltas."""
    from evalkit.metrics import MetricsError, validate_metrics

    rows: list[dict[str, Any]] = []
    for f in report.features:
        row: dict[str, Any] = {
            "name": f"{prefix}.psi",
            "unit": "ratio",
            "value": f["psi"],
            "labels": {"feature": f["feature"]},
        }
        if f["n_new"] is not None:
            row["n"] = f["n_new"]
        rows.append(row)
    cal = report.calibration
    if cal is not None:
        for key in ("brier", "ece"):
            fig = cal["new"].get(key)
            if fig:
                row = {"name": f"{prefix}.{key}", "unit": "ratio", "value": fig["value"]}
                if fig.get("ci_low") is not None and fig.get("ci_high") is not None:
                    row.update(
                        ci_low=fig["ci_low"], ci_high=fig["ci_high"], ci_method=fig["ci_method"]
                    )
                if cal["new"].get("n"):
                    row["n"] = cal["new"]["n"]
                rows.append(row)
        for d in cal["deltas"]:
            if d["name"] not in ("brier", "ece"):
                continue
            row = {"name": f"{prefix}.{d['name']}_delta", "unit": "ratio", "value": d["delta"]}
            if d["ci_low"] is not None:
                row.update(ci_low=d["ci_low"], ci_high=d["ci_high"], ci_method=d["method"])
            rows.append(row)
    doc = {
        "schema_version": "metrics-1",
        "project": project,
        "run_id": run_id,
        "created_at": _stamp(created_at),
        "metrics": rows,
    }
    try:
        validate_metrics(doc)
    except MetricsError as exc:
        raise DriftError("emitted metrics: " + "; ".join(exc.problems)) from None
    return doc
