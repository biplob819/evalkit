import asyncio
import importlib.metadata
import warnings
from types import SimpleNamespace as NS

import pytest

from evalkit.adapters import BatchRequest, CompletionRequest, base, get_adapter
from evalkit.adapters.anthropic import AnthropicAdapter
from evalkit.adapters.openai import OpenAIAdapter, OpenAICompatibleAdapter
from evalkit.cost import load_prices
from tests.adapters.fakes import (
    FakeAnthropic,
    FakeOpenAI,
    StatusError,
    anthropic_message,
    openai_completion,
)

PRICES = load_prices()
SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
    "additionalProperties": False,
}


async def _nosleep(_s: float) -> None:
    await asyncio.sleep(0)


def req(model="sonnet", model_id="claude-sonnet-5-5", **kw):
    return CompletionRequest(
        model=model,
        model_id=model_id,
        prompt="Q?",
        max_output_tokens=800,
        params={"effort": "medium"},
        json_schema=SCHEMA,
        **kw,
    )


# ---- ADP-01 Anthropic -----------------------------------------------------------------


def test_anthropic_request_shape_has_effort_schema_and_no_sampling():
    params = AnthropicAdapter(client=FakeAnthropic(), prices=PRICES).build_params(req(system="S"))
    assert params["model"] == "claude-sonnet-5-5" and params["max_tokens"] == 800
    assert params["system"] == "S" and params["messages"] == [{"role": "user", "content": "Q?"}]
    assert params["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert not {"temperature", "top_p", "top_k", "fallbacks", "betas"} & set(params)


def test_sampling_params_are_refused_before_any_call():
    bad = CompletionRequest(
        model="s", model_id="claude-sonnet-5-5", prompt="q", params={"temperature": "0"}
    )
    with pytest.raises(ValueError, match="D-22"):
        AnthropicAdapter(client=FakeAnthropic(), prices=PRICES).build_params(bad)
    with pytest.raises(ValueError, match="D-22"):
        OpenAIAdapter(client=FakeOpenAI(), prices=PRICES).build_params(bad)


def test_anthropic_parse_splits_thinking_and_cache_and_costs_it():
    fake = FakeAnthropic(
        [
            anthropic_message(
                input_tokens=500, output_tokens=300, thinking=100, cache_read=1000, cache_write=200
            )
        ]
    )
    out = asyncio.run(AnthropicAdapter(client=fake, prices=PRICES).complete(req()))
    assert out.raw_text == '{"label":"yes"}' and out.model_snapshot == "claude-sonnet-5-5"
    assert (out.tokens_in, out.tokens_out, out.tokens_reasoning) == (1700, 200, 100)
    assert (out.tokens_cached, out.tokens_cache_write) == (1000, 200)
    expected = (500 * 2 + 1000 * 0.20 + 200 * 2.5 + 300 * 10) / 1e6
    assert out.cost_usd == pytest.approx(expected)
    assert out.request_id == "req_1" and not out.refusal


def test_anthropic_refusal_is_flagged_not_retried():
    fake = FakeAnthropic([anthropic_message(text="", stop="refusal")])
    out = asyncio.run(AnthropicAdapter(client=fake, prices=PRICES).complete(req()))
    assert out.refusal and out.stop_reason == "refusal" and len(fake.calls) == 1


# ---- ADP-02 OpenAI --------------------------------------------------------------------


def test_openai_request_shape():
    params = OpenAIAdapter(client=FakeOpenAI(), prices=PRICES).build_params(
        req(model="sol", model_id="gpt-6.1-sol", system="S")
    )
    assert params["messages"][0] == {"role": "system", "content": "S"}
    assert params["max_completion_tokens"] == 800 and params["reasoning_effort"] == "medium"
    assert params["response_format"]["json_schema"]["strict"] is True
    assert not {"temperature", "top_p", "max_tokens"} & set(params)


def test_openai_parse_reports_reasoning_separately():
    fake = FakeOpenAI([openai_completion(prompt=600, completion=500, reasoning=300, cached=100)])
    out = asyncio.run(
        OpenAIAdapter(client=fake, prices=PRICES).complete(req(model="sol", model_id="gpt-6.1-sol"))
    )
    assert (out.tokens_in, out.tokens_out, out.tokens_reasoning, out.tokens_cached) == (
        600,
        200,
        300,
        100,
    )
    assert out.cost_usd == pytest.approx((500 * 2 + 100 * 0.10 + 500 * 10) / 1e6)
    assert out.model_snapshot == "gpt-6.1-sol-2026-10-01"


def test_openai_refusal_flagged():
    fake = FakeOpenAI([openai_completion(text=None, refusal="I can't help with that")])
    out = asyncio.run(
        OpenAIAdapter(client=fake, prices=PRICES).complete(req(model="sol", model_id="gpt-6.1-sol"))
    )
    assert out.refusal and out.raw_text == ""


# ---- ADP-03 batches -------------------------------------------------------------------


def test_anthropic_batch_submit_collect_keyed_by_custom_id():
    fake = FakeAnthropic()
    ad = AnthropicAdapter(client=fake, prices=PRICES)
    reqs = {f"c{i}": req() for i in range(3)}
    bid = asyncio.run(
        ad.submit_batch([BatchRequest(custom_id=k, request=v) for k, v in reqs.items()])
    )
    assert bid == "msgbatch_1" and [r["custom_id"] for r in fake.created_batches[0]] == [
        "c0",
        "c1",
        "c2",
    ]
    assert asyncio.run(ad.collect_batch(bid, reqs)) is None
    fake.batch_status = "ended"
    fake.batch_items = [
        NS(custom_id="c2", result=NS(type="succeeded", message=anthropic_message())),
        NS(custom_id="c0", result=NS(type="errored", error=NS(type="invalid_request"))),
        NS(custom_id="stranger", result=NS(type="succeeded", message=anthropic_message())),
    ]
    collected = asyncio.run(ad.collect_batch(bid, reqs))
    assert collected is not None
    results = {r.custom_id: r for r in collected}
    assert set(results) == {"c0", "c1", "c2"}
    assert results["c2"].completion is not None and results["c2"].completion.batch
    assert results["c0"].error == "errored:invalid_request"
    assert results["c1"].error == "missing_from_results"


def test_batch_prices_are_discounted():
    fake = FakeAnthropic()
    ad = AnthropicAdapter(client=fake, prices=PRICES)
    live = ad.parse(anthropic_message(), req(), 10)
    batched = ad.parse(anthropic_message(), req(), 0, batch=True)
    assert batched.cost_usd == pytest.approx(live.cost_usd * 0.5)


def test_openai_batch_round_trip():
    fake = FakeOpenAI()
    ad = OpenAIAdapter(client=fake, prices=PRICES)
    reqs = {
        "a": req(model="sol", model_id="gpt-6.1-sol"),
        "b": req(model="sol", model_id="gpt-6.1-sol"),
    }
    bid = asyncio.run(
        ad.submit_batch([BatchRequest(custom_id=k, request=v) for k, v in reqs.items()])
    )
    assert fake.batch_args == ("file_in", "/v1/chat/completions", "24h")
    lines = fake.uploads[0].decode().splitlines()
    assert [__import__("json").loads(x)["custom_id"] for x in lines] == ["a", "b"]
    assert asyncio.run(ad.collect_batch(bid, reqs)) is None
    fake.batch_status = "completed"
    fake.output_lines = [
        {"custom_id": "b", "response": {"status_code": 200, "body": openai_completion()}},
        {"custom_id": "a", "response": {"status_code": 429, "body": {}}, "error": None},
    ]
    collected = asyncio.run(ad.collect_batch(bid, reqs))
    assert collected is not None
    results = {r.custom_id: r for r in collected}
    assert results["b"].completion is not None and results["a"].error == "http_429"


def test_failed_openai_batch_errors_every_request():
    fake = FakeOpenAI()
    fake.batch_status = "expired"
    results = asyncio.run(
        OpenAIAdapter(client=fake, prices=PRICES).collect_batch("b", {"x": req()})
    )
    assert results is not None and results[0].error == "batch_expired"


# ---- ADP-06 retries in adapters ---------------------------------------------------------


def test_adapter_retries_transient_errors_but_not_bad_requests():
    fake = FakeAnthropic([StatusError(529), StatusError(429), anthropic_message()])
    out = asyncio.run(AnthropicAdapter(client=fake, prices=PRICES, sleep=_nosleep).complete(req()))
    assert out.raw_text and len(fake.calls) == 3
    bad = FakeAnthropic([StatusError(400)])
    with pytest.raises(StatusError):
        asyncio.run(AnthropicAdapter(client=bad, prices=PRICES, sleep=_nosleep).complete(req()))
    assert len(bad.calls) == 1


# ---- ADP-08 OpenAI-compatible -----------------------------------------------------------


def test_openai_compatible_uses_base_url_and_costs_unknown_models_at_zero(monkeypatch):
    import openai

    seen = {}

    class Capture:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(openai, "AsyncOpenAI", Capture)
    monkeypatch.setenv("LOCAL_KEY", "abc")
    ad = OpenAICompatibleAdapter(
        base_url="http://localhost:11434/v1", api_key_env="LOCAL_KEY", prices=PRICES
    )
    ad.client  # noqa: B018 - force client creation
    assert seen == {"base_url": "http://localhost:11434/v1", "api_key": "abc", "max_retries": 0}
    ad._client = FakeOpenAI()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = asyncio.run(ad.complete(req(model="llama", model_id="llama3.3:70b")))
    assert out.cost_usd == 0 and any("costing it at $0" in str(w.message) for w in caught)


def test_unknown_model_on_paid_provider_is_an_error():
    with pytest.raises(KeyError, match="no price"):
        asyncio.run(
            OpenAIAdapter(client=FakeOpenAI(), prices=PRICES).complete(
                req(model="x", model_id="gpt-unknown")
            )
        )


# ---- ADP-09 registry and entry points ---------------------------------------------------


def test_builtin_adapters_are_registered():
    names = set(base.discover())
    assert {"anthropic", "openai", "openai_compatible"} <= names
    assert isinstance(
        get_adapter("anthropic", client=FakeAnthropic(), prices=PRICES), AnthropicAdapter
    )
    with pytest.raises(KeyError, match="available"):
        get_adapter("nope")


def test_entry_point_adapters_are_discovered(monkeypatch):
    class PluginAdapter(AnthropicAdapter):
        provider = "plugin"

    ep = NS(name="plugin_provider", load=lambda: PluginAdapter)
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [ep])
    monkeypatch.setattr(base, "_REGISTRY", dict(base._REGISTRY))
    assert base.discover()["plugin_provider"] is PluginAdapter
    bad = NS(name="broken", load=lambda: object)
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [bad])
    with pytest.warns(UserWarning, match="not a ModelAdapter"):
        assert "broken" not in base.discover()


# ---- safety: keys never travel with results --------------------------------------------


@pytest.mark.safety
def test_api_keys_never_appear_in_results_or_requests(monkeypatch):
    secret = "sk-ant-" + "Z" * 40  # secret-scan: allow
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    fake = FakeAnthropic()
    out = asyncio.run(AnthropicAdapter(client=fake, prices=PRICES).complete(req()))
    assert secret not in out.model_dump_json()
    assert secret not in repr(fake.calls)


# ---- N1.2 verifier regressions (L-17) ---------------------------------------------------


def test_expired_openai_batch_keeps_finished_results():
    fake = FakeOpenAI()
    fake.batch_status = "expired"
    fake.output_lines = [
        {"custom_id": "a", "response": {"status_code": 200, "body": openai_completion()}}
    ]
    reqs = {
        "a": req(model="sol", model_id="gpt-6.1-sol"),
        "b": req(model="sol", model_id="gpt-6.1-sol"),
    }
    collected = asyncio.run(OpenAIAdapter(client=fake, prices=PRICES).collect_batch("b1", reqs))
    assert collected is not None
    results = {r.custom_id: r for r in collected}
    assert results["a"].completion is not None and results["b"].error == "batch_expired"


def test_damaged_batch_line_reports_that_request_missing():
    fake = FakeOpenAI()
    fake.batch_status = "completed"
    fake.output_lines = [
        {"custom_id": "a", "response": {"status_code": 200, "body": openai_completion()}}
    ]

    async def content(file_id):
        return NS(
            text='{"custom_id": "a", "response": {"status_code": 200, "body": '
            + __import__("json").dumps(openai_completion())
            + '}}\n{"custom_id": "b", "resp'
        )

    fake.files.content = content  # type: ignore[method-assign]
    reqs = {
        "a": req(model="sol", model_id="gpt-6.1-sol"),
        "b": req(model="sol", model_id="gpt-6.1-sol"),
    }
    collected = asyncio.run(OpenAIAdapter(client=fake, prices=PRICES).collect_batch("b1", reqs))
    assert collected is not None
    assert {r.custom_id: r.error for r in collected} == {"a": None, "b": "missing_from_results"}


def test_anthropic_errored_item_keeps_the_real_sdk_error_type():
    from anthropic.types.messages import MessageBatchIndividualResponse

    item = MessageBatchIndividualResponse.model_validate(
        {
            "custom_id": "c0",
            "result": {
                "type": "errored",
                "error": {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "bad"},
                },
            },
        }
    )
    fake = FakeAnthropic()
    fake.batch_status = "ended"
    fake.batch_items = [item]
    collected = asyncio.run(
        AnthropicAdapter(client=fake, prices=PRICES).collect_batch("b", {"c0": req()})
    )
    assert collected is not None and collected[0].error == "errored:invalid_request_error"


def test_duplicate_custom_ids_are_refused():
    dup = [BatchRequest(custom_id="x", request=req()), BatchRequest(custom_id="x", request=req())]
    with pytest.raises(ValueError, match="duplicate custom_id"):
        asyncio.run(AnthropicAdapter(client=FakeAnthropic(), prices=PRICES).submit_batch(dup))
    with pytest.raises(ValueError, match="duplicate custom_id"):
        asyncio.run(OpenAIAdapter(client=FakeOpenAI(), prices=PRICES).submit_batch(dup))


def test_effort_must_be_explicit_except_for_local_servers():
    no_effort = CompletionRequest(model="s", model_id="claude-sonnet-5-5", prompt="q")
    with pytest.raises(ValueError, match="D-24"):
        AnthropicAdapter(client=FakeAnthropic(), prices=PRICES).build_params(no_effort)
    local = OpenAICompatibleAdapter(
        base_url="http://localhost:8000/v1", client=FakeOpenAI(), prices=PRICES
    )
    assert "reasoning_effort" not in local.build_params(no_effort)


def test_token_bucket_paces_every_adapter_call():
    acquired: list[float] = []

    class CountingBucket:
        async def acquire(self, tokens: float = 1.0) -> None:
            acquired.append(tokens)

    ad = AnthropicAdapter(client=FakeAnthropic(), prices=PRICES, bucket=CountingBucket())  # type: ignore[arg-type]
    asyncio.run(ad.complete(req()))
    asyncio.run(ad.complete(req()))
    assert acquired == [1.0, 1.0]


def test_broken_or_clashing_plugins_warn_and_builtins_survive(monkeypatch):
    def boom():
        raise ModuleNotFoundError("plugin_dep")

    eps = [NS(name="broken", load=boom), NS(name="openai", load=lambda: AnthropicAdapter)]
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps)
    monkeypatch.setattr(base, "_REGISTRY", dict(base._REGISTRY))
    with pytest.warns(UserWarning) as caught:
        adapter = get_adapter("anthropic", client=FakeAnthropic(), prices=PRICES)
    assert isinstance(adapter, AnthropicAdapter)
    messages = " ".join(str(w.message) for w in caught)
    assert "failed to load" in messages and "built in" in messages
    assert base.discover()["openai"] is OpenAIAdapter


def test_openai_compatible_validates_url_and_named_key(monkeypatch):
    with pytest.raises(ValueError, match="http"):
        OpenAICompatibleAdapter(base_url="not a url")
    monkeypatch.delenv("MISSING_KEY_VAR", raising=False)
    ad = OpenAICompatibleAdapter(base_url="https://example.test/v1", api_key_env="MISSING_KEY_VAR")
    with pytest.raises(ValueError, match="MISSING_KEY_VAR"):
        ad.client  # noqa: B018


def test_thinking_never_exceeds_output_tokens():
    out = AnthropicAdapter(client=FakeAnthropic(), prices=PRICES).parse(
        anthropic_message(output_tokens=50, thinking=80), req(), 1
    )
    assert out.tokens_reasoning == 50 and out.tokens_out == 0
