"""Judge-vs-human agreement at any label unit (PUB-04, D-65). Public API since 0.3.0.

A `CalibrationPair` is one labelled unit (a claim, a citation or an answer) with the owner's
label, the judge's verdict and a cluster key: the group the bootstrap resamples. Units in one
cluster share a context and a writer (e.g. the claims or citations of one answer), so
`cluster_kappa` resamples clusters, not units, and does not overstate precision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from evalkit.schemas import Interval
from evalkit.stats import Resamples, _quantile

__all__ = ["AgreementTable", "CalibrationPair", "agreement_table", "cluster_kappa", "kappa_from"]


@dataclass(frozen=True)
class CalibrationPair:
    cluster: str  # what the bootstrap resamples, e.g. the answer a claim or citation is in
    human: bool  # the owner's label: supported?
    judge: bool  # the judge's verdict: supported?


@dataclass(frozen=True)
class _Pair:
    """The 0.2 shape (was `evalkit.pipeline.calibrate._Pair`), kept until 0.4.0: its `answer`
    is its cluster. Use `CalibrationPair`."""

    answer: str
    human: bool  # supported?
    judge: bool

    @property
    def cluster(self) -> str:
        return self.answer


class _Labelled(Protocol):
    @property
    def cluster(self) -> str: ...
    @property
    def human(self) -> bool: ...
    @property
    def judge(self) -> bool: ...


def _confusion(pairs: Sequence[_Labelled]) -> tuple[int, int, int, int]:
    tp = sum(p.human and p.judge for p in pairs)
    fn = sum(p.human and not p.judge for p in pairs)
    fp = sum(not p.human and p.judge for p in pairs)
    tn = sum(not p.human and not p.judge for p in pairs)
    return tp, fn, fp, tn


def kappa_from(tp: int, fn: int, fp: int, tn: int) -> float:
    """Cohen's kappa for two raters on a binary label. When chance agreement is total (both
    raters constant) kappa is undefined; it is reported as 1.0 if they agree, else 0.0."""
    n = tp + fn + fp + tn
    if n == 0:
        return 0.0
    po = (tp + tn) / n
    pe = ((tp + fn) * (tp + fp) + (fp + tn) * (fn + tn)) / (n * n)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def cluster_kappa(
    pairs: Sequence[_Labelled], *, seed: int, n_resamples: int = 10_000, alpha: float = 0.05
) -> Interval:
    """Kappa with a percentile bootstrap that resamples clusters, not units: units in one
    cluster (e.g. the claims of one answer) share a context and a writer, so treating them as
    independent would overstate precision."""
    if not pairs:
        raise ValueError("no labelled claims to compare")
    clusters = sorted({p.cluster for p in pairs})
    per: dict[str, list[int]] = {c: [0, 0, 0, 0] for c in clusters}
    for p in pairs:
        c = per[p.cluster]
        i = (0 if p.judge else 1) if p.human else (2 if p.judge else 3)
        c[i] += 1
    point = kappa_from(*_confusion(pairs))
    if len(clusters) < 2:
        return Interval(point=point, lo=point, hi=point)
    rs = Resamples(len(clusters), max(n_resamples, 100), seed=seed)
    counts = [per[c] for c in clusters]
    boot = []
    for row in rs.rows:
        tp = fn = fp = tn = 0
        for idx in row:
            c = counts[idx]
            tp += c[0]
            fn += c[1]
            fp += c[2]
            tn += c[3]
        boot.append(kappa_from(tp, fn, fp, tn))
    boot.sort()
    lo, hi = _quantile(boot, alpha / 2), _quantile(boot, 1 - alpha / 2)
    return Interval(point=point, lo=min(lo, point), hi=max(hi, point))


@dataclass(frozen=True)
class AgreementTable:
    """The confusion table of judge against human, and the rates read from it. A rate whose
    denominator is 0 is None, never 0."""

    tp: int  # human supported, judge supported
    fn: int  # human supported, judge refused
    fp: int  # human refused, judge supported
    tn: int  # human refused, judge refused
    n: int
    clusters: int
    agreement: float | None  # (tp + tn) / n
    strictness: float | None  # fn / (tp + fn): the judge refuses what the human supports
    leniency: float | None  # fp / (fp + tn): the judge supports what the human refuses
    kappa: float | None  # kappa_from(tp, fn, fp, tn)
    owner_constant: bool  # every human label is the same value: kappa is uninformative


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def agreement_table(pairs: Sequence[_Labelled]) -> AgreementTable:
    """Confusion counts, agreement, strictness, leniency and kappa for judge vs human.
    `owner_constant` is True when every human label is the same value: kappa is then 0 by
    construction (or 1.0 when the judge is constant and agrees), whatever the judge does, so
    it says nothing about the judge; read strictness or leniency instead."""
    tp, fn, fp, tn = _confusion(pairs)
    n = tp + fn + fp + tn
    return AgreementTable(
        tp=tp,
        fn=fn,
        fp=fp,
        tn=tn,
        n=n,
        clusters=len({p.cluster for p in pairs}),
        agreement=_ratio(tp + tn, n),
        strictness=_ratio(fn, tp + fn),
        leniency=_ratio(fp, fp + tn),
        kappa=kappa_from(tp, fn, fp, tn) if n else None,
        owner_constant=len({p.human for p in pairs}) == 1,
    )
