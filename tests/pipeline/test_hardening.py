"""Regression tests for the A2.1 verifier findings (lesson L-21)."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from evalkit.pipeline.answer import SchemaError, parse_answer
from evalkit.pipeline.generate import generate_and_validate, provider_for
from evalkit.runtime import BudgetExceeded, Executor, Store
from evalkit.smoke import assumptions_from, write_measurements
from tests.adapters.fakes import StatusError
from tests.pipeline.fakes import ScriptedAdapter
from tests.pipeline.test_generate import CASES, CONFIG, GOOD, PRICES, TASK

ROOT = Path(__file__).resolve().parents[2]


def run(executor, reply):
    a = ScriptedAdapter(reply)
    return asyncio.run(
        generate_and_validate(
            executor, CASES, TASK, CONFIG, {"anthropic": a, "openai": a}, PRICES, run_id="r"
        )
    ), a


# ---- finding 1: the spend cap stops the run -----------------------------------------------


def test_budget_exhaustion_stops_the_run_instead_of_recording_failures(tmp_path):
    with pytest.raises(BudgetExceeded) as exc:
        run(Executor(Store(tmp_path), max_cost_usd=0.0001), lambda req: GOOD)
    assert exc.value.remaining == 6


# ---- finding 2: only transient provider errors become "transport" -------------------------


@pytest.mark.parametrize(
    "error",
    [
        ValueError("bad params"),
        KeyError("no adapter"),
        StatusError(401),
        StatusError(400),
        StatusError(404),
    ],
)
def test_local_and_config_errors_stop_the_run(tmp_path, error):
    with pytest.raises(type(error)):
        run(Executor(Store(tmp_path)), lambda req: error)


@pytest.mark.parametrize(
    "error", [StatusError(529), StatusError(429), ConnectionError("x"), TimeoutError()]
)
def test_transient_provider_errors_are_recorded_as_transport(tmp_path, error):
    out, _ = run(Executor(Store(tmp_path)), lambda req: error)
    assert {g.completion.error for g in out} == {"transport"}


# ---- finding 3: the whole output must be one JSON object ----------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"label": "yes", "answer": "x", "citations": [1]} hope this helps',
        'I think maybe. The format is {"label": "yes", "answer": "x", "citations": [1]}',
        'Sure!\n```json\n{"label": "yes", "answer": "x", "citations": [1]}\n```',
    ],
)
def test_prose_around_json_is_a_schema_error(raw):
    with pytest.raises(SchemaError):
        parse_answer(raw, TASK.labels)


def test_a_single_fenced_object_is_still_accepted():
    a = parse_answer('```json\n{"label": "no", "answer": "x", "citations": [1]}\n```', TASK.labels)
    assert a.label == "no"


# ---- finding 4: .env parsing ------------------------------------------------------------


def _smoke_module():
    spec = importlib.util.spec_from_file_location("smoke_run", ROOT / "scripts" / "smoke_run.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("line", "parsed"),
    [
        ("export ANTHROPIC_API_KEY=abc", ("ANTHROPIC_API_KEY", "abc")),
        ('OPENAI_API_KEY="x=y" # personal', ("OPENAI_API_KEY", "x=y")),
        ("KEY='single' ", ("KEY", "single")),
        ("KEY=plain # note", ("KEY", "plain")),
        ("# comment", None),
        ("", None),
    ],
)
def test_dotenv_lines(line, parsed):
    assert _smoke_module().parse_dotenv_line(line) == parsed


# ---- findings 5 and 6: measurements, routing, concurrency ---------------------------------


def test_models_with_only_schema_errors_still_feed_token_assumptions(tmp_path):
    path = tmp_path / "m.json"
    write_measurements(
        path,
        {
            "a": {
                "n": 2,
                "errors": {"schema": 2},
                "avg_tokens_in": 500.0,
                "avg_tokens_out": 40.0,
                "avg_tokens_reasoning": 100.0,
                "avg_latency_ms": 1.0,
                "total_cost_usd": 0.0,
            },
            "b": {
                "n": 2,
                "errors": {"transport": 2},
                "avg_tokens_in": 0.0,
                "avg_tokens_out": 0.0,
                "avg_tokens_reasoning": 0.0,
                "avg_latency_ms": 0.0,
                "total_cost_usd": 0.0,
            },
        },
        {},
    )
    a = assumptions_from(path)
    assert (a.t_in, a.t_out, a.t_reason) == (500, 40, 100)
    assert json.loads(path.read_text(encoding="utf-8"))["models"]["b"]["errors"] == {"transport": 2}


@pytest.mark.parametrize("model", ["o1x", "gpt5", "oracle-7b", "opus"])
def test_openai_routing_is_not_a_loose_prefix(model):
    with pytest.raises(ValueError):
        provider_for(model)
    assert provider_for("o10-mini") == "openai" and provider_for("o3") == "openai"


def test_provider_concurrency_is_enforced_during_generation(tmp_path):
    active = {"anthropic": 0, "openai": 0}
    peak = {"anthropic": 0, "openai": 0}

    class Slow(ScriptedAdapter):
        async def complete(self, req):
            prov = "anthropic" if req.model_id.startswith("claude") else "openai"
            active[prov] += 1
            peak[prov] = max(peak[prov], active[prov])
            await asyncio.sleep(0.01)
            active[prov] -= 1
            return await super().complete(req)

    a = Slow(lambda req: GOOD)
    cfg = CONFIG.model_copy(update={"concurrency": {"anthropic": 1, "openai": 2}})
    asyncio.run(
        generate_and_validate(
            Executor(Store(tmp_path), concurrency=dict(cfg.concurrency)),
            CASES,
            TASK,
            cfg,
            {"anthropic": a, "openai": a},
            PRICES,
            run_id="r",
        )
    )
    assert peak == {"anthropic": 1, "openai": 2}


def test_transport_rerun_is_deterministic(tmp_path):
    first, _ = run(Executor(Store(tmp_path)), lambda req: ConnectionError("x"))
    second, _ = run(Executor(Store(tmp_path / "other")), lambda req: ConnectionError("x"))
    assert [g.completion for g in first] == [g.completion for g in second]
