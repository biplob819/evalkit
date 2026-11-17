"""Summary schemas (SCH-02): what `summary.json` holds for a run."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from evalkit.schemas._base import (
    Fraction,
    FrozenDict,
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
    latency_p50_ms: NonNegInt | None  # over calls that reached the provider; None if none did
    latency_p95_ms: NonNegInt | None
    passes_threshold: bool
    failed_thresholds: tuple[str, ...]
    # v0.2 (D-59): task scorer -> mean over all cases with its 95% bootstrap interval.
    task_scores: FrozenMap[Interval] = Field(default_factory=FrozenDict)
    # B5.12, per judge (tasks that list `citation_support`): supporting cited sentences over all
    # cited sentences, and the share of answers citing a non-supporting `aim` sentence.
    citation_precision: FrozenMap[Interval] = Field(default_factory=FrozenDict)
    aim_citation_rate: FrozenMap[Interval] = Field(default_factory=FrozenDict)

    _omit_if_default: ClassVar[tuple[str, ...]] = (
        "task_scores",
        "citation_precision",
        "aim_citation_rate",
    )

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
        lat = (self.latency_p50_ms, self.latency_p95_ms)
        if (lat[0] is None) != (lat[1] is None):
            raise ValueError("latency percentiles are both set or both None")
        if lat[0] is not None and lat[1] is not None and lat[1] < lat[0]:
            raise ValueError("latency_p95_ms cannot be below latency_p50_ms")
        if set(self.headline) != set(self.groundedness_mean):
            raise ValueError("headline and groundedness_mean must name the same judges")
        return self


def _check_errors_and_latency(
    n: int, n_errors: dict[str, int], lat: tuple[int | None, int | None]
) -> None:
    unknown = set(n_errors) - set(ERROR_TYPES)
    if unknown:
        raise ValueError(f"unknown error types in n_errors: {sorted(unknown)}")
    if sum(n_errors.values()) > n:
        raise ValueError("n_errors cannot exceed n")
    if (lat[0] is None) != (lat[1] is None):
        raise ValueError("latency percentiles are both set or both None")
    if lat[0] is not None and lat[1] is not None and lat[1] < lat[0]:
        raise ValueError("latency_p95_ms cannot be below latency_p50_ms")


class SliceSummary(Model):
    """One model on one slice of the cases (v0.2, D-59): the cases whose `field` (a case field
    such as `rule` or `split`) has `value`. Rates are over the slice's cases only, with 95%
    bootstrap intervals drawn over those cases."""

    field: NonBlank
    value: NonBlank  # "(none)" for cases without the field
    model: NonBlank
    n: NonNegInt
    n_errors: FrozenMap[NonNegInt]
    accuracy: Interval
    headline: FrozenMap[Interval]  # per judge; empty while judging is pending
    groundedness_mean: FrozenMap[Interval]
    citation_validity: Interval
    coverage: Interval
    task_scores: FrozenMap[Interval] = Field(default_factory=FrozenDict)
    cost_per_100_usd: NonNegFloat
    latency_p50_ms: NonNegInt | None
    latency_p95_ms: NonNegInt | None
    citation_precision: FrozenMap[Interval] = Field(default_factory=FrozenDict)  # B5.12
    aim_citation_rate: FrozenMap[Interval] = Field(default_factory=FrozenDict)  # B5.12

    _omit_if_default: ClassVar[tuple[str, ...]] = ("citation_precision", "aim_citation_rate")

    @model_validator(mode="after")
    def _consistency(self) -> SliceSummary:
        _check_errors_and_latency(
            self.n, dict(self.n_errors), (self.latency_p50_ms, self.latency_p95_ms)
        )
        if set(self.headline) != set(self.groundedness_mean):
            raise ValueError("headline and groundedness_mean must name the same judges")
        return self


class JudgeAgreement(Model):
    """Held-out agreement for one judge, valid only for this task and rubric (Tech Spec §5)."""

    judge: NonBlank
    task: NonBlank
    judge_prompt_hash: NonBlank
    split: Literal["heldout", "tuning"] = "heldout"  # only held-out agreement certifies
    n_claims: NonNegInt  # labelled claims compared (extraction errors excluded)
    coverage: Fraction  # share of eligible labelled claims the judge ruled on
    kappa: Interval
    agreement: Fraction
    confusion: tuple[tuple[NonNegInt, ...], ...]  # [[TP, FN], [FP, TN]], "supported" positive
    by_family: FrozenMap[Interval]

    @model_validator(mode="after")
    def _square(self) -> JudgeAgreement:
        if len(self.confusion) != 2 or any(len(r) != 2 for r in self.confusion):
            raise ValueError("confusion must be a 2x2 matrix")
        return self


class PairComparison(Model):
    """Paired-bootstrap difference model_a - model_b on shared cases (Tech Spec §6). `diff` is an
    unadjusted 95% interval; `separated` is decided by Holm across the model pairs of one metric
    and judge (OWNER D-36), so an interval that excludes 0 can still be a tie. Reports must
    label the interval as unadjusted."""

    metric: Literal["accuracy", "headline"]
    judge: NonBlank | None  # None for judge-free metrics
    model_a: NonBlank
    model_b: NonBlank
    diff: Interval
    p_value: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    separated: bool

    @model_validator(mode="after")
    def _shape(self) -> PairComparison:
        if self.model_a == self.model_b:
            raise ValueError("a model cannot be compared with itself")
        if (self.metric == "headline") != (self.judge is not None):
            raise ValueError("headline comparisons name a judge; accuracy comparisons do not")
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
    comparisons: tuple[PairComparison, ...] = ()
    extraction_error_rate: Fraction | None = None
    judging_status: Literal["pending", "complete"]
    started_at: Timestamp
    finished_at: Timestamp | None = None
    slices: tuple[SliceSummary, ...] = ()  # v0.2 (D-59): by task slice field, value, model

    _omit_if_default: ClassVar[tuple[str, ...]] = ("slices",)

    @model_validator(mode="after")
    def _stamps_are_honest(self) -> RunSummary:
        n_judges = 2 if self.judge_mode == "dual" else 1
        known = {m.model for m in self.models}
        for s in self.slices:
            if s.model not in known:
                raise ValueError(f"slice {s.field}={s.value} names unknown model {s.model!r}")
            if self.judging_status == "pending" and (
                s.headline or s.citation_precision or s.aim_citation_rate
            ):
                raise ValueError("a pending run cannot report per-judge figures in slices")
            if self.judging_status == "complete" and len(s.headline) != n_judges:
                raise ValueError(
                    f"slice {s.field}={s.value}/{s.model}: a {self.judge_mode}-judge run needs "
                    f"{n_judges} headline entries, got {len(s.headline)}"
                )
        names = [j.judge for j in self.judges]
        if any(j.split != "heldout" for j in self.judges):
            raise ValueError("only held-out agreement can be reported as calibration")
        if any(j.task != self.task for j in self.judges):
            raise ValueError("judge agreement must come from this run's task")
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
        if self.judging_status == "pending":
            # D-37: no per-judge figures until every judge has finished.
            self._check_times()
            if self.self_preference or any(
                m.headline or m.citation_precision or m.aim_citation_rate for m in self.models
            ):
                raise ValueError("a pending run cannot report per-judge figures")
            if any(c.metric == "headline" for c in self.comparisons):
                raise ValueError("a pending run cannot compare headlines")
            return self._check_times()
        for m in self.models:
            if len(m.headline) != n_judges:
                raise ValueError(
                    f"{m.model}: a {self.judge_mode}-judge run needs {n_judges} headline "
                    f"entries, got {len(m.headline)}"
                )
        return self._check_times()

    def _check_times(self) -> RunSummary:
        if (self.judging_status == "complete") != (self.finished_at is not None):
            raise ValueError("finished_at is set exactly when judging_status is complete")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be before started_at")
        return self
