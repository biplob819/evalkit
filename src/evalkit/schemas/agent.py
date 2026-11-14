"""Agent-track schemas (SCH-05)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from evalkit.schemas._base import (
    Fraction,
    FrozenMap,
    JsonValue,
    Model,
    Name,
    NonBlank,
    NonNegFloat,
    NonNegInt,
    PosInt,
    StrictInt,
)


class ResultSet(Model):
    kind: Literal["result_set"] = "result_set"
    columns: Annotated[tuple[Name, ...], Field(min_length=1)]
    rows: tuple[tuple[JsonValue, ...], ...]
    ordered: bool = False

    @model_validator(mode="after")
    def _rectangular(self) -> ResultSet:
        bad = [i for i, r in enumerate(self.rows) if len(r) != len(self.columns)]
        if bad:
            raise ValueError(f"rows {bad} do not have {len(self.columns)} values")
        return self


class AttackBlocked(Model):
    kind: Literal["attack_blocked"] = "attack_blocked"
    attack: NonBlank  # e.g. "prompt_injection"


GoldOutcome = Annotated[ResultSet | AttackBlocked, Field(discriminator="kind")]


class AgentTask(Model):
    id: NonBlank
    source_project: NonBlank  # free string; no downstream names in the library
    goal: NonBlank
    allowed_tools: Annotated[tuple[Name, ...], Field(min_length=1)]
    step_budget: PosInt
    gold_outcome: GoldOutcome
    simulated_user_intent: str | None  # required; None means the task is unambiguous
    split: Literal["public", "heldout"]


class Step(Model):
    index: NonNegInt
    tool: Name
    arguments: FrozenMap[JsonValue]
    result_summary: str
    error: str | None = None
    tokens_in: NonNegInt
    tokens_out: NonNegInt
    latency_ms: NonNegInt


class Trajectory(Model):
    task_id: NonBlank
    agent: NonBlank
    simulator: str | None = None
    trial: PosInt
    seed: StrictInt
    steps: tuple[Step, ...]
    final_answer: str
    sandbox_reset_ok: bool
    cost_usd: NonNegFloat

    @model_validator(mode="after")
    def _steps_in_order(self) -> Trajectory:
        if [s.index for s in self.steps] != list(range(len(self.steps))):
            raise ValueError("step indexes must be 0, 1, 2, ... in order")
        return self


class AgentScore(Model):
    task_id: NonBlank
    agent: NonBlank
    trial: PosInt
    success: bool
    valid_tool_calls: Fraction
    policy_violations: NonNegInt
    steps_over_gold: StrictInt
    recovered_from_error: bool | None = None
    cost_usd: NonNegFloat
    error: str | None = None
