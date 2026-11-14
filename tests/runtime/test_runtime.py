import asyncio
import json
import os

import pytest

from evalkit.runtime import (
    BudgetExceeded,
    Executor,
    JsonlWriter,
    RunContext,
    Store,
    canonical_json,
)
from evalkit.runtime.store import Lineage
from tests.runtime.helpers import Double, Flaky, Num, Paid, Slow, Spend

CTX = RunContext(run_id="r1", model="m", params={"effort": "high"}, prompt_hash="p", seed=7)


# ---- RUN-01 cache key ------------------------------------------------------------------


def test_cache_key_ignores_dict_order_and_run_id():
    assert canonical_json({"b": 1, "a": [1.5, "é"]}) == '{"a":[1.5,"é"],"b":1}'
    node = Double()
    k1 = node.cache_key(Num(x=1), CTX)
    reordered = RunContext(
        run_id="r2", model="m", params={"effort": "high"}, prompt_hash="p", seed=7
    )
    assert node.cache_key(Num(x=1), reordered) == k1


@pytest.mark.parametrize(
    "change",
    [
        {"model": "other"},
        {"params": {"effort": "low"}},
        {"prompt_hash": "q"},
        {"seed": 8},
    ],
)
def test_cache_key_changes_with_every_dependency(change):
    node = Double()
    base = node.cache_key(Num(x=1), CTX)
    assert node.cache_key(Num(x=1), CTX.model_copy(update=change)) != base
    assert node.cache_key(Num(x=2), CTX) != base


def test_cache_key_changes_with_code_version_and_name():
    node = Double()
    base = node.cache_key(Num(x=1), CTX)

    class Bumped(Double):
        code_version = "2"

    class Renamed(Double):
        name = "double2"

    assert Bumped().cache_key(Num(x=1), CTX) != base
    assert Renamed().cache_key(Num(x=1), CTX) != base


def test_canonical_json_refuses_nan():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


# ---- RUN-02 store, RUN-03 lineage ----------------------------------------------------


def _lineage(key: str, parents: tuple[str, ...] = ()) -> Lineage:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return Lineage(key=key, node="n", parents=parents, started_at=now, finished_at=now, cost_usd=0)


def test_store_round_trip_and_lineage(tmp_path):
    store = Store(tmp_path)
    key = "ab" + "0" * 62
    store.put(key, Num(x=3), _lineage(key, ("cd" + "1" * 62,)))
    assert store.get(key, Num) == Num(x=3)
    assert store.path(key).parent.name == "ab"
    lin = store.lineage(key)
    assert lin is not None and lin.parents == ("cd" + "1" * 62,)


def test_interrupted_write_leaves_no_entry(tmp_path, monkeypatch):
    store = Store(tmp_path)
    key = "ef" + "2" * 62

    def fail(*_a, **_k):
        raise OSError("disk pulled")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.put(key, Num(x=1), _lineage(key))
    monkeypatch.undo()
    assert not store.has(key)
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == [], leftovers


def test_store_rejects_non_keys(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path).path("../../etc/passwd")


def test_lineage_links_child_to_parent(tmp_path):
    async def go():
        ex = Executor(Store(tmp_path))
        d = Double()
        a = await ex.run_node(d, Num(x=1), CTX)
        parent_key = d.cache_key(Num(x=1), CTX)
        await ex.run_node(d, a, CTX, parents=[parent_key])
        child_key = d.cache_key(a, CTX)
        lin = ex.store.lineage(child_key)
        assert lin is not None and lin.parents == (parent_key,)

    asyncio.run(go())


# ---- RUN-04 fan-out / concurrency ----------------------------------------------------


def test_map_respects_provider_concurrency_and_keeps_order(tmp_path):
    async def go():
        ex = Executor(Store(tmp_path), concurrency={"anthropic": 3})
        node = Slow()
        out = await ex.map(node, [Num(x=i) for i in range(12)], CTX)
        assert [o.x for o in out] == list(range(12))
        assert node.peak == 3

    asyncio.run(go())


# ---- RUN-05 resume -------------------------------------------------------------------


def test_rerun_after_failure_only_runs_what_is_missing(tmp_path):
    async def go():
        store = Store(tmp_path)
        node = Flaky(broken=5)
        with pytest.raises(RuntimeError, match="boom on 5"):
            await Executor(store).map(node, [Num(x=i) for i in range(10)], CTX)
        assert node.calls == 10
        node.broken = None
        ex2 = Executor(store)
        out = await ex2.map(node, [Num(x=i) for i in range(10)], CTX)
        assert [o.x for o in out] == [i + 100 for i in range(10)]
        assert ex2.calls == 1 and ex2.cache_hits == 9

    asyncio.run(go())


def test_batch_id_meta_survives_for_resume(tmp_path):
    store = Store(tmp_path)
    key = "aa" + "3" * 62
    store.put_meta(key, "batch", {"batch_id": "msgbatch_1", "provider": "anthropic"})
    assert Store(tmp_path).get_meta(key, "batch") == {
        "batch_id": "msgbatch_1",
        "provider": "anthropic",
    }
    assert store.get_meta(key, "other") is None


# ---- RUN-06 single writer -------------------------------------------------------------


def test_single_writer_produces_whole_lines(tmp_path):
    path = tmp_path / "records.jsonl"

    async def go():
        async with JsonlWriter(path) as w:
            await asyncio.gather(*(w.write({"i": i, "pad": "x" * 500}) for i in range(100)))
        return w.lines_written

    assert asyncio.run(go()) == 100
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    assert sorted(json.loads(line)["i"] for line in lines) == list(range(100))


def test_run_log_records_cache_hits_and_costs(tmp_path):
    log_path = tmp_path / "run.log"

    async def go():
        async with JsonlWriter(log_path) as log:
            ex = Executor(Store(tmp_path / "s"), log=log)
            await ex.run_node(Spend(0.5), Num(x=1), CTX)
            await ex.run_node(Spend(0.5), Num(x=1), CTX)

    asyncio.run(go())
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [e["cache_hit"] for e in entries] == [False, True]
    assert entries[0]["cost_usd"] == 0.5 and {"node", "key", "latency_ms"} <= set(entries[0])


def test_wrong_output_type_is_an_error(tmp_path):
    class Liar(Double):
        name = "liar"

        async def run(self, inp, ctx):  # type: ignore[override]
            return Paid(y=1, cost_usd=0)

    with pytest.raises(TypeError, match="not Num"):
        asyncio.run(Executor(Store(tmp_path)).run_node(Liar(), Num(x=1), CTX))


# ---- RUN-08 spend cap ----------------------------------------------------------------


def test_spend_cap_stops_before_passing_the_cap_and_resumes(tmp_path):
    async def go():
        store = Store(tmp_path)
        node = Spend(1.0)
        ex = Executor(store, concurrency={"openai": 1}, max_cost_usd=3.5)
        with pytest.raises(BudgetExceeded) as exc:
            await ex.map(node, [Num(x=i) for i in range(10)], CTX)
        assert node.calls == 3 and ex.spent_usd == 3.0
        assert exc.value.remaining == 7 and exc.value.spent <= 3.5
        ex2 = Executor(store, concurrency={"openai": 1}, max_cost_usd=100)
        out = await ex2.map(node, [Num(x=i) for i in range(10)], CTX)
        assert len(out) == 10 and ex2.cache_hits == 3 and ex2.calls == 7

    asyncio.run(go())


def test_spend_cap_counts_in_flight_estimates(tmp_path):
    async def go():
        ex = Executor(Store(tmp_path), concurrency={"openai": 10}, max_cost_usd=2.5)
        with pytest.raises(BudgetExceeded):
            await ex.map(Spend(1.0), [Num(x=i) for i in range(10)], CTX)
        assert ex.spent_usd <= 2.5

    asyncio.run(go())


def test_cache_hits_cost_nothing_against_the_cap(tmp_path):
    async def go():
        store = Store(tmp_path)
        await Executor(store).map(Spend(1.0), [Num(x=i) for i in range(3)], CTX)
        ex = Executor(store, max_cost_usd=0.5)
        out = await ex.map(Spend(1.0), [Num(x=i) for i in range(3)], CTX)
        assert len(out) == 3 and ex.spent_usd == 0

    asyncio.run(go())
