# ruff: noqa: E402  (imports follow the optional-dependency skip)
"""C3.4: trajectory scorers, the agent aggregate, `evalkit agent-run`. No network, no spend."""

import asyncio
import json
import re
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from evalkit.adapters import CompletionRequest
from evalkit.adapters.anthropic import AnthropicAdapter
from evalkit.agents import runner as R
from evalkit.agents.aggregate import METRICS, Plan, summarize
from evalkit.agents.loop import action_schema, prompt_parts
from evalkit.agents.score import min_steps, score_trajectory
from evalkit.agents.simuser import REPLY_SCHEMA
from evalkit.agents.tasks import TASKS_DIR, load_agent_tasks
from evalkit.cost import load_prices
from evalkit.runtime import BudgetExceeded
from evalkit.runtime.canonical import to_jsonable
from evalkit.schemas import AgentScore, Step, Trajectory
from tests.pipeline.fakes import ScriptedAdapter

PRICES = load_prices()


@pytest.fixture(scope="module")
def tasks():
    return {t.id: t for t in load_agent_tasks([TASKS_DIR])}


def step(i, tool, args=None, error=None, violation=False, kind: Any = "tool_call"):
    return Step(
        index=i,
        tool=tool,
        arguments=args or {},
        result_summary="",
        error=error,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
        kind=kind,
        violation=violation,
    )


def traj(
    task_id, steps, answer="", stop: Any = "final_answer", cost=0.01, agent="a", sim=None, trial=1
):
    return Trajectory(
        task_id=task_id,
        agent=agent,
        simulator=sim,
        trial=trial,
        seed=7,
        steps=tuple(steps),
        final_answer=answer,
        sandbox_reset_ok=stop != "void",
        cost_usd=cost if stop != "void" else 0.0,
        stop_reason=stop,
    )


def gold_answer(task):
    g = task.gold_outcome
    return {"answer": "done", "columns": list(g.columns), "rows": [list(r) for r in g.rows]}


# ---- ASC-01 scorers ---------------------------------------------------------------------------


def test_correct_analytics_answer_succeeds(tasks):
    t = tasks["an-01"]
    s = score_trajectory(
        t,
        traj(
            "an-01",
            [step(0, "run_sql", {"query": "x"}), step(1, "final_answer", gold_answer(t))],
            "done",
        ),
    )
    assert s.success and s.steps_over_gold == 0 and s.valid_tool_calls == 1.0
    assert s.recovered_from_error is None and s.detail == "" and s.split == "public"


@pytest.mark.parametrize(
    ("mangle", "reason"),
    [
        (lambda a: {**a, "rows": [[0.0] * len(a["columns"])]}, "wrong result"),
        (lambda a: {"answer": "done"}, "no result table"),
        (lambda a: {**a, "rows": [[1, 2, 3, 4, 5]]}, "malformed"),
    ],
)
def test_wrong_or_missing_tables_fail_with_a_reason(tasks, mangle, reason):
    t = tasks["an-01"]
    s = score_trajectory(t, traj("an-01", [step(0, "final_answer", mangle(gold_answer(t)))]))
    assert not s.success and reason in s.detail


def test_running_out_of_steps_fails(tasks):
    s = score_trajectory(
        tasks["an-01"], traj("an-01", [step(0, "list_tables")], stop="step_budget")
    )
    assert not s.success and "step_budget" in s.detail and s.steps_over_gold == -1


def test_attack_success_counts_as_a_violation(tasks):
    t = tasks["at-01"]
    canary = t.gold_outcome.canaries[0]
    leaked = score_trajectory(
        t, traj("at-01", [step(0, "final_answer", {"answer": canary})], canary)
    )
    assert not leaked.success and leaked.policy_violations == 1 and "succeeded" in leaked.detail
    clean = score_trajectory(
        t,
        traj(
            "at-01",
            [step(0, "run_sql", {"query": "x"}), step(1, "final_answer", {"answer": "ok"})],
            "ok",
        ),
    )
    assert clean.success and clean.policy_violations == 0


def test_refused_calls_lower_valid_tool_calls_and_count_as_violations(tasks):
    t = tasks["at-02"]
    s = score_trajectory(
        t,
        traj(
            "at-02",
            [
                step(
                    0, "send_email", {"to": "x", "body": "y"}, error="not allowed", violation=True
                ),
                step(1, "final_answer", {"answer": "ok"}),
            ],
            "ok",
        ),
    )
    assert s.valid_tool_calls == 0.5 and s.policy_violations == 1
    assert not s.success  # a blocked forbidden call is a partial attack outcome


def test_recovery_after_a_tool_error(tasks):
    t = tasks["an-01"]
    s = score_trajectory(
        t,
        traj(
            "an-01",
            [
                step(0, "run_sql", {"query": "bad"}, error="query failed: x"),
                step(1, "run_sql", {"query": "good"}),
                step(2, "final_answer", gold_answer(t)),
            ],
            "done",
        ),
    )
    assert s.recovered_from_error is True and s.steps_over_gold == 1


def test_void_trials_are_marked_not_failed(tasks):
    s = score_trajectory(tasks["an-01"], traj("an-01", [], stop="void"))
    assert s.error == "void" and not s.success


def test_minimum_steps_by_task_kind(tasks):
    assert min_steps(tasks["an-01"]) == 2
    assert min_steps(tasks["an-10"]) == 3  # ambiguous: ask first
    assert min_steps(tasks["at-01"]) == 2


def test_cost_is_the_agents_own(tasks):
    t = tasks["an-01"]
    tr = traj("an-01", [step(0, "final_answer", gold_answer(t))], cost=0.02)
    tr = tr.model_copy(update={"simulator_cost_usd": 0.005})
    s = score_trajectory(t, tr)
    assert s.cost_usd == 0.02 and s.simulator_cost_usd == 0.005


# ---- ASC-02/03/04 aggregate -------------------------------------------------------------------


def sc(task, agent, trial, success, *, split: Any = "public", sim=None, cost=0.01, void=False):
    return AgentScore(
        task_id=task,
        agent=agent,
        simulator=sim,
        trial=trial,
        success=success,
        valid_tool_calls=1.0,
        policy_violations=0,
        steps_over_gold=0,
        cost_usd=cost,
        split=split,
        error="void" if void else None,
    )


def suite(pattern_a, pattern_b, n_tasks=20, split: Any = "public"):
    out = []
    for t in range(n_tasks):
        for k in range(3):
            out.append(sc(f"t{t:02d}", "A", k + 1, pattern_a(t, k), split=split))
            out.append(sc(f"t{t:02d}", "B", k + 1, pattern_b(t, k), split=split))
    return out


def test_pass_at_k_and_pass_hat_k(tasks):
    # task 0: 3/3, task 1: 1/3, task 2: 0/3
    scores = [sc("t0", "A", k, True) for k in (1, 2, 3)]
    scores += [sc("t1", "A", k, k == 1) for k in (1, 2, 3)]
    scores += [sc("t2", "A", k, False) for k in (1, 2, 3)]
    row = summarize(scores, heldout_present=False).rows[0]
    m = row.metrics
    assert m["success"].point == pytest.approx(4 / 9)
    assert m["pass_at_k"].point == pytest.approx(2 / 3)
    assert m["pass_hat_k"].point == pytest.approx(1 / 3)
    assert row.k == 3 and row.n_tasks == 3 and row.n_trials == 9
    # only 3 tasks: some resamples solve nothing, so cost per success has no stable interval
    assert row.cost_per_success is None and row.cost_unsolved_share > 0.05
    for name in METRICS:
        assert m[name].lo <= m[name].point <= m[name].hi
        assert row.widths[name] == pytest.approx(m[name].hi - m[name].lo)


def test_a_real_gap_is_separated_and_noise_is_a_tie():
    strong = summarize(suite(lambda t, k: True, lambda t, k: t % 4 == 0), heldout_present=False)
    sep = {s.metric: s for s in strong.separation}
    assert sep["success"].separated and sep["success"].diff.lo > 0
    noisy = summarize(
        suite(lambda t, k: (t + k) % 2 == 0, lambda t, k: (t + k + 1) % 2 == 0),
        heldout_present=False,
    )
    assert not any(s.separated for s in noisy.separation)


def test_two_level_bootstrap_resamples_tasks_then_trials():
    plan = Plan(n_tasks=4, max_trials=3, seed="x", n=200)
    tasks_ = [[sc(f"t{t}", "A", k, k == 1) for k in (1, 2, 3)] for t in range(4)]
    samples = plan.samples(tasks_)
    assert len(samples) == 200 and all(len(s) == 4 for s in samples)
    assert all(len(ts) == 3 for s in samples for ts in s)
    trial_ids = {tuple(t.trial for t in ts) for s in samples for ts in s}
    assert len(trial_ids) > 3  # trials are resampled within tasks, not fixed
    again = Plan(n_tasks=4, max_trials=3, seed="x", n=200).samples(tasks_)
    assert [[[t.trial for t in ts] for ts in s] for s in again] == [
        [[t.trial for t in ts] for ts in s] for s in samples
    ]  # seeded: reproducible


def test_void_trials_are_excluded_and_counted():
    scores = [sc("t0", "A", 1, True), sc("t0", "A", 2, False, void=True), sc("t0", "A", 3, True)]
    row = summarize(scores, heldout_present=False).rows[0]
    assert row.n_void == 1 and row.n_trials == 2 and row.metrics["success"].point == 1.0


def test_simulator_views_share_unambiguous_tasks():
    scores = [sc("plain", "A", 1, True)]
    scores += [sc("amb", "A", 1, True, sim="haiku@v"), sc("amb", "A", 1, False, sim="luna@v")]
    s = summarize(scores, heldout_present=False)
    by_view = {r.view: r for r in s.rows}
    assert set(by_view) == {"haiku@v", "luna@v"}
    assert by_view["haiku@v"].metrics["success"].point == 1.0
    assert by_view["luna@v"].metrics["success"].point == 0.5
    assert all(r.n_tasks == 2 for r in s.rows)


def test_heldout_is_scored_apart_with_a_gap():
    pub = suite(lambda t, k: True, lambda t, k: True)
    held = suite(lambda t, k: t % 2 == 0, lambda t, k: True, n_tasks=8, split="heldout")
    s = summarize(pub + held, heldout_present=True)
    splits = {(r.agent, r.split) for r in s.rows}
    assert ("A", "heldout") in splits and ("A", "public") in splits
    gap = next(g for g in s.heldout_gap if g.agent == "A" and g.metric == "success")
    assert gap.diff.point == pytest.approx(0.5) and gap.diff.lo > 0
    none = summarize(pub, heldout_present=False)
    assert not none.heldout_gap and "not available" in none.note


# ---- prompt caching ---------------------------------------------------------------------------


def test_each_turn_prompt_extends_the_previous_one():
    a = prompt_parts("goal", 5, [])
    b = prompt_parts("goal", 5, [({"tool": "list_tables"}, "stores")])
    c = prompt_parts("goal", 5, [({"tool": "list_tables"}, "stores"), ({"tool": "x"}, "y")])
    assert b[: len(a)] == a and c[: len(b)] == b  # cacheable prefixes, newest step last
    assert "".join(c).endswith("(3 steps left)\n\n")


def test_anthropic_requests_cache_the_growing_history():
    parts = ("head\n", "step 0\n", "step 1\n")
    req = CompletionRequest(
        model="m",
        model_id="claude-sonnet-5-5",
        prompt="".join(parts),
        prompt_parts=parts,
        cache=True,
        params={"effort": "low"},
    )
    params = AnthropicAdapter(client=object(), prices=PRICES).build_params(req)
    blocks = params["messages"][0]["content"]
    assert [b["text"] for b in blocks] == list(parts)
    assert params["cache_control"] == {"type": "ephemeral"}
    with pytest.raises(ValueError, match="joined"):
        CompletionRequest(model="m", model_id="x", prompt="other", prompt_parts=parts)


# ---- ASC-05 agent-run -------------------------------------------------------------------------


def step_of(req):
    return len(re.findall(r"^Step \d+ result:", req.prompt, re.M))


class Bot(ScriptedAdapter):
    """A scripted agent that solves analytics tasks from their gold SQL and answers attacks
    safely; simulator calls get a clarifying reply."""

    def __init__(self, tasks):
        super().__init__(self.reply)
        self.tasks = tasks

    def reply(self, req: CompletionRequest) -> str:
        if to_jsonable(req.json_schema) == REPLY_SCHEMA:
            return json.dumps({"reply": "Use 2025."})
        task = next(t for t in self.tasks.values() if f"Task: {t.goal}\n" in req.prompt)
        full = {n: None for n in action_schema()["properties"] if n not in ("tool", "reason")}
        if task.gold_sql:
            g = task.gold_outcome
            act = {
                "tool": "final_answer",
                "answer": "done",
                "columns": list(g.columns),
                "rows": [list(r) for r in g.rows],
            }
        else:
            act = {"tool": "final_answer", "answer": "A routine request."}
        return json.dumps({**full, "reason": "t", **act})


def plan_for(tmp_path, only=("an-01", "an-10", "at-01"), **kw: Any):
    return R.plan_agent_run(
        agents=("claude-sonnet-5-5", "gpt-6.1-sol"),
        k=2,
        out=tmp_path / "runs",
        store_dir=tmp_path / ".evalkit" / "store",
        heldout_dir=None,
        only=only,
        **kw,
    )


def test_plan_counts_trials_and_simulator_runs(tmp_path):
    plan = plan_for(tmp_path)
    # 2 agents x (2 plain tasks x k=2 + 1 ambiguous task x 2 simulators x k=2)
    assert len(plan.jobs) == 2 * (2 * 2 + 1 * 2 * 2) and not plan.heldout_present
    costs = R.estimate_usd(plan, PRICES)
    assert set(costs) == {"claude-sonnet-5-5", "gpt-6.1-sol", "claude-haiku-5-5", "gpt-6-luna"}
    assert plan.run_id.startswith("a-") and plan_for(tmp_path).run_id == plan.run_id


def test_agent_run_writes_the_folder_and_replays_free(tmp_path, tasks):
    plan = plan_for(tmp_path)
    bots = {"anthropic": Bot(tasks), "openai": Bot(tasks)}
    out = asyncio.run(R.execute_agent_run(plan, bots, PRICES, max_cost_usd=5.0))
    folder = plan.run_dir
    for name in (
        "agent_run.json",
        "trajectories.jsonl",
        "scores.jsonl",
        "agent_summary.json",
        "report.html",
    ):
        assert (folder / name).is_file(), name
    assert all(s.success for s in out.scores) and len(out.scores) == len(plan.jobs)
    html = (folder / "report.html").read_text(encoding="utf-8")
    assert "Which gaps are real" in html and "Held-out versus public" in html
    m = R.read_agent_manifest(folder)
    assert m and m.finished_at and m.spent_usd == pytest.approx(out.spent_usd)
    calls = sum(len(b.requests) for b in bots.values())
    again = asyncio.run(R.execute_agent_run(plan, bots, PRICES, max_cost_usd=5.0))
    assert sum(len(b.requests) for b in bots.values()) == calls and again.spent_usd == 0


class CostlyBot(Bot):
    def reply(self, req: CompletionRequest):  # type: ignore[override]
        from evalkit.adapters import RawCompletion

        return RawCompletion(
            model=req.model,
            model_snapshot="s",
            params=dict(req.params),
            raw_text=super().reply(req),
            tokens_in=10,
            tokens_out=10,
            latency_ms=1,
            cost_usd=0.05,
        )


def test_the_cap_is_a_total_across_resumes(tmp_path, tasks):
    plan = plan_for(tmp_path)
    bots = {"anthropic": CostlyBot(tasks), "openai": CostlyBot(tasks)}

    def calls():
        return sum(len(b.requests) for b in bots.values())

    with pytest.raises(BudgetExceeded):  # each call costs $0.05: a couple finish, then stop
        asyncio.run(R.execute_agent_run(plan, bots, PRICES, max_cost_usd=0.12))
    first = calls()
    m = R.read_agent_manifest(plan.run_dir)
    assert 0 < first < len(plan.jobs) and m and m.spent_usd == pytest.approx(0.05 * first)
    with pytest.raises(BudgetExceeded):  # same cap again: earlier spend counts, no new calls
        asyncio.run(R.execute_agent_run(plan, bots, PRICES, max_cost_usd=0.12))
    assert calls() == first
    out = asyncio.run(R.execute_agent_run(plan, bots, PRICES, max_cost_usd=5.0))
    assert len(out.scores) == len(plan.jobs)
    # one call per distinct turn overall: finished trials never rerun (an agent that never asks
    # has identical first turns under both simulators, which the turn cache shares)
    assert calls() == len({(j.agent, j.task.id, j.trial) for j in plan.jobs})


def test_heldout_mismatch_stops_before_any_call(tmp_path):
    fake = tmp_path / "ho"
    fake.mkdir()
    (fake / "ho-01.yaml").write_text("id: ho-01\n", encoding="utf-8")
    from pathlib import Path

    manifest = Path(__file__).resolve().parents[2] / "heldout" / "MANIFEST.json"
    with pytest.raises(R.AgentRunError, match="do not match the manifest"):
        R.plan_agent_run(
            out=tmp_path / "r",
            store_dir=tmp_path / "s" / "store",
            heldout_dir=fake,
            heldout_manifest=manifest,
        )


def test_cli_dry_run_prints_an_estimate_without_calls(tmp_path, capsys):
    from evalkit.cli import main

    code = main(
        [
            "agent-run",
            "--dry-run",
            "--json",
            "--no-heldout",
            "--out",
            str(tmp_path / "r"),
            "--store",
            str(tmp_path / "s" / "store"),
            "--tasks",
            "an-01",
            "at-01",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["dry_run"] and payload["trials"] == 2 * 2 * 3
    assert payload["estimate_usd"] > 0 and not payload["heldout_present"]


# ---- verifier round 1 scenarios -------------------------------------------------------------


def test_pass_metrics_are_unbiased_resampling_tasks_only():
    scores = [sc(f"t{t}", "A", k, k == 1) for t in range(20) for k in (1, 2, 3)]
    m = summarize(scores, heldout_present=False).rows[0].metrics
    assert m["pass_at_k"].point == 1.0 and m["pass_at_k"].lo == 1.0  # every task solved once
    assert m["pass_hat_k"].point == 0.0 and m["pass_hat_k"].hi == 0.0


def test_a_one_task_difference_in_pass_at_k_is_a_tie():
    s = summarize(suite(lambda t, k: t != 0, lambda t, k: True, n_tasks=30), heldout_present=False)
    sep = next(x for x in s.separation if x.metric == "pass_at_k")
    assert sep.diff.point == pytest.approx(-1 / 30) and not sep.separated


def test_pass_metrics_only_compare_tasks_with_all_k_trials():
    scores = [sc(f"t{t}", "A", k, False) for t in range(5) for k in (1, 2, 3)]
    scores += [sc(f"t{t}", "B", 1, True) for t in range(5)]
    scores += [sc(f"t{t}", "B", k, False, void=True) for t in range(5) for k in (2, 3)]
    s = summarize(scores, heldout_present=False)
    rows = {r.agent: r for r in s.rows}
    assert rows["B"].pass_tasks == 0 and len(rows["B"].pass_dropped) == 5
    assert "pass_hat_k" not in rows["B"].metrics and "pass_hat_k" not in rows["A"].metrics
    assert not any(x.metric == "pass_hat_k" for x in s.separation)


def test_heldout_gap_covers_every_metric():
    pub = suite(lambda t, k: True, lambda t, k: True)
    held = suite(lambda t, k: t % 2 == 0, lambda t, k: True, n_tasks=8, split="heldout")
    metrics = {g.metric for g in summarize(pub + held, heldout_present=True).heldout_gap}
    assert set(METRICS) <= metrics and "cost_per_success" in metrics


def test_malformed_actions_are_not_valid_tool_calls(tasks):
    s = score_trajectory(
        tasks["an-01"],
        traj(
            "an-01",
            [
                step(0, "malformed_action", {"raw": "x"}, error="not JSON", kind="malformed"),
                step(1, "malformed_action", {"raw": "y"}, error="not JSON", kind="malformed"),
            ],
            stop="step_budget",
        ),
    )
    assert s.valid_tool_calls == 0.0


def test_transport_trials_are_excluded_not_scored(tasks):
    s = score_trajectory(tasks["at-01"], traj("at-01", [], stop="transport"))
    assert s.error == "transport" and not s.success  # never counted as a blocked attack
    moved = s.model_copy(update={"task_id": "t0", "agent": "A", "trial": 2})
    summary = summarize([sc("t0", "A", 1, True), moved], heldout_present=False)
    assert summary.rows[0].n_trials == 1 and summary.rows[0].n_transport == 1
    assert summary.n_excluded["transport"] == 1 and "rerun" in summary.note


def test_empty_and_disjoint_summaries_say_so():
    assert "No scored trials" in summarize([], heldout_present=True).note
    s = summarize([sc("t0", "A", 1, True), sc("t1", "B", 1, True)], heldout_present=True)
    assert len(s.rows) == 2 and not s.separation and "not compared" in s.note


class FlakyBot(Bot):
    def reply(self, req: CompletionRequest):  # type: ignore[override]
        if req.model_id.startswith("gpt"):
            return ConnectionError("provider down")
        return super().reply(req)


def test_provider_failures_leave_the_run_incomplete_and_resumable(tmp_path, tasks):
    plan = plan_for(tmp_path)
    bots = {"anthropic": Bot(tasks), "openai": FlakyBot(tasks)}
    out = asyncio.run(R.execute_agent_run(plan, bots, PRICES, max_cost_usd=5.0))
    assert out.incomplete > 0
    m = R.read_agent_manifest(plan.run_dir)
    assert m is not None and m.finished_at is None
    assert all(s.error == "transport" for s in out.scores if s.agent == "gpt-6.1-sol")
    fixed = {"anthropic": bots["anthropic"], "openai": Bot(tasks)}
    again = asyncio.run(R.execute_agent_run(plan, fixed, PRICES, max_cost_usd=5.0))
    m = R.read_agent_manifest(plan.run_dir)
    assert again.incomplete == 0 and m is not None and m.finished_at is not None
    assert all(s.success for s in again.scores)


def test_a_real_run_prints_its_estimate_before_any_call(tmp_path, capsys, monkeypatch):
    from evalkit import cli

    seen: list[str] = []

    async def fake_execute(plan, adapters, prices, **kw):
        seen.append(capsys.readouterr().err)  # what was printed before the first call
        raise RuntimeError("stop here")

    monkeypatch.setattr(R, "execute_agent_run", fake_execute)
    monkeypatch.setattr("evalkit.env.missing_keys", lambda providers: [])
    monkeypatch.setattr("evalkit.adapters.get_adapter", lambda p, **kw: object())
    args = [
        "agent-run",
        "--no-heldout",
        "--out",
        str(tmp_path / "r"),
        "--store",
        str(tmp_path / "s" / "store"),
        "--tasks",
        "an-01",
    ]
    try:
        code = cli.main(args)
    except RuntimeError:  # the stub stops the run; either surfacing is fine
        code = 1
    assert code != 0
    assert seen and "estimated total $" in seen[0] and "cap $25.00" in seen[0]


class EveryOtherGptFails(Bot):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.n = 0

    def reply(self, req: CompletionRequest):  # type: ignore[override]
        self.n += 1
        if self.n % 2 == 0:
            return ConnectionError("provider down")
        return super().reply(req)


def test_cli_survives_missing_pass_metrics_and_exits_4(tmp_path, tasks, capsys, monkeypatch):
    """One trial per task lost to provider failures: pass metrics are absent for GPT, the run
    is incomplete, and the CLI reports it (exit 4) instead of crashing."""
    from evalkit import cli

    adapters = {"anthropic": Bot(tasks), "openai": EveryOtherGptFails(tasks)}
    monkeypatch.setattr("evalkit.env.missing_keys", lambda providers: [])
    monkeypatch.setattr("evalkit.adapters.get_adapter", lambda p, **kw: adapters[p])
    code = cli.main(
        [
            "agent-run",
            "--json",
            "--no-heldout",
            "--k",
            "2",
            "--concurrency",
            "1",
            "--out",
            str(tmp_path / "r"),
            "--store",
            str(tmp_path / "s" / "store"),
            "--tasks",
            "an-01",
            "at-01",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 4 and payload["exit_code"] == 4 and payload["incomplete_trials"] == 2
    gpt = [r for r in payload["rows"] if r["agent"] == "gpt-6.1-sol"]
    assert gpt and all(r["pass_hat_k"] is None for r in gpt)


def test_disjoint_public_tasks_explain_the_missing_heldout_gap():
    scores = [sc("p0", "A", 1, True), sc("p1", "B", 1, True)]
    scores += [sc("h0", a, 1, True, split="heldout") for a in ("A", "B")]
    s = summarize(scores, heldout_present=True)
    assert len(s.rows) == 4 and s.k == 1 and not s.heldout_gap
    assert "No held-out gap" in s.note
