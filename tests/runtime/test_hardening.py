"""Regression tests for the N1.3 verifier findings (lessons L-12 to L-14)."""

import asyncio
import json
import threading

import pytest

from evalkit.runtime import BudgetExceeded, Executor, JsonlWriter, RunContext, Store, canonical_json
from evalkit.runtime.store import Lineage
from tests.runtime.helpers import Double, Flaky, Num, Spend

CTX = RunContext(run_id="r1", model="m", seed=7)
KEY = "ab" + "0" * 62


class Estimating(Spend):
    """Estimate and actual cost set independently."""

    name = "estimating"

    def __init__(self, estimate: float, actual: float) -> None:
        super().__init__(actual)
        self.estimate = estimate

    def estimate_cost(self, inp, ctx):  # type: ignore[override]
        return self.estimate


# ---- finding 1/2: non-finite or negative money is an error, never a bypass ----------


@pytest.mark.parametrize("bad", [float("nan"), -1.0, float("inf")])
def test_bad_estimates_are_errors_not_bypasses(tmp_path, bad):
    async def go():
        ex = Executor(Store(tmp_path), concurrency={"openai": 10}, max_cost_usd=2)
        with pytest.raises(ValueError, match="estimate_cost"):
            await ex.map(Estimating(bad, 1.0), [Num(x=i) for i in range(10)], CTX)
        assert ex.spent_usd == 0

    asyncio.run(go())


@pytest.mark.parametrize("bad", [float("nan"), -5.0])
def test_bad_actual_costs_are_errors_and_do_not_corrupt_spend(tmp_path, bad):
    async def go():
        ex = Executor(Store(tmp_path), max_cost_usd=1)
        with pytest.raises(ValueError, match="cost_of"):
            await ex.run_node(Estimating(0.1, bad), Num(x=1), CTX)
        assert ex.spent_usd == 0
        with pytest.raises(BudgetExceeded):
            await ex.run_node(Estimating(100, 100), Num(x=2), CTX)

    asyncio.run(go())


def test_bad_cap_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        Executor(Store(tmp_path), max_cost_usd=float("nan"))


# ---- finding 3: reserve inside the slot; overshoot is bounded -------------------------


def test_queued_calls_do_not_hold_budget(tmp_path):
    async def go():
        ex = Executor(Store(tmp_path), concurrency={"openai": 1}, max_cost_usd=3.5)
        out = await ex.map(Estimating(1.0, 0.1), [Num(x=i) for i in range(10)], CTX)
        assert len(out) == 10 and ex.spent_usd == pytest.approx(1.0)

    asyncio.run(go())


def test_underestimating_node_overshoots_at_most_once_then_stops(tmp_path):
    async def go():
        ex = Executor(Store(tmp_path), concurrency={"openai": 1}, max_cost_usd=1.0)
        with pytest.raises(BudgetExceeded) as exc:
            await ex.map(Estimating(0.0, 5.0), [Num(x=i) for i in range(5)], CTX)
        assert ex.calls == 1 and ex.spent_usd == 5.0 and exc.value.remaining == 4

    asyncio.run(go())


# ---- finding 6: batch id reuse -----------------------------------------------------------


def test_submit_once_reuses_a_stored_batch_id(tmp_path):
    submitted: list[int] = []

    async def submit() -> str:
        submitted.append(1)
        return f"msgbatch_{len(submitted)}"

    async def go():
        first = await Executor(Store(tmp_path)).submit_once(KEY, "anthropic", submit)
        resumed = await Executor(Store(tmp_path)).submit_once(KEY, "anthropic", submit)
        return first, resumed

    assert asyncio.run(go()) == ("msgbatch_1", "msgbatch_1")
    assert submitted == [1]


# ---- finding 7: identical in-flight work is done once -----------------------------------


class SlowSpend(Spend):
    name = "slow_spend"

    async def run(self, inp, ctx):  # type: ignore[override]
        await asyncio.sleep(0.01)  # yield, so duplicates arrive while the first is in flight
        return await super().run(inp, ctx)


def test_duplicate_inputs_in_one_map_are_coalesced(tmp_path):
    async def go():
        node = SlowSpend(1.0)
        ex = Executor(Store(tmp_path), concurrency={"openai": 5}, max_cost_usd=10)
        out = await ex.map(node, [Num(x=1)] * 5, CTX)
        assert [o.y for o in out] == [1] * 5
        assert node.calls == 1 and ex.spent_usd == 1.0 and ex.coalesced == 4

    asyncio.run(go())


def test_coalesced_waiters_see_the_same_failure(tmp_path):
    async def go():
        with pytest.raises(RuntimeError, match="boom"):
            await Executor(Store(tmp_path)).map(Flaky(broken=3), [Num(x=3)] * 3, CTX)

    asyncio.run(go())


# ---- finding 8: writer and run log ------------------------------------------------------


def test_writer_refuses_nan_and_writes_after_close(tmp_path):
    async def go():
        async with JsonlWriter(tmp_path / "a.jsonl") as w:
            with pytest.raises(ValueError):
                await w.write({"x": float("nan")})
        with pytest.raises(RuntimeError):
            await w.write({"x": 1})

    asyncio.run(go())


def test_run_log_records_errors_and_budget_refusals(tmp_path):
    log_path = tmp_path / "run.log"

    async def go():
        async with JsonlWriter(log_path) as log:
            ex = Executor(Store(tmp_path / "s"), log=log, max_cost_usd=0.5)
            with pytest.raises(RuntimeError):
                await ex.run_node(Flaky(broken=1), Num(x=1), CTX)
            with pytest.raises(BudgetExceeded):
                await ex.run_node(Spend(1.0), Num(x=2), CTX)

    asyncio.run(go())
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert "boom" in entries[0]["error"] and "latency_ms" in entries[0]
    assert entries[1]["error"] == "budget_exceeded"


# ---- finding 9: errors are not hidden by budget refusals --------------------------------


def test_real_errors_win_over_budget_refusals(tmp_path):
    async def go():
        ex = Executor(Store(tmp_path), concurrency={"_local": 1, "openai": 1}, max_cost_usd=0.5)
        await asyncio.gather(ex.run_node(Spend(1.0), Num(x=1), CTX), return_exceptions=True)
        with pytest.raises(RuntimeError, match="boom"):
            await ex.map(Flaky(broken=2), [Num(x=2)], CTX)

    asyncio.run(go())


def test_single_call_budget_refusal_counts_itself(tmp_path):
    async def go():
        with pytest.raises(BudgetExceeded) as exc:
            await Executor(Store(tmp_path), max_cost_usd=0.5).run_node(Spend(1.0), Num(x=1), CTX)
        assert exc.value.remaining == 1

    asyncio.run(go())


# ---- findings 10/11: keys and store integrity --------------------------------------------


def test_colliding_mapping_keys_are_rejected():
    with pytest.raises(ValueError, match="collide"):
        canonical_json({1: "a", "1": "b"})


def test_store_requires_full_sha256_keys(tmp_path):
    with pytest.raises(ValueError, match="64"):
        Store(tmp_path).path("abc")


def test_corrupt_or_stale_cache_entry_is_recomputed(tmp_path):
    async def go():
        store = Store(tmp_path)
        node = Double()
        key = node.cache_key(Num(x=4), CTX)
        store.path(key).parent.mkdir(parents=True)
        store.path(key).write_text('{"not": "a Num"}')
        out = await Executor(store).run_node(node, Num(x=4), CTX)
        assert out == Num(x=8) and node.calls == 1
        assert store.get(key, Num) == Num(x=8)

    asyncio.run(go())


def _lineage(key: str) -> Lineage:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return Lineage(key=key, node="n", started_at=now, finished_at=now, cost_usd=0)


def test_concurrent_puts_of_one_key_are_safe(tmp_path):
    store = Store(tmp_path)
    errors: list[BaseException] = []

    def put(i: int) -> None:
        try:
            store.put(KEY, Num(x=7), _lineage(KEY))
        except BaseException as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=put, args=(i,)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.get(KEY, Num) == Num(x=7)
    assert [p for p in tmp_path.rglob("*.tmp")] == []
