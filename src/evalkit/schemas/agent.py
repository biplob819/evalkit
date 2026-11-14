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
    canaries: tuple[Name, ...] = ()  # planted secrets/markers; leaking one = attack succeeded


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
    gold_sql: str | None = None  # analytics: the query that produces `gold_outcome` (C3.1)


class Step(Model):
    index: NonNegInt
    tool: Name
    arguments: FrozenMap[JsonValue]
    result_summary: str
    error: str | None = None
    tokens_in: NonNegInt
    tokens_out: NonNegInt  # visible output plus reasoning tokens (both billed as output)
    latency_ms: NonNegInt
    # C3.2: "tool_call" is an action the agent took; "malformed" (unparseable output, kept
    # whole in arguments["raw"] so it is still scanned for leaks) and "refusal" are not.
    kind: Literal["tool_call", "malformed", "refusal"] = "tool_call"
    # The sandbox refused the call (tool outside the allow-list, invalid arguments, a write
    # or escape attempt) and it never ran. A failed query that did run is not a violation.
    violation: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Step:
        if self.violation and (self.error is None or self.kind != "tool_call"):
            raise ValueError("a violation is a refused tool call and carries its error")
        if self.kind != "tool_call" and self.error is None:
            raise ValueError(f"a {self.kind} step carries an error")
        return self


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
    # Why the trial ended (C3.2): void = failed reset check (excluded from scores, counted);
    # transport = provider failure after retries (rerun to resume from cached turns).
    stop_reason: Literal["final_answer", "step_budget", "refusal", "transport", "void"] = (
        "final_answer"
    )
    # C3.3: what the simulated user cost in this trial. Kept apart from `cost_usd` (the
    # agent's own spend, the basis of cost per success); reported alongside it.
    simulator_cost_usd: NonNegFloat = 0.0

    @model_validator(mode="after")
    def _steps_in_order(self) -> Trajectory:
        if [s.index for s in self.steps] != list(range(len(self.steps))):
            raise ValueError("step indexes must be 0, 1, 2, ... in order")
        if (self.stop_reason == "void") != (not self.sandbox_reset_ok):
            raise ValueError("a trajectory is void exactly when its sandbox reset check failed")
        if self.stop_reason == "void" and self.steps:
            raise ValueError("a void trajectory has no steps")
        return self


class AgentScore(Model):
    task_id: NonBlank
    agent: NonBlank
    simulator: str | None = None  # per-simulator reporting (FR22); None without ask_user
    trial: PosInt
    success: bool
    valid_tool_calls: Fraction
    policy_violations: NonNegInt
    steps_over_gold: StrictInt
    recovered_from_error: bool | None = None
    cost_usd: NonNegFloat  # the agent's own spend (basis of cost per success)
    simulator_cost_usd: NonNegFloat = 0.0
    split: Literal["public", "heldout"] = "public"
    detail: str = ""  # why it failed, in a few words (empty on success)
    error: str | None = None  # "void" when the sandbox reset check failed (excluded, counted)
