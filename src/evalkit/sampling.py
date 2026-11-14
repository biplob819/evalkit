"""Seeded, label-stratified sampling (DAT-03)."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from evalkit.schemas import Case


@dataclass(frozen=True)
class Sample:
    cases: tuple[Case, ...]
    label_mix: dict[str, int]  # realised count per reference label


def _allocate(counts: dict[str, int], n: int) -> dict[str, int]:
    """Largest-remainder allocation of n across strata, proportional to their sizes."""
    total = sum(counts.values())
    exact = {k: n * c / total for k, c in counts.items()}
    alloc = {k: int(v) for k, v in exact.items()}
    by_remainder = sorted(counts, key=lambda k: (-(exact[k] - alloc[k]), k))
    for k in by_remainder[: n - sum(alloc.values())]:
        alloc[k] += 1
    return alloc


def stratified_sample(cases: Sequence[Case], n: int, seed: int) -> Sample:
    """Draw n cases so each reference label keeps its population share (±1 case).

    Same cases, n and seed always give the same sample in the same order. Cases without a
    reference label form their own stratum.
    """
    if not 0 < n <= len(cases):
        raise ValueError(f"n must be between 1 and {len(cases)}, got {n}")
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case ids must be unique to sample deterministically")
    strata: dict[str, list[Case]] = defaultdict(list)
    for c in sorted(cases, key=lambda c: c.id):
        strata[c.reference_label or "<unlabelled>"].append(c)
    alloc = _allocate({k: len(v) for k, v in strata.items()}, n)
    rng = random.Random(seed)
    picked: list[Case] = []
    for label in sorted(strata):
        picked.extend(rng.sample(strata[label], alloc[label]))
    rng.shuffle(picked)
    mix = Counter(c.reference_label or "<unlabelled>" for c in picked)
    full_mix = {label: mix.get(label, 0) for label in sorted(strata)}  # zero-count labels kept
    return Sample(cases=tuple(picked), label_mix=full_mix)
