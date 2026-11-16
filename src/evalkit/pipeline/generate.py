"""Generate and Validate nodes (GEN-01 to GEN-04), and GenerateSystem for systems under test
(v0.2, D-59)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evalkit.adapters import CompletionRequest, ModelAdapter, RawCompletion, SystemAdapter
from evalkit.cost import Price, call_cost, estimate_tokens, price_for
from evalkit.pipeline.answer import SchemaError, answer_schema, parse_answer
from evalkit.retry import is_retryable
from evalkit.runtime import BudgetExceeded, Executor, Node, RunContext
from evalkit.schemas import Case, Completion, RunConfig, TaskConfig
from evalkit.schemas._base import Model, Name, NonBlank, PosInt
from evalkit.tasks import render_prompt

OPENAI_MODEL = re.compile(r"^(gpt-|o\d+(-|$))")


SYSTEM = "system"


def provider_for(
    model_id: str,
    endpoints: Mapping[str, str] | None = None,
    systems: Mapping[str, str] | None = None,
) -> str:
    """Which adapter serves a model: a system under test (v0.2), an OpenAI-compatible
    endpoint, Anthropic or OpenAI."""
    if systems and model_id in systems:
        return SYSTEM
    if endpoints and model_id in endpoints:
        return "openai_compatible"
    if model_id.startswith("claude-"):
        return "anthropic"
    if OPENAI_MODEL.match(model_id):
        return "openai"
    raise ValueError(
        f"cannot tell which provider serves {model_id!r}; add it to `endpoints` with a base_url"
    )


def adapter_key(provider: str, model_id: str) -> str:
    """Adapters are shared per provider; each OpenAI-compatible endpoint and each system under
    test gets its own."""
    return f"{provider}:{model_id}" if provider in ("openai_compatible", SYSTEM) else provider


class GenerateInput(Model):
    case_id: NonBlank
    prompt: str
    model: Name
    model_id: Name
    provider: Name
    effort: Name | None
    max_output_tokens: PosInt
    structured_output: bool
    labels: tuple[Name, ...]


class Generate(Node[GenerateInput, RawCompletion]):
    """Call one model on one prompt. Transport errors propagate after the adapter's retries,
    so they are never cached and a rerun tries again."""

    name = "generate"
    code_version = "1"
    In = GenerateInput
    Out = RawCompletion

    def __init__(self, adapters: Mapping[str, ModelAdapter], prices: dict[str, Price]) -> None:
        self.adapters = adapters
        self.prices = prices

    def pool(self, inp: GenerateInput) -> str | None:
        return inp.provider

    def estimate_cost(self, inp: GenerateInput, ctx: RunContext) -> float:
        # Upper bound: ~1 token per 3 prompt characters, plus the full output budget.
        price = price_for(
            self.prices, inp.model_id, free_if_unknown=inp.provider == "openai_compatible"
        )
        return call_cost(
            price, tokens_in=estimate_tokens(inp.prompt), tokens_out=inp.max_output_tokens
        )

    async def run(self, inp: GenerateInput, ctx: RunContext) -> RawCompletion:
        adapter = self.adapters[adapter_key(inp.provider, inp.model_id)]
        req = CompletionRequest(
            model=inp.model,
            model_id=inp.model_id,
            prompt=inp.prompt,
            max_output_tokens=inp.max_output_tokens,
            params={"effort": inp.effort} if inp.effort else {},
            json_schema=answer_schema(inp.labels) if inp.structured_output else None,
        )
        return await adapter.complete(req)


class ValidateInput(Model):
    raw: RawCompletion
    labels: tuple[Name, ...]
    structured_output: bool


def _completion(raw: RawCompletion, structured: bool, **over: Any) -> Completion:
    params: dict[str, Any] = {**raw.params, "structured_output": structured}
    base: dict[str, Any] = dict(
        model=raw.model,
        model_snapshot=raw.model_snapshot,
        params=params,
        raw_text=raw.raw_text,
        tokens_in=raw.tokens_in,
        tokens_out=raw.tokens_out,
        tokens_reasoning=raw.tokens_reasoning,
        tokens_cached=raw.tokens_cached,
        latency_ms=raw.latency_ms,
        cost_usd=raw.cost_usd,
        request_id=raw.request_id,
    )
    base.update(over)
    return Completion(**base)


class Validate(Node[ValidateInput, Completion]):
    """Parse the structured answer. A refusal is error="refusal"; anything unparseable or
    invalid is error="schema" and is never retried with hints (B1)."""

    name = "validate"
    code_version = "2"  # 2: whole output must be one JSON object (A2.1 verifier)
    In = ValidateInput
    Out = Completion

    async def run(self, inp: ValidateInput, ctx: RunContext) -> Completion:
        raw = inp.raw
        if raw.refusal:
            return _completion(raw, inp.structured_output, answer=None, error="refusal")
        try:
            answer = parse_answer(raw.raw_text, inp.labels)
        except SchemaError:
            return _completion(raw, inp.structured_output, answer=None, error="schema")
        return _completion(raw, inp.structured_output, answer=answer, error=None)


def is_provider_failure(exc: BaseException) -> bool:
    """Only transient provider errors that survived every retry are recorded as
    error="transport". Auth (401/403), bad requests (400/404/422), unknown prices and missing
    adapters are configuration problems and stop the run instead."""
    return is_retryable(exc)


def transport_failure(inp: GenerateInput, exc: BaseException) -> Completion:
    """The record for a call that failed after every retry (GEN-03). Not cached."""
    return Completion(
        model=inp.model,
        model_snapshot="",
        raw_text="",
        params={
            **({"effort": inp.effort} if inp.effort else {}),
            "structured_output": inp.structured_output,
            "transport_error": type(exc).__name__,
        },
        answer=None,
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        cost_usd=0.0,
        error="transport",
    )


class SystemInput(Model):
    """One case for a system under test. The whole case is in the cache key (its `meta`
    included), with the system's name and version, so a changed case or a new system version
    asks again and anything else is served from the cache."""

    case: Case
    prompt: str
    model: Name  # the alias in `models`
    system: Name  # the registered system name
    system_version: NonBlank
    max_output_tokens: PosInt
    structured_output: bool
    labels: tuple[Name, ...]


class GenerateSystem(Node[SystemInput, RawCompletion]):
    """Ask a system under test one case. Failures propagate like Generate's: transient ones
    become transport records upstream, never cached; anything else stops the run."""

    name = "generate_system"
    code_version = "1"
    In = SystemInput
    Out = RawCompletion

    def __init__(self, adapters: Mapping[str, ModelAdapter], prices: dict[str, Price]) -> None:
        self.adapters = adapters
        self.prices = prices

    def _adapter(self, inp: SystemInput) -> SystemAdapter:
        adapter = self.adapters[adapter_key(SYSTEM, inp.model)]
        if not isinstance(adapter, SystemAdapter):
            raise TypeError(f"{inp.model!r} is routed to a system but got {type(adapter).__name__}")
        return adapter

    def _request(self, inp: SystemInput) -> CompletionRequest:
        return CompletionRequest(
            model=inp.model,
            model_id=inp.model,
            prompt=inp.prompt,
            max_output_tokens=inp.max_output_tokens,
            json_schema=answer_schema(inp.labels) if inp.structured_output else None,
        )

    def pool(self, inp: SystemInput) -> str | None:
        return f"{SYSTEM}:{inp.model}"

    def estimate_cost(self, inp: SystemInput, ctx: RunContext) -> float:
        return float(self._adapter(inp).estimate_cost(inp.case, self._request(inp)))

    async def run(self, inp: SystemInput, ctx: RunContext) -> RawCompletion:
        out = await self._adapter(inp).answer(inp.case, self._request(inp))
        if not isinstance(out, RawCompletion):
            raise TypeError(
                f"system {inp.system!r} returned {type(out).__name__}, expected RawCompletion"
            )
        return out


def system_inputs(cases: Sequence[Case], task: TaskConfig, config: RunConfig) -> list[SystemInput]:
    """One input per (system model, case), in config order."""
    from evalkit.adapters import discover_systems

    registered = discover_systems()
    out = []
    for model in config.models:
        if model not in config.systems:
            continue
        name = config.systems[model]
        if name not in registered:
            raise KeyError(f"unknown system {name!r}; registered: {sorted(registered)}")
        for case in cases:
            out.append(
                SystemInput(
                    case=case,
                    prompt=render_prompt(task, case),
                    model=model,
                    system=name,
                    system_version=registered[name].version,
                    max_output_tokens=config.max_output_tokens,
                    structured_output=config.structured_output,
                    labels=task.labels,
                )
            )
    return out


@dataclass(frozen=True)
class Generated:
    case: Case
    model: str
    completion: Completion


def generate_inputs(
    cases: Sequence[Case], task: TaskConfig, config: RunConfig
) -> list[GenerateInput]:
    out = []
    for model in config.models:
        provider = provider_for(model, config.endpoints, config.systems)
        if provider == SYSTEM:
            continue  # systems under test go through GenerateSystem
        effort = (
            None
            if provider == "openai_compatible" and model not in config.efforts
            else config.effort_for(model)
        )
        for case in cases:
            out.append(
                GenerateInput(
                    case_id=case.id,
                    prompt=render_prompt(task, case),
                    model=model,
                    model_id=model,
                    provider=provider,
                    effort=effort,
                    max_output_tokens=config.max_output_tokens,
                    structured_output=config.structured_output,
                    labels=task.labels,
                )
            )
    return out


async def generate_and_validate(
    executor: Executor,
    cases: Sequence[Case],
    task: TaskConfig,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    *,
    run_id: str,
) -> list[Generated]:
    """Every (model, case) pair becomes one Generated record; failures are recorded, not raised."""
    by_id = {c.id: c for c in cases}
    inputs = generate_inputs(cases, task, config)
    gen = Generate(adapters, prices)
    ctx = RunContext(run_id=run_id, seed=config.seed)
    if config.systems:
        return await _with_systems(executor, cases, task, config, adapters, prices, ctx)
    raws = await executor.map_settled(gen, inputs, ctx)
    budget = [r for r in raws if isinstance(r, BudgetExceeded)]
    if budget:
        # Stop: calls that never started are not provider failures. Completed calls are
        # cached, so rerunning with a higher cap resumes (RUN-08).
        raise BudgetExceeded(executor.spent_usd, budget[0].cap, remaining=len(budget))
    local = [r for r in raws if isinstance(r, Exception) and not is_provider_failure(r)]
    if local:
        raise local[0]  # bad key, bad config, missing adapter or price: fix it, do not score it
    validate = Validate()
    out: list[Generated] = []
    for inp, raw in zip(inputs, raws, strict=True):
        if isinstance(raw, Exception):
            completion = transport_failure(inp, raw)
        else:
            completion = await executor.run_node(
                validate,
                ValidateInput(raw=raw, labels=inp.labels, structured_output=inp.structured_output),
                ctx,
                parents=[gen.cache_key(inp, ctx)],
            )
        out.append(Generated(case=by_id[inp.case_id], model=inp.model, completion=completion))
    return out


async def _with_systems(
    executor: Executor,
    cases: Sequence[Case],
    task: TaskConfig,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    ctx: RunContext,
) -> list[Generated]:
    """generate_and_validate for a config with systems under test (v0.2): provider models go
    through Generate, systems through GenerateSystem, both with the same failure taxonomy and
    the same Validate step; the result is in config order, model by model, case by case."""
    by_id = {c.id: c for c in cases}
    gen, sysgen = Generate(adapters, prices), GenerateSystem(adapters, prices)
    inputs = generate_inputs(cases, task, config)
    sys_inputs = system_inputs(cases, task, config)
    raws, sys_raws = await asyncio.gather(
        executor.map_settled(gen, inputs, ctx), executor.map_settled(sysgen, sys_inputs, ctx)
    )
    every: list[RawCompletion | Exception] = [*raws, *sys_raws]
    budget = [r for r in every if isinstance(r, BudgetExceeded)]
    if budget:
        raise BudgetExceeded(executor.spent_usd, budget[0].cap, remaining=len(budget))
    local = [r for r in every if isinstance(r, Exception) and not is_provider_failure(r)]
    if local:
        raise local[0]
    validate = Validate()
    done: dict[tuple[str, str], Generated] = {}
    items: list[tuple[GenerateInput | SystemInput, RawCompletion | Exception, str]] = [
        *((i, r, gen.cache_key(i, ctx)) for i, r in zip(inputs, raws, strict=True)),
        *((i, r, sysgen.cache_key(i, ctx)) for i, r in zip(sys_inputs, sys_raws, strict=True)),
    ]
    for inp, raw, parent in items:
        case_id = inp.case_id if isinstance(inp, GenerateInput) else inp.case.id
        if isinstance(raw, Exception):
            completion = _failure(inp, raw)
        else:
            completion = await executor.run_node(
                validate,
                ValidateInput(raw=raw, labels=inp.labels, structured_output=inp.structured_output),
                ctx,
                parents=[parent],
            )
        done[inp.model, case_id] = Generated(
            case=by_id[case_id], model=inp.model, completion=completion
        )
    return [done[m, c.id] for m in config.models for c in cases]


def _failure(inp: GenerateInput | SystemInput, exc: BaseException) -> Completion:
    if isinstance(inp, GenerateInput):
        return transport_failure(inp, exc)
    return Completion(
        model=inp.model,
        model_snapshot="",
        raw_text="",
        params={"structured_output": inp.structured_output, "transport_error": type(exc).__name__},
        answer=None,
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        cost_usd=0.0,
        error="transport",
    )
