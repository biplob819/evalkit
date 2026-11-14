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
import weakref
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
        self._batch_reservations: dict[str, float] = {}
        # Per event loop, weakly keyed so a finished loop and its locks can be freed.
        self._named_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
        ] = weakref.WeakKeyDictionary()
        self.cache_hits = 0
        self.calls = 0
        self.coalesced = 0

    @property
    def spent_usd(self) -> float:
        return self._spend.spent

    @property
    def in_flight_usd(self) -> float:
        """Reserved but not yet settled spend, including open batch holds."""
        return self._spend.in_flight

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
        async with self._sem(node.pool(inp)):
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
        # A corrupt/stale entry exists on disk: overwrite it; otherwise first writer wins.
        self.store.put(key, out, lineage, overwrite=self.store.has(key))
        latency = int((time.perf_counter() - t0) * 1000)
        await self._log(node=node.name, key=key, cache_hit=False, latency_ms=latency, cost_usd=cost)
        return out

    async def reserve(self, estimate: float) -> None:
        """Reserve budget for work that does not go through run_node (provider batches).
        Raises BudgetExceeded if it would not fit; pair with `settle`."""
        await self._reserve(_check_amount(estimate, "estimate", "batch"))

    async def settle(self, estimate: float, actual: float) -> None:
        """Release a reservation and record what was actually spent."""
        actual = _check_amount(actual, "actual cost", "batch")
        async with self._lock:
            self._spend.in_flight -= estimate
            if abs(self._spend.in_flight) < 1e-12:  # float residue from many add/subtracts
                self._spend.in_flight = 0.0
            self._spend.spent += actual

    async def hold_batch(self, batch_id: str, estimate: float) -> None:
        """Keep a reservation open for a submitted batch until `settle_batch` (money for a
        submitted batch is committed, so a pending batch keeps its budget held)."""
        self._batch_reservations[batch_id] = self._batch_reservations.get(batch_id, 0.0) + estimate

    async def rehold_batch(self, batch_id: str, estimate: float) -> None:
        """Hold budget for a batch an earlier process submitted and has not been collected.
        That money is already committed, so the hold is taken even past the cap; fresh work
        then reserves against what is left. A batch already held here is not held twice."""
        estimate = _check_amount(estimate, "estimate", "batch")
        async with self._lock:
            if batch_id in self._batch_reservations:
                return
            self._spend.in_flight += estimate
            self._batch_reservations[batch_id] = estimate

    def named_lock(self, name: str) -> asyncio.Lock:
        """One lock per name (and event loop) for this executor, e.g. to serialise
        scan-and-submit per judge. Keyed by loop because a lock that has waited in one loop
        cannot be used from another, and an executor may outlive an `asyncio.run`."""
        locks = self._named_locks.setdefault(asyncio.get_running_loop(), {})
        if name not in locks:
            locks[name] = asyncio.Lock()
        return locks[name]

    async def settle_batch(self, batch_id: str, actual: float) -> None:
        """Record a collected batch's real cost, releasing this process's reservation for it.
        Batches submitted by an earlier process have no reservation here; their cost is
        recorded as committed spend."""
        await self.settle(self._batch_reservations.pop(batch_id, 0.0), actual)

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

    async def map_settled(
        self,
        node: Node[In, Out],
        inputs: Sequence[In],
        ctx: RunContext | Callable[[In], RunContext],
        *,
        parents: Callable[[In], Sequence[str]] | None = None,
    ) -> list[Out | Exception]:
        """Like `map`, but returns every item's result or exception in input order and never
        raises for a failed item, so one bad call cannot stop a run (NFR5, GEN-03)."""

        def ctx_for(item: In) -> RunContext:
            return ctx(item) if callable(ctx) else ctx

        results = await asyncio.gather(
            *(
                self.run_node(node, item, ctx_for(item), parents=parents(item) if parents else ())
                for item in inputs
            ),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, Exception):
                raise r  # cancellation and interrupts still propagate
        return list(results)  # type: ignore[arg-type]

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
