"""Run and task configuration (SCH-04)."""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import Field, field_validator

from evalkit.schemas._base import FrozenMap, Model, Name, PosInt, StrictInt

# Per-judge metrics (headline, groundedness) are checked against the worst judge.
ThresholdMetric = Literal[
    "headline",
    "groundedness",
    "accuracy",
    "citation_validity",
    "coverage",
    "cost_per_100_usd",
    "cost_per_correct_usd",
    "latency_p50_ms",
    "latency_p95_ms",
]
THRESHOLD_METRICS: tuple[str, ...] = get_args(ThresholdMetric)


class Threshold(Model):
    """A pass bar: `metric op value`. A model passes only if every threshold holds."""

    metric: ThresholdMetric
    op: Literal[">=", "<="]
    value: Annotated[float, Field(allow_inf_nan=False)]

    def holds(self, observed: float) -> bool:
        return observed >= self.value if self.op == ">=" else observed <= self.value


def _unique(v: tuple[str, ...]) -> tuple[str, ...]:
    folded = [s.casefold() for s in v]
    if len(set(folded)) != len(folded):
        raise ValueError("entries must be unique (case-insensitive)")
    return v


class RunConfig(Model):
    models: Annotated[tuple[Name, ...], Field(min_length=1)]
    judges: Annotated[tuple[Name, ...], Field(min_length=1, max_length=2)]
    extractor: Name
    thresholds: tuple[Threshold, ...] = ()
    structured_output: bool = True
    max_output_tokens: PosInt = 1024
    concurrency: FrozenMap[PosInt] = Field(default_factory=dict)
    seed: StrictInt = 7
    max_cost_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    endpoints: FrozenMap[Name] = Field(default_factory=dict)
    # Effort is always explicit (D-24): per-model overrides, else the default.
    efforts: FrozenMap[Name] = Field(default_factory=dict)
    default_effort: Name = "medium"
    judge_effort: Name = "medium"  # OWNER D-34 (supersedes D-28)
    extractor_effort: Name = "medium"

    def effort_for(self, model: str) -> str:
        return self.efforts.get(model, self.default_effort)

    @field_validator("models", "judges")
    @classmethod
    def _unique_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(v)


class TaskConfig(Model):
    name: Name
    dataset: Name
    split: Name
    labels: Annotated[tuple[Name, ...], Field(min_length=1)]
    prompt_template: Annotated[str, Field(min_length=1)]
    output_schema: Name
    scorers: Annotated[tuple[Name, ...], Field(min_length=1)]
    thresholds: tuple[Threshold, ...] = ()

    @field_validator("labels")
    @classmethod
    def _unique_labels(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        try:
            return _unique(v)
        except ValueError:
            raise ValueError("labels must be unique (case-insensitive)") from None
