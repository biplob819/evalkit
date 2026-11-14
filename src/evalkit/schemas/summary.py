"""Summary schemas (SCH-02): what `summary.json` holds for a run."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from evalkit.schemas._base import (
    Fraction,
    FrozenMap,
    Model,
    NonBlank,
    NonNegFloat,
    NonNegInt,
    Timestamp,
    Versioned,
)
from evalkit.schemas.benchmark import ERROR_TYPES


class Interval(Model):
    """A point estimate with its 95% confidence interval. All bounds are finite."""

    point: float
    lo: float
    hi: float

    @model_validator(mode="after")
    def _ordered(self) -> Interval:
        if not self.lo <= self.point <= self.hi:
            raise ValueError(f"interval must satisfy lo <= point <= hi, got {self}")
        return self


class ModelSummary(Model):
    model: NonBlank
    family: Literal["anthropic", "openai", "other"]
    n: NonNegInt
    n_errors: FrozenMap[NonNegInt]  # by error type
    headline: FrozenMap[Interval]  # correct_and_grounded, per judge
    accuracy: Interval
    groundedness_mean: FrozenMap[Interval]
    citation_validity: Interval
    coverage: Interval
    cost_per_100_usd: NonNegFloat
    cost_per_correct_usd: NonNegFloat | None  # None when nothing was correct
    latency_p50_ms: NonNegInt
    latency_p95_ms: NonNegInt
    passes_threshold: bool
    failed_thresholds: tuple[str, ...]

    @model_validator(mode="after")
    def _consistency(self) -> ModelSummary:
        if self.passes_threshold == bool(self.failed_thresholds):
            raise ValueError(
                "passes_threshold must be true exactly when failed_thresholds is empty"
            )
        unknown = set(self.n_errors) - set(ERROR_TYPES)
        if unknown:
            raise ValueError(f"unknown error types in n_errors: {sorted(unknown)}")
        if sum(self.n_errors.values()) > self.n:
            raise ValueError("n_errors cannot exceed n")
        if self.latency_p95_ms < self.latency_p50_ms:
            raise ValueError("latency_p95_ms cannot be below latency_p50_ms")
        if set(self.headline) != set(self.groundedness_mean):
            raise ValueError("headline and groundedness_mean must name the same judges")
        return self


class JudgeAgreement(Model):
    judge: NonBlank
    kappa: Interval
    agreement: Fraction
    confusion: tuple[tuple[NonNegInt, ...], ...]  # [[TP, FN], [FP, TN]], "supported" positive
    by_family: FrozenMap[Interval]

    @model_validator(mode="after")
    def _square(self) -> JudgeAgreement:
        if len(self.confusion) != 2 or any(len(r) != 2 for r in self.confusion):
            raise ValueError("confusion must be a 2x2 matrix")
        return self


class RunSummary(Versioned):
    run_id: NonBlank
    task: NonBlank
    config_hash: NonBlank
    judge_mode: Literal["dual", "single"]
    calibration_status: Literal["calibrated", "uncalibrated"]
    models: tuple[ModelSummary, ...]
    judges: tuple[JudgeAgreement, ...]  # agreement with held-out labels; empty when uncalibrated
    self_preference: FrozenMap[FrozenMap[Interval]]  # judge -> generator family -> groundedness
    extraction_error_rate: Fraction | None = None
    judging_status: Literal["pending", "complete"]
    started_at: Timestamp
    finished_at: Timestamp | None = None

    @model_validator(mode="after")
    def _stamps_are_honest(self) -> RunSummary:
        n_judges = 2 if self.judge_mode == "dual" else 1
        names = [j.judge for j in self.judges]
        if len(names) != len(set(names)):
            raise ValueError("each judge may appear only once in judges")
        if self.calibration_status == "calibrated" and len(names) != n_judges:
            raise ValueError(
                f"a calibrated {self.judge_mode}-judge run needs {n_judges} judge "
                f"agreement entries, got {len(names)}"
            )
        if self.calibration_status == "uncalibrated" and names:
            raise ValueError("an uncalibrated run cannot report judge agreement")
        if self.judge_mode == "single" and self.self_preference:
            raise ValueError("self-preference needs two judges; a single-judge run has none")
        for m in self.models:
            if len(m.headline) != n_judges:
                raise ValueError(
                    f"{m.model}: a {self.judge_mode}-judge run needs {n_judges} headline "
                    f"entries, got {len(m.headline)}"
                )
        if (self.judging_status == "complete") != (self.finished_at is not None):
            raise ValueError("finished_at is set exactly when judging_status is complete")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be before started_at")
        return self
