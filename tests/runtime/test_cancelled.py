"""Calls cancelled mid-flight (an upstream timeout): counted at their reservation only when the
executor opts in (the live demo), never left reserved, never cached."""

import asyncio
import json

import pytest

from evalkit.cost import load_prices
from evalkit.pipeline.judge import judge_claims_live, live_call_estimate, verdict_schema
from evalkit.runtime import Executor, MemoryStore, RunContext, Store
from evalkit.runtime.canonical import to_jsonable
from evalkit.schemas import Case, Claim, RunConfig
from tests.pipeline.fakes import ScriptedAdapter
from tests.runtime.helpers import Num, Paid, Spend

CTX = RunContext(run_id="t", seed=0)


class Hang(Spend):
    async def run(self, inp: Num, ctx: RunContext) -> Paid:
        await asyncio.sleep(5)
        return await super().run(inp, ctx)


@pytest.mark.parametrize(("count", "expected"), [(True, 0.25), (False, 0.0)])
def test_a_cancelled_call_is_counted_only_when_asked(tmp_path, count, expected):
    ex = Executor(Store(tmp_path), max_cost_usd=1.0, count_cancelled=count)

    async def go():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ex.run_node(Hang(cost=0.25), Num(x=1), CTX), 0.05)

    asyncio.run(go())
    assert ex.spent_usd == pytest.approx(expected) and ex.in_flight_usd == 0
    assert ex.cancelled == 1
    assert not any(tmp_path.rglob("*.json"))  # nothing cached for a call that never finished


class SlowJudge(ScriptedAdapter):
    def __init__(self, reply):
        super().__init__(reply)
        self.seen: list = []

    async def complete(self, req):
        self.seen = [*self.seen, req]  # recorded on arrival: the call never returns
        await asyncio.sleep(5)
        return await super().complete(req)


@pytest.mark.parametrize("count", [True, False])
def test_a_cancelled_live_verdict_is_counted_only_when_asked(count):
    prices = load_prices()
    case = Case(id="c1", question="q?", context=("Wounds healed faster.",), source="t")
    claim = Claim(id="c1:m:0", text="Wounds healed faster.", cited=(1,), extractor_version="v")
    config = RunConfig(
        models=("claude-haiku-5-5",), judges=("claude-opus-5-5",), extractor="claude-haiku-5-5"
    )
    adapter = SlowJudge(
        lambda req: json.dumps(
            {"verdict": "supported", "evidence": [1], "quote": None, "rationale": "r"}
        )
    )
    ex = Executor(MemoryStore(), count_cancelled=count)

    async def go():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                judge_claims_live(
                    ex,
                    [(case, claim)],
                    "claude-opus-5-5",
                    config,
                    {"anthropic": adapter},
                    prices,
                    run_id="t",
                    max_output_tokens=800,
                ),
                0.05,
            )

    asyncio.run(go())
    est = live_call_estimate(
        case, claim, "claude-opus-5-5", config.judge_effort, prices["claude-opus-5-5"], 800
    )
    assert ex.spent_usd == pytest.approx(est if count else 0.0) and ex.in_flight_usd == 0
    assert to_jsonable(adapter.seen[0].json_schema) == verdict_schema()
    assert adapter.seen[0].max_output_tokens == 800
