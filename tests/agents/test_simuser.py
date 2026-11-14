# ruff: noqa: E402  (imports follow the optional-dependency skip)
"""C3.3: the simulated user. Scripted adapters only: no network, no spend."""

import asyncio
import json
import re
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from evalkit.adapters import RawCompletion
from evalkit.agents.db import build_database
from evalkit.agents.loop import action_schema, run_trial
from evalkit.agents.simuser import (
    FALLBACK,
    REPLY_SCHEMA,
    SIM_PROMPT_VERSION,
    SIMULATORS,
    WITHHELD,
    AnswerGuard,
    SimulatedUser,
)
from evalkit.agents.tasks import TASKS_DIR, load_agent_tasks
from evalkit.cost import load_prices
from evalkit.runtime import Executor, Store
from evalkit.runtime.canonical import to_jsonable
from tests.pipeline.fakes import ScriptedAdapter

PRICES = load_prices()
HAIKU, LUNA = SIMULATORS


@pytest.fixture(scope="module")
def tasks():
    return {t.id: t for t in load_agent_tasks([TASKS_DIR])}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return build_database(tmp_path_factory.mktemp("src") / "riverbend.duckdb")


def says(text):
    return ScriptedAdapter(lambda req: json.dumps({"reply": text}))


def provider(model):
    return "anthropic" if model.startswith("claude") else "openai"


def sim_for(tmp_path, task, adapter, *, model=HAIKU, trial=1, store="store"):
    ex = Executor(Store(tmp_path / store))
    sim = SimulatedUser(ex, task, model, {provider(model): adapter}, PRICES, trial=trial, seed=7)
    return sim, ex


def ask(sim, question):
    return asyncio.run(sim(question))


# ---- SIM-01 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("model", SIMULATORS)
def test_both_simulators_answer_through_structured_output(tmp_path, tasks, model):
    adapter = says("I mean 2025.")
    sim, _ = sim_for(tmp_path, tasks["an-10"], adapter, model=model)
    assert ask(sim, "Which year?") == "I mean 2025."
    req = adapter.requests[0]
    assert to_jsonable(req.json_schema) == REPLY_SCHEMA and req.params == {"effort": "low"}
    assert sim.name.startswith(f"{model}@{SIM_PROMPT_VERSION}+effort=low+max=1024+prompt=")
    assert "+guard=" in sim.name


def test_replies_are_cached_and_trials_are_independent(tmp_path, tasks):
    task = tasks["an-10"]
    first, _ = sim_for(tmp_path, task, says("2025, please."))
    ask(first, "Which year?")
    replay_adapter = says("something else")
    replay, ex = sim_for(tmp_path, task, replay_adapter)
    assert ask(replay, "Which year?") == "2025, please."  # same trial and seed: replayed
    assert replay_adapter.requests == [] and ex.calls == 0
    other_adapter = says("2025.")
    other, _ = sim_for(tmp_path, task, other_adapter, trial=2)
    ask(other, "Which year?")
    assert len(other_adapter.requests) == 1  # another trial is a new sample


def test_history_is_part_of_the_question(tmp_path, tasks):
    adapter = says("Yes.")
    sim, _ = sim_for(tmp_path, tasks["an-14"], adapter)
    ask(sim, "Both years?")
    ask(sim, "Earliest first?")
    assert "The analyst asked: Both years?\nYou replied: Yes." in adapter.requests[1].prompt


# ---- SIM-02 ----------------------------------------------------------------------------------


def test_the_simulator_never_sees_the_data_or_the_answer(tmp_path, tasks):
    for task in tasks.values():
        if not task.simulated_user_intent:
            continue
        adapter = says("Use your judgement.")
        sim, _ = sim_for(tmp_path, task, adapter, store=f"s-{task.id}")
        ask(sim, "Anything else I should know?")
        req = adapter.requests[0]
        seen = f"{req.system}\n{req.prompt}"
        assert task.goal in seen and task.simulated_user_intent in seen
        assert task.gold_sql not in seen
        for row in task.gold_outcome.rows:  # type: ignore[union-attr]
            for v in row:
                if isinstance(v, str) or (isinstance(v, float) and v != int(v)):
                    assert str(v) not in seen, (task.id, v)


def test_the_user_may_restate_what_they_meant(tasks):
    guard = AnswerGuard(tasks["an-15"])  # intent: top 3 ... in 2025
    assert guard.leaks("Just the top 3 categories from 2025, most first.") is None
    assert guard.leaks("Three, please.") is None
    assert guard.leaks("Use calendar 2025, January 1 to December 31.") is None


LEAKS = [
    ("an-10", "Roughly 190k"),
    ("an-10", "About $190.7k"),
    ("an-10", "Around 191 thousand"),
    ("an-10", "0.19 million"),
    ("an-16", "Two hundred eighty-seven"),
    ("an-16", "about 290"),
    ("an-09", "Probably Finn, then Wren"),
    ("an-09", "about 7.2k"),
    ("an-13", "Roughly 1.19x, so it more than doubled"),
    ("an-11", "the Jacket and the Stove"),
    ("an-15", "backpack sales lead"),
    ("an-13", "West is growing fastest."),
    ("an-10", "$190,657"),
    ("an-10", "About 190,656.99"),
    ("an-10", "1.9e5"),
    ("an-16", "A couple hundred, I'd guess."),
    ("an-16", "a few hundred"),
    ("an-16", "hundreds, probably"),
    ("an-10", "about .19 million"),
    ("an-10", "$.19M"),
]
HONEST = [
    ("an-13", "Show the newest year against the one before."),
    ("an-13", "Northern or southeast stores? No preference."),
    ("an-12", "Calendar 2025, January 1 to December 31."),
    ("an-11", "Products with 45 or fewer units count."),
    ("an-12", "Include damaged items too."),
    ("an-09", "The top 5 customers by net revenue in 2025, with their ids."),
    ("an-13", "Compare 2025 with 2024 and round to one decimal."),
    ("an-14", "Both years, 2024 first."),
    ("an-16", "Active means at least one completed order in 2025."),
    ("an-12", "Count returns logged in 2025 by reason, most common first."),
    ("an-10", "I mean calendar 2025, January 1 to December 31, 2025."),
    ("an-12", "I mean calendar 2025, January 1 to December 31, 2025."),
    ("an-14", "Compare 2024,2025 please."),
    ("an-16", "From 2025-01-01 to 2025-12-31."),
    ("an-10", "Please report it in thousands."),
    ("an-10", "In millions is fine, rounded to the nearest thousand."),
    ("an-10", "1/1/2025 to 12/31/2025"),
    ("an-10", "Up to 2025-12-31T23:59."),
]


@pytest.mark.parametrize(("tid", "reply"), LEAKS)
def test_rounded_worded_and_partial_answers_are_withheld(tasks, tid, reply):
    assert AnswerGuard(tasks[tid]).leaks(reply), reply


@pytest.mark.parametrize(("tid", "reply"), HONEST)
def test_honest_clarifications_pass(tasks, tid, reply):
    assert AnswerGuard(tasks[tid]).leaks(reply) is None, reply


def test_the_guard_withholds_replies_that_state_the_answer(tmp_path, tasks):
    task = tasks["an-10"]  # one number: total web revenue in 2025
    total = task.gold_outcome.rows[0][0]  # type: ignore[union-attr]
    for leak in (f"It should be {total}.", f"About {round(total):,}.", f"{total:,.2f} I think"):
        sim, _ = sim_for(tmp_path, task, says(leak), store=f"g-{leak}")
        assert ask(sim, "What's the number?") == WITHHELD
        assert sim.guard_hits == 1 and sim.transcript[0]["model_reply"] == leak
        assert sim.transcript[0]["withheld"]
    ok, _ = sim_for(tmp_path, task, says("Last year means 2025."), store="g-ok")
    assert ask(ok, "Which year?") == "Last year means 2025." and ok.guard_hits == 0


def test_the_guard_catches_names_from_the_answer(tmp_path, tasks):
    task = tasks["an-15"]
    category = task.gold_outcome.rows[0][0]  # type: ignore[union-attr]
    sim, _ = sim_for(tmp_path, task, says(f"Probably {category.upper()} is top."))
    assert ask(sim, "Which is first?") == WITHHELD


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"reply": "   "}),
        json.dumps({"answer": "2025"}),
        json.dumps(["2025"]),
    ],
)
def test_unusable_replies_fall_back(tmp_path, tasks, raw):
    sim, _ = sim_for(tmp_path, tasks["an-10"], ScriptedAdapter(lambda req: raw))
    assert ask(sim, "Which year?") == FALLBACK


def test_a_refusal_falls_back(tmp_path, tasks):
    def refuse(req):
        return RawCompletion(
            model=req.model,
            model_snapshot="s",
            params={},
            raw_text="",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
            cost_usd=0.0,
            refusal=True,
        )

    sim, _ = sim_for(tmp_path, tasks["an-10"], ScriptedAdapter(refuse))
    assert ask(sim, "Which year?") == FALLBACK


def test_a_task_without_intent_needs_no_simulator(tmp_path, tasks):
    with pytest.raises(ValueError, match="no hidden intent"):
        sim_for(tmp_path, tasks["an-01"], says("x"))


# The 10 scripted B9 checks (the live script asks the same questions of the real models).
def _check_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "simuser_check.py"
    spec = importlib.util.spec_from_file_location("simuser_check", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ten_scripted_checks_offline_with_a_leaky_simulator(tmp_path, tasks):
    """A simulator that tries to leak on every reply never gets an answer through, and an
    honest one that restates the intent passes every check (the checks are fair)."""
    mod = _check_module()
    assert len(mod.CHECKS) >= 10
    for i, (tid, questions, expect) in enumerate(mod.CHECKS):
        task = tasks[tid]
        first = questions if isinstance(questions, str) else questions[0]
        for value in task.gold_outcome.rows[0]:  # type: ignore[union-attr]
            if isinstance(value, bool) or value in (None, ""):
                continue
            sim, _ = sim_for(tmp_path, task, says(f"Sure: {value}"), store=f"b9-{i}-{value}")
            if AnswerGuard(task).leaks(f"Sure: {value}"):
                assert ask(sim, first) == WITHHELD, (tid, value)
        honest, _ = sim_for(tmp_path, task, says(task.simulated_user_intent), store=f"ok-{i}")
        reply = ask(honest, first)
        assert reply == task.simulated_user_intent, (tid, reply)
        assert mod.strict_leak(honest, reply) is None, tid
        if expect:
            words = set(re.findall(r"[a-z0-9]+", reply.casefold()))
            assert any(t.casefold() in words for t in expect), tid


# ---- SIM-03 ----------------------------------------------------------------------------------


def action(tool, **args):
    full = {n: None for n in action_schema()["properties"] if n not in ("tool", "reason")}
    full.update(args)
    return json.dumps({"tool": tool, "reason": "t", **full})


def step_of(req):
    return req.prompt.count("<assistant>")  # one assistant turn per step taken


class Router(ScriptedAdapter):
    """One provider adapter serving the agent and a same-family simulator (by schema)."""

    def __init__(self, agent, sim):
        super().__init__(lambda req: "")
        self.agent, self.sim = agent, sim

    async def complete(self, req):
        target = self.sim if to_jsonable(req.json_schema) == REPLY_SCHEMA else self.agent
        return await target.complete(req)


def agent_script():
    replies = [
        action("ask_user", question="Which year is last year?"),
        action("final_answer", answer="done"),
    ]
    return ScriptedAdapter(lambda req: replies[min(step_of(req), 1)])


def run_with(tmp_path, db, task, model, agent):
    """The same agent (Sonnet) with either simulator; one store across calls."""
    haiku, luna = says("I mean 2025."), says("2025, the last full year.")
    adapters = {"anthropic": Router(agent, haiku), "openai": luna}

    async def go():
        ex = Executor(Store(tmp_path / "store"))
        sim = SimulatedUser(ex, task, model, adapters, PRICES, trial=1, seed=7)
        traj = await run_trial(
            ex,
            task,
            agent="claude-sonnet-5-5",
            trial=1,
            seed=7,
            db_path=db,
            workdir=tmp_path / "w",
            adapters=adapters,
            prices=PRICES,
            user=sim,
            simulator=sim.name,
        )
        return traj, sim

    return asyncio.run(go())


@pytest.mark.parametrize("model", SIMULATORS)
def test_trajectories_record_their_simulator(tmp_path, db, tasks, model):
    traj, sim = run_with(tmp_path, db, tasks["an-10"], model, agent_script())
    assert traj.simulator == sim.name and sim.name.startswith(f"{model}@{SIM_PROMPT_VERSION}")
    assert traj.simulator_cost_usd == pytest.approx(0.001)  # one simulator reply
    assert traj.steps[0].tool == "ask_user"
    assert traj.steps[0].result_summary == sim.transcript[0]["reply"]
    assert traj.stop_reason == "final_answer"


def test_different_simulators_never_share_a_trial(tmp_path, db, tasks):
    agent = agent_script()
    a, _ = run_with(tmp_path, db, tasks["an-10"], HAIKU, agent)
    b, _ = run_with(tmp_path, db, tasks["an-10"], LUNA, agent)
    assert a.simulator != b.simulator
    assert a.steps[0].result_summary != b.steps[0].result_summary
    # the second trial ran again: step 0 (identical prompt) reused its cached turn, step 1
    # saw a different reply and was a new call
    assert len(agent.requests) == 3
    run_with(tmp_path, db, tasks["an-10"], HAIKU, agent)
    assert len(agent.requests) == 3  # and each replays from its own cache entry


def test_simulator_settings_and_guard_are_part_of_its_identity(tmp_path, tasks):
    task = tasks["an-10"]
    low, _ = sim_for(tmp_path, task, says("x"))
    ex = Executor(Store(tmp_path / "store"))
    high = SimulatedUser(
        ex, task, HAIKU, {"anthropic": says("x")}, PRICES, trial=1, seed=7, effort="high"
    )
    assert low.name != high.name
    fixed = task.model_copy(
        update={"gold_outcome": task.gold_outcome.model_copy(update={"rows": ((1.5,),)})}
    )
    regold = SimulatedUser(ex, fixed, HAIKU, {"anthropic": says("x")}, PRICES, trial=1, seed=7)
    assert regold.name != low.name  # the guard protects different values: rerun, not replay


def test_run_trial_ties_the_simulator_to_its_trial(tmp_path, db, tasks):
    task = tasks["an-10"]
    sim, ex = sim_for(tmp_path, task, says("2025"), trial=2)
    kwargs: dict[str, Any] = dict(
        agent="claude-sonnet-5-5",
        seed=7,
        db_path=db,
        workdir=tmp_path / "w",
        adapters={"anthropic": says("x")},
        prices=PRICES,
        user=sim,
    )
    with pytest.raises(ValueError, match="another trial"):
        asyncio.run(run_trial(ex, task, trial=1, simulator=sim.name, **kwargs))
    with pytest.raises(ValueError, match="own name"):
        asyncio.run(run_trial(ex, task, trial=2, simulator="someone-else@v1", **kwargs))


def test_guard_rule_text_is_part_of_the_identity(tasks, monkeypatch):
    import evalkit.agents.simuser as simuser_mod

    before = AnswerGuard(tasks["an-10"]).fingerprint
    monkeypatch.setattr(simuser_mod, "_RULES_HASH", "changed-rules")
    assert AnswerGuard(tasks["an-10"]).fingerprint != before
