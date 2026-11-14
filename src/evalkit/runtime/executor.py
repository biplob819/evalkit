"""Async executor (RUN-04, RUN-05, RUN-08): cache-aware node calls, fan-out, spend cap.

Spend cap rules:
- A call reserves its `estimate_cost` only once it holds its concurrency slot, so queued
  calls do not block each other's budget.
- Estimates and actual costs must be finite and non-negative; anything else is an error.
- Estimates should be upper bounds. If a node under-estimates, the cap can be overshot by at
  most the in-flight calls' shortfall: once actual spend reaches the cap, every later call
  is refused, because its reservation can no longer fit.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from evalkit.runtime.node import Node, RunContext
from evalkit.runtime.store import Lineage, Store
from evalkit.runtime.writer import JsonlWriter

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class BudgetExceeded(Exception):
    """Raised when the next call could push spend past `max_cost_usd`. Completed work stays
    cached, so rerunning with a higher cap (or none) resumes where this stopped."""

    def __init__(self, spent: float, cap: float, remaining: int = 1) -> None:
        self.spent, self.cap, self.remaining = spent, cap, remaining
        super().__init__(
            f"cost cap ${cap:.2f} reached (spent ${spent:.4f}); {remaining} calls not started"
        )


def _check_amount(value: float, what: str, node: str) -> float:
    if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{node}: {what} must be a finite, non-negative number, got {value!r}")
    return float(value)


@dataclass
class _Spend:
    spent: float = 0.0
    in_flight: float = 0.0


class Executor:
    def __init__(
        self,
        store: Store,
        *,
        concurrency: dict[str, int] | None = None,
        default_concurrency: int = 4,
        max_cost_usd: float | None = None,
        log: JsonlWriter | None = None,
    ) -> None:
        if max_cost_usd is not None:
            _check_amount(max_cost_usd, "max_cost_usd", "executor")
        self.store = store
        self.max_cost_usd = max_cost_usd
        self.log = log
        self._limits = dict(concurrency or {})
        self._default = default_concurrency
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._spend = _Spend()
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[BaseModel]] = {}
        self.cache_hits = 0
        self.calls = 0
        self.coalesced = 0

    @property
    def spent_usd(self) -> float:
        return self._spend.spent

    def _sem(self, provider: str | None) -> asyncio.Semaphore:
        # Nodes without a provider share the "_local" pool (default_concurrency slots).
        name = provider or "_local"
        if name not in self._sems:
            self._sems[name] = asyncio.Semaphore(self._limits.get(name, self._default))
        return self._sems[name]

    async def _reserve(self, estimate: float) -> None:
        async with self._lock:
            cap = self.max_cost_usd
            if cap is not None and self._spend.spent + self._spend.in_flight + estimate > cap:
                raise BudgetExceeded(self._spend.spent, cap)
            self._spend.in_flight += estimate

    async def _log(self, **fields: Any) -> None:
        if self.log is not None:
            await self.log.write(fields)

    def _cached(self, node: Node[In, Out], key: str) -> Out | None:
        try:
            return self.store.get(key, node.Out)  # type: ignore[return-value]
        except (ValidationError, ValueError):
            # Corrupt or stale-schema entry: treat as a miss and recompute (it is overwritten).
            return None

    async def run_node(
        self,
        node: Node[In, Out],
        inp: In,
        ctx: RunContext,
        *,
        parents: Sequence[str] = (),
    ) -> Out:
        key = node.cache_key(inp, ctx)
        cached = self._cached(node, key)
        if cached is not None:
            self.cache_hits += 1
            await self._log(node=node.name, key=key, cache_hit=True, latency_ms=0, cost_usd=0.0)
            return cached
        pending = self._pending.get(key)
        if pending is not None:  # identical work already running: share its result
            self.coalesced += 1
            return await asyncio.shield(pending)  # type: ignore[return-value]
        fut: asyncio.Future[BaseModel] = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        try:
            out = await self._execute(node, inp, ctx, key, parents)
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so an unawaited future does not warn
            raise
        else:
            fut.set_result(out)
            return out
        finally:
            del self._pending[key]

    async def _execute(
        self, node: Node[In, Out], inp: In, ctx: RunContext, key: str, parents: Sequence[str]
    ) -> Out:
        estimate = _check_amount(node.estimate_cost(inp, ctx), "estimate_cost", node.name)
        async with self._sem(node.provider):
            try:
                await self._reserve(estimate)
            except BudgetExceeded:
                await self._log(node=node.name, key=key, cache_hit=False, error="budget_exceeded")
                raise
            started = datetime.now(UTC)
            t0 = time.perf_counter()
            try:
                self.calls += 1
                out = await node.run(inp, ctx)
                if not isinstance(out, node.Out):
                    raise TypeError(
                        f"{node.name} returned {type(out).__name__}, not {node.Out.__name__}"
                    )
                cost = _check_amount(node.cost_of(out), "cost_of", node.name)
            except Exception as exc:
                latency = int((time.perf_counter() - t0) * 1000)
                await self._log(
                    node=node.name,
                    key=key,
                    cache_hit=False,
                    latency_ms=latency,
                    cost_usd=0.0,
                    error=repr(exc),
                )
                raise
            finally:
                async with self._lock:
                    self._spend.in_flight -= estimate
            async with self._lock:
                self._spend.spent += cost
        lineage = Lineage(
            key=key,
            node=node.name,
            parents=tuple(parents),
            started_at=started,
            finished_at=datetime.now(UTC),
            cost_usd=cost,
        )
        self.store.put(key, out, lineage)
        latency = int((time.perf_counter() - t0) * 1000)
        await self._log(node=node.name, key=key, cache_hit=False, latency_ms=latency, cost_usd=cost)
        return out

    async def submit_once(
        self, key: str, provider: str, submit: Callable[[], Awaitable[str]]
    ) -> str:
        """Return the batch id stored under `key`, submitting only if none exists yet.

        A resumed run therefore collects the batch it already paid for instead of
        submitting a second one (RUN-05).
        """
        meta = self.store.get_meta(key, "batch")
        if meta is not None:
            return str(meta["batch_id"])
        batch_id = await submit()
        self.store.put_meta(key, "batch", {"batch_id": batch_id, "provider": provider})
        return batch_id

    async def map(
        self,
        node: Node[In, Out],
        inputs: Sequence[In],
        ctx: RunContext | Callable[[In], RunContext],
        *,
        parents: Callable[[In], Sequence[str]] | None = None,
    ) -> list[Out]:
        """Fan out over inputs, fan results back in input order. Every item is attempted.

        If any item failed, the first non-budget error is raised (after the rest finished and
        were cached); if the only failures were budget refusals, BudgetExceeded is raised
        with the number of calls that did not start.
        """

        def ctx_for(item: In) -> RunContext:
            return ctx(item) if callable(ctx) else ctx

        results = await asyncio.gather(
            *(
                self.run_node(node, item, ctx_for(item), parents=parents(item) if parents else ())
                for item in inputs
            ),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        other = [e for e in errors if not isinstance(e, BudgetExceeded)]
        if other:
            raise other[0]
        if errors:
            cap = errors[0].cap  # type: ignore[attr-defined]
            raise BudgetExceeded(self.spent_usd, cap, remaining=len(errors))
        return list(results)  # type: ignore[arg-type]
