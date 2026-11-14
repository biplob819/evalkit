"""`evalkit agent-run` (C3.4, ASC-05): plan, estimate, run and summarise an agent suite.

Every (agent, task, trial) is one trial; ambiguous tasks run once per simulator. Trials are
cached whole (C3.2), so rerunning the command replays a finished run with no calls and resumes
an interrupted one. The cost cap is a total for the run folder across resumes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evalkit.adapters import ModelAdapter
from evalkit.agents.aggregate import AgentSummary, summarize
from evalkit.agents.db import DB_SHA256, build_database, content_hash, dump_database
from evalkit.agents.loop import run_trial
from evalkit.agents.score import score_trajectory
from evalkit.agents.simuser import SIMULATORS, SimulatedUser
from evalkit.agents.tasks import TASKS_DIR, check_heldout, load_agent_tasks
from evalkit.cost import Price, call_cost, price_for
from evalkit.pipeline.generate import provider_for
from evalkit.runtime import BudgetExceeded, Executor, Store
from evalkit.schemas import AgentScore, AgentTask, Trajectory
from evalkit.schemas._base import Model, NonBlank, Timestamp

AGENTS = ("claude-sonnet-5-5", "gpt-6.1-sol")  # the matched-price pair (equal list prices)
SCHEMA_VERSION = "agent-run-1"


class AgentRunError(Exception):
    """A configuration or input problem: exit 2 before any model call."""


class AgentTokenAssumptions(Model):
    """Per-trial token assumptions for the cost estimate, measured by a 10-trial pilot
    (docs/measurements/agent-tokens-2026-11-15.json: Sonnet 5.2 turns, 1,777 in, 194 out,
    66% cached; Sol 6.8 turns, 1,163 in, 168 out, 46% cached). Output includes reasoning."""

    turns: float = 6.0  # agent turns per trial
    t_in: float = 1500.0  # prompt tokens per agent turn (the conversation grows each turn)
    cached_share: float = 0.5  # share of prompt tokens read from the provider cache
    t_out: float = 180.0
    t_reason: float = 0.0  # already inside t_out (measured as billed output)
    sim_calls: float = 1.0  # simulated-user replies per ambiguous trial
    sim_in: float = 650.0
    sim_out: float = 60.0
    sim_reason: float = 150.0


TOKENS = AgentTokenAssumptions()


@dataclass(frozen=True)
class Job:
    task: AgentTask
    agent: str
    trial: int
    simulator: str | None  # simulator model, None on unambiguous tasks


class AgentRunManifest(Model):
    schema_version: NonBlank
    run_id: NonBlank
    agents: tuple[NonBlank, ...]
    simulators: tuple[NonBlank, ...]
    k: int
    seed: int
    effort: str | None
    task_hashes: dict[str, str]
    heldout_present: bool
    db_sha256: NonBlank
    started_at: Timestamp
    finished_at: Timestamp | None = None
    spent_usd: float = 0.0  # all real spend on this folder, across resumes (the cap is total)


@dataclass(frozen=True)
class AgentPlan:
    tasks: tuple[AgentTask, ...]
    jobs: tuple[Job, ...]
    agents: tuple[str, ...]
    simulators: tuple[str, ...]
    k: int
    seed: int
    effort: str | None
    run_id: str
    run_dir: Path
    store_dir: Path
    db_path: Path
    heldout_present: bool
    task_hashes: dict[str, str]


def _task_hash(t: AgentTask) -> str:
    return hashlib.sha256(t.model_dump_json().encode()).hexdigest()


def ensure_database(path: Path) -> Path:
    """The pinned database at `path`, built if missing or different."""
    if path.is_file():
        try:
            if content_hash(dump_database(path)) == DB_SHA256:
                return path
        except Exception:  # a broken file is rebuilt
            pass
    return build_database(path)


def plan_agent_run(
    *,
    agents: Sequence[str] = AGENTS,
    simulators: Sequence[str] = SIMULATORS,
    k: int = 3,
    seed: int = 7,
    effort: str | None = "medium",
    out: Path = Path("runs"),
    store_dir: Path = Path(".evalkit/store"),
    heldout_dir: Path | None = Path("heldout/agent_tasks"),
    heldout_manifest: Path = Path("heldout/MANIFEST.json"),
    task_dirs: Sequence[Path] = (TASKS_DIR,),
    only: Sequence[str] = (),
) -> AgentPlan:
    if k < 1:
        raise AgentRunError("k must be at least 1")
    if len(set(agents)) != len(agents) or not agents:
        raise AgentRunError("give one or more distinct agents")
    for m in (*agents, *simulators):
        try:
            provider_for(m)
        except ValueError as exc:
            raise AgentRunError(str(exc)) from None
    db_path = ensure_database(store_dir.parent / "agents" / "riverbend.duckdb")
    dirs = list(task_dirs)
    heldout_present = False
    if heldout_dir is not None and heldout_dir.is_dir():
        problems = check_heldout(heldout_dir, heldout_manifest)
        if problems:
            raise AgentRunError("held-out tasks do not match the manifest: " + "; ".join(problems))
        dirs.append(heldout_dir)
        heldout_present = True
    try:
        tasks = load_agent_tasks(dirs, db_path=db_path)
    except ValueError as exc:
        raise AgentRunError(str(exc)) from None
    if only:
        missing = sorted(set(only) - {t.id for t in tasks})
        if missing:
            raise AgentRunError(f"unknown task ids: {missing}")
        tasks = [t for t in tasks if t.id in set(only)]
    jobs = [
        Job(t, a, trial, sim)
        for a in agents
        for t in tasks
        for trial in range(1, k + 1)
        for sim in ((simulators or (None,)) if t.simulated_user_intent else (None,))
    ]
    if any(t.simulated_user_intent for t in tasks) and not simulators:
        raise AgentRunError("ambiguous tasks need at least one simulator")
    hashes = {t.id: _task_hash(t) for t in tasks}
    ident = json.dumps([list(agents), list(simulators), k, seed, effort, hashes], sort_keys=True)
    run_id = "a-" + hashlib.sha256(ident.encode()).hexdigest()[:12]
    return AgentPlan(
        tasks=tuple(tasks),
        jobs=tuple(jobs),
        agents=tuple(agents),
        simulators=tuple(simulators),
        k=k,
        seed=seed,
        effort=effort,
        run_id=run_id,
        run_dir=out / run_id,
        store_dir=store_dir,
        db_path=db_path,
        heldout_present=heldout_present,
        task_hashes=hashes,
    )


def estimate_usd(
    plan: AgentPlan, prices: dict[str, Price], tokens: AgentTokenAssumptions = TOKENS
) -> dict[str, float]:
    """Estimated spend per model (agents and simulators) before any call."""
    out: dict[str, float] = {}
    for job in plan.jobs:
        per_turn = call_cost(
            price_for(prices, job.agent),
            tokens_in=int(tokens.t_in),
            tokens_cached=int(tokens.t_in * tokens.cached_share),
            tokens_out=int(tokens.t_out + tokens.t_reason),
        )
        out[job.agent] = out.get(job.agent, 0.0) + tokens.turns * per_turn
        if job.simulator:
            reply = call_cost(
                price_for(prices, job.simulator),
                tokens_in=int(tokens.sim_in),
                tokens_out=int(tokens.sim_out + tokens.sim_reason),
            )
            out[job.simulator] = out.get(job.simulator, 0.0) + tokens.sim_calls * reply
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    os.chmod(tmp, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_agent_manifest(run_dir: Path) -> AgentRunManifest | None:
    path = run_dir / "agent_run.json"
    if not path.is_file():
        return None
    return AgentRunManifest.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass
class AgentOutcome:
    summary: AgentSummary
    trajectories: list[Trajectory]
    scores: list[AgentScore]
    spent_usd: float
    run_dir: Path
    incomplete: int = 0  # trials interrupted by provider failures: rerun to finish them


async def execute_agent_run(
    plan: AgentPlan,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    *,
    max_cost_usd: float | None,
    concurrency: int = 8,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AgentOutcome:
    """Run (or resume, or replay) every trial, then score, summarise and write the folder.
    BudgetExceeded propagates after the finished trials are written and the spend recorded."""
    previous = read_agent_manifest(plan.run_dir)
    spent_before = previous.spent_usd if previous else 0.0
    remaining = None if max_cost_usd is None else max(0.0, max_cost_usd - spent_before)
    ex = Executor(
        Store(plan.store_dir),
        max_cost_usd=remaining,
        concurrency={"agent_trial": concurrency},
        default_concurrency=concurrency,
    )
    manifest = AgentRunManifest(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        agents=plan.agents,
        simulators=plan.simulators,
        k=plan.k,
        seed=plan.seed,
        effort=plan.effort,
        task_hashes=plan.task_hashes,
        heldout_present=plan.heldout_present,
        db_sha256=DB_SHA256,
        started_at=previous.started_at if previous else now(),
        spent_usd=spent_before,
    )
    _atomic_write(plan.run_dir / "agent_run.json", manifest.model_dump_json(indent=2) + "\n")
    workdir = plan.store_dir.parent / "agents" / "trials"

    async def one(job: Job) -> Trajectory:
        user = None
        if job.simulator:
            user = SimulatedUser(
                ex,
                job.task,
                job.simulator,
                adapters,
                prices,
                trial=job.trial,
                seed=plan.seed,
                run_id=plan.run_id,
            )
        return await run_trial(
            ex,
            job.task,
            agent=job.agent,
            trial=job.trial,
            seed=plan.seed,
            db_path=plan.db_path,
            workdir=workdir,
            adapters=adapters,
            prices=prices,
            effort=plan.effort,
            user=user,
            simulator=user.name if user else None,
            run_id=plan.run_id,
        )

    try:
        results = await asyncio.gather(*(one(j) for j in plan.jobs), return_exceptions=True)
    finally:
        if ex.spent_usd:
            latest = read_agent_manifest(plan.run_dir) or manifest
            _atomic_write(
                plan.run_dir / "agent_run.json",
                latest.model_copy(
                    update={"spent_usd": spent_before + ex.spent_usd}
                ).model_dump_json(indent=2)
                + "\n",
            )
    done = [(j, r) for j, r in zip(plan.jobs, results, strict=True) if isinstance(r, Trajectory)]
    by_id = {t.id: t for t in plan.tasks}
    trajectories = [r for _, r in done]
    scores = [score_trajectory(by_id[j.task.id], r) for j, r in done]
    _atomic_write(
        plan.run_dir / "trajectories.jsonl",
        "".join(t.model_dump_json() + "\n" for t in trajectories),
    )
    _atomic_write(
        plan.run_dir / "scores.jsonl", "".join(s.model_dump_json() + "\n" for s in scores)
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    other = [e for e in errors if not isinstance(e, BudgetExceeded)]
    if other:  # a configuration or code problem outranks the (resumable) cost cap
        if len(other) < len(errors):
            other[0].add_note(f"{len(errors) - len(other)} trial(s) also stopped at the cost cap")
        raise other[0]
    if errors:
        cap = errors[0].cap  # type: ignore[attr-defined]
        raise BudgetExceeded(spent_before + ex.spent_usd, cap, remaining=len(errors))
    summary = summarize(scores, seed=plan.seed, heldout_present=plan.heldout_present)
    _atomic_write(plan.run_dir / "agent_summary.json", summary.model_dump_json(indent=2) + "\n")
    latest = read_agent_manifest(plan.run_dir) or manifest
    incomplete = sum(t.stop_reason == "transport" for t in trajectories)
    if latest.finished_at is None and not incomplete:  # finished means every trial has a result
        latest = latest.model_copy(update={"finished_at": now()})
        _atomic_write(plan.run_dir / "agent_run.json", latest.model_dump_json(indent=2) + "\n")
    from evalkit.report.agents import render_agent_report

    _atomic_write(plan.run_dir / "report.html", render_agent_report(summary, plan.run_id, scores))
    return AgentOutcome(summary, trajectories, scores, ex.spent_usd, plan.run_dir, incomplete)


def total(costs: Mapping[str, float]) -> float:
    return math.fsum(costs.values())
