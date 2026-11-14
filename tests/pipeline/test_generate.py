import asyncio
import json

import pytest

from evalkit.adapters import RawCompletion
from evalkit.cost import load_prices
from evalkit.pipeline.answer import SchemaError, answer_schema, parse_answer
from evalkit.pipeline.generate import (
    Generate,
    Validate,
    ValidateInput,
    generate_and_validate,
    generate_inputs,
    provider_for,
)
from evalkit.runtime import Executor, RunContext, Store
from evalkit.schemas import Case, RunConfig
from evalkit.tasks import builtin_task
from tests.pipeline.fakes import ScriptedAdapter

PRICES = load_prices()
TASK = builtin_task("pubmedqa")
CASES = [
    Case(
        id=f"pqa-{i}", question=f"Q{i}?", context=("s1.", "s2."), reference_label="yes", source="t"
    )
    for i in range(3)
]
GOOD = json.dumps({"label": "yes", "answer": "It does.", "citations": [1]})
CONFIG = RunConfig(
    models=("claude-sonnet-5-5", "gpt-6.1-sol"),
    judges=("claude-opus-5-5",),
    extractor="claude-haiku-5-5",
    concurrency={"anthropic": 2, "openai": 2},
)


def adapters(reply=lambda req: GOOD):
    a = ScriptedAdapter(reply)
    return {"anthropic": a, "openai": a}, a


def run(executor, ads, config=CONFIG):
    return asyncio.run(
        generate_and_validate(executor, CASES, TASK, config, ads, PRICES, run_id="r")
    )


# ---- GEN-01 routing, fan-out, cache ------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "provider"),
    [("claude-haiku-5-5", "anthropic"), ("gpt-6-luna", "openai"), ("o3-mini", "openai")],
)
def test_provider_routing(model, provider):
    assert provider_for(model) == provider
    assert provider_for("llama3.3", {"llama3.3": "http://localhost/v1"}) == "openai_compatible"
    with pytest.raises(ValueError, match="endpoints"):
        provider_for("mystery-7b")


def test_fan_out_over_models_and_cases_and_cache_on_rerun(tmp_path):
    ads, fake = adapters()
    out = run(Executor(Store(tmp_path)), ads)
    assert len(out) == 6 and {g.model for g in out} == {"claude-sonnet-5-5", "gpt-6.1-sol"}
    assert all(g.completion.answer is not None for g in out) and len(fake.requests) == 6
    ex2 = Executor(Store(tmp_path))
    run(ex2, ads)
    assert len(fake.requests) == 6 and ex2.calls == 0


def test_generate_uses_the_provider_pool():
    gen = Generate({}, PRICES)
    inp = generate_inputs(CASES[:1], TASK, CONFIG)[1]
    assert gen.pool(inp) == "openai"


# ---- GEN-02 validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"label": "perhaps", "answer": "x", "citations": [1]}',
        '{"label": "yes", "answer": "x"}',
        '{"label": "yes", "answer": "x", "citations": ["1"]}',
        '{"label": "yes", "answer": "x", "citations": [1], "extra": 1}',
        "[1, 2]",
    ],
)
def test_invalid_answers_are_schema_errors(raw):
    with pytest.raises(SchemaError):
        parse_answer(raw, TASK.labels)


def test_fenced_json_and_label_case_are_accepted():
    a = parse_answer('```json\n{"label": "YES", "answer": "x", "citations": [2]}\n```', TASK.labels)
    assert a.label == "yes" and a.citations == (2,)


def test_schema_failure_is_recorded_once_never_retried(tmp_path):
    ads, fake = adapters(lambda req: "I think yes")
    out = run(Executor(Store(tmp_path)), ads)
    assert all(g.completion.error == "schema" and g.completion.answer is None for g in out)
    assert len(fake.requests) == 6


# ---- GEN-03 error taxonomy ------------------------------------------------------------------


def test_transport_failures_are_recorded_and_not_cached(tmp_path):
    def reply(req):
        return ConnectionError("down") if req.model == "gpt-6.1-sol" else GOOD

    ads, _fake = adapters(reply)
    out = run(Executor(Store(tmp_path)), ads)
    errs = {g.model: g.completion.error for g in out}
    assert errs == {"claude-sonnet-5-5": None, "gpt-6.1-sol": "transport"}
    failed = [g.completion for g in out if g.completion.error == "transport"]
    assert failed[0].params["transport_error"] == "ConnectionError" and failed[0].cost_usd == 0
    ads2, fake2 = adapters()
    run(Executor(Store(tmp_path)), ads2)
    assert len(fake2.requests) == 3  # only the 3 failed calls are retried on rerun


def test_refusals_are_their_own_error_type(tmp_path):
    def reply(req):
        return RawCompletion(
            model=req.model,
            model_snapshot="",
            params={},
            raw_text="",
            tokens_in=10,
            tokens_out=0,
            latency_ms=5,
            cost_usd=0.0001,
            refusal=True,
            stop_reason="refusal",
        )

    out = run(Executor(Store(tmp_path)), adapters(reply)[0])
    assert {g.completion.error for g in out} == {"refusal"}


def test_map_settled_returns_every_item(tmp_path):
    from tests.runtime.helpers import Flaky, Num

    async def go():
        res = await Executor(Store(tmp_path)).map_settled(
            Flaky(broken=1), [Num(x=0), Num(x=1), Num(x=2)], RunContext(run_id="r")
        )
        return res

    res = asyncio.run(go())
    assert isinstance(res[1], RuntimeError) and res[0] == Num(x=100) and res[2] == Num(x=102)


# ---- GEN-04 structured output switch --------------------------------------------------------


@pytest.mark.parametrize("structured", [True, False])
def test_structured_output_is_all_or_nothing_and_recorded(tmp_path, structured):
    cfg = CONFIG.model_copy(update={"structured_output": structured})
    ads, fake = adapters()
    out = run(Executor(Store(tmp_path)), ads, cfg)
    sent = {r.json_schema is not None for r in fake.requests}
    assert sent == {structured}
    assert all(g.completion.params["structured_output"] is structured for g in out)
    if structured:
        from evalkit.runtime.canonical import to_jsonable

        assert to_jsonable(fake.requests[0].json_schema) == answer_schema(TASK.labels)


def test_structured_flag_and_effort_are_in_the_cache_key():
    gen = Generate({}, PRICES)
    ctx = RunContext(run_id="r")
    base = generate_inputs(CASES[:1], TASK, CONFIG)[0]
    flipped = base.model_copy(update={"structured_output": False})
    low = base.model_copy(update={"effort": "low"})
    keys = {gen.cache_key(i, ctx) for i in (base, flipped, low)}
    assert len(keys) == 3


def test_effort_is_explicit_per_model():
    cfg = CONFIG.model_copy(update={"efforts": {"gpt-6.1-sol": "high"}})
    inputs = generate_inputs(CASES[:1], TASK, cfg)
    assert {i.model: i.effort for i in inputs} == {
        "claude-sonnet-5-5": "medium",
        "gpt-6.1-sol": "high",
    }


def test_validate_is_pure_on_its_input():
    raw = RawCompletion(
        model="m",
        model_snapshot="",
        params={"effort": "low"},
        raw_text=GOOD,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
        cost_usd=0,
    )
    inp = ValidateInput(raw=raw, labels=TASK.labels, structured_output=True)
    a = asyncio.run(Validate().run(inp, RunContext(run_id="r")))
    b = asyncio.run(Validate().run(inp, RunContext(run_id="r2")))
    assert a == b and a.answer is not None and a.params["effort"] == "low"
