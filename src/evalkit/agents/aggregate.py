"""Agent aggregate (C3.4, ASC-02 to ASC-04).

Per agent, simulator view and split, with 95% bootstrap intervals:

- Trial-level metrics (success rate, steps over the minimum, violations per trial, valid tool
  calls, cost per success) use a two-level bootstrap: resample tasks, then trials within each
  task.
- Task-level metrics (pass@k: any of the k trials solved it; pass^k: all of them did) are
  computed per task from all k observed trials and resample tasks only. Resampling trials
  before any()/all() would bias both (D-51).

Pairs of agents are compared on the same draws; a gap is claimed only where the
Holm-adjusted p-value rejects and the paired interval excludes zero. Void and
transport-interrupted trials are excluded and counted; pass@k and pass^k use only tasks where
every agent has all k trials, and say which tasks were dropped. Held-out tasks are scored
apart and compared with the public ones on every metric.

A simulator view is the whole suite with one simulator answering the ambiguous tasks;
unambiguous tasks (no simulator) are shared by every view, so results are reported per
simulator (FR22). Per-view costs therefore overlap: `agent_cost_usd` holds the true totals.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Literal

from evalkit.schemas import AgentScore, Interval
from evalkit.schemas._base import Model, NonNegFloat, NonNegInt
from evalkit.stats import holm, interval_from

N_RESAMPLES = 2000
TRIAL_METRICS = ("success", "steps_over_gold", "violations", "valid_tool_calls")
TASK_METRICS = ("pass_at_k", "pass_hat_k")
METRICS = (
    "success",
    "pass_at_k",
    "pass_hat_k",
    "steps_over_gold",
    "violations",
    "valid_tool_calls",
)
SEPARATION_METRICS = ("success", "pass_at_k", "pass_hat_k")
EXCLUDED = ("void", "transport")  # trials that never produced an agent result
UNSTABLE = 0.05  # cost per success has no interval when more resamples than this solve nothing
Split = Literal["public", "heldout"]

Trials = list[AgentScore]
Key = tuple[str, str | None, str, str]


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


TRIAL_FNS: dict[str, Callable[[AgentScore], float]] = {
    "success": lambda t: float(t.success),
    "steps_over_gold": lambda t: float(t.steps_over_gold),
    "violations": lambda t: float(t.policy_violations),
    "valid_tool_calls": lambda t: float(t.valid_tool_calls),
}
TASK_FNS: dict[str, Callable[[Trials], float]] = {
    "pass_at_k": lambda ts: float(any(t.success for t in ts)),
    "pass_hat_k": lambda ts: float(all(t.success for t in ts)),
}


def _trial_metric(sample: Sequence[Trials], fn: Callable[[AgentScore], float]) -> float:
    return _mean([_mean([fn(t) for t in ts]) for ts in sample])


def _cost_per_success(sample: Sequence[Trials]) -> float | None:
    wins = sum(t.success for ts in sample for t in ts)
    return math.fsum(t.cost_usd for ts in sample for t in ts) / wins if wins else None


class Plan:
    """Seeded resample plan over `n_tasks` tasks: task draws, plus trial picks stored as random
    integers reduced modulo each agent's own trial count, so one plan serves every agent and
    paired comparisons share the same draws."""

    def __init__(self, n_tasks: int, max_trials: int, seed: str, n: int = N_RESAMPLES) -> None:
        rng = random.Random(seed)
        self.rows = [
            [
                (rng.randrange(n_tasks), tuple(rng.randrange(1 << 30) for _ in range(max_trials)))
                for _ in range(n_tasks)
            ]
            for _ in range(n)
        ]

    def samples(self, tasks: Sequence[Trials]) -> list[list[Trials]]:
        """Two-level: resampled tasks, each with its trials resampled."""
        return [
            [[tasks[t][p % len(tasks[t])] for p in picks[: len(tasks[t])]] for t, picks in row]
            for row in self.rows
        ]

    def task_samples(self, tasks: Sequence[Trials]) -> list[list[Trials]]:
        """Tasks only: resampled tasks, each with all of its observed trials."""
        return [[tasks[t] for t, _ in row] for row in self.rows]


class AgentMetrics(Model):
    agent: str
    view: str | None  # simulator name, or None when the run used no simulator
    split: Split
    n_tasks: NonNegInt
    n_trials: NonNegInt
    n_void: NonNegInt
    n_transport: NonNegInt  # interrupted by a provider failure: excluded, resume to finish
    k: NonNegInt  # trials per task for pass@k / pass^k
    pass_tasks: NonNegInt  # tasks with all k trials for every agent (pass@k / pass^k use these)
    pass_dropped: list[str]  # tasks left out of pass@k / pass^k (missing trials)
    metrics: dict[str, Interval]  # METRICS (pass metrics absent when no task has all k)
    widths: dict[str, NonNegFloat]
    cost_per_success: Interval | None  # None when unstable (see cost_unsolved_share)
    cost_unsolved_share: NonNegFloat  # share of resamples with no success at all
    tool_error_trials: NonNegInt
    recovered: NonNegInt  # of those, trials that still succeeded
    cost_usd: NonNegFloat  # this view and split (shared tasks repeat across views)
    simulator_cost_usd: NonNegFloat


class Separation(Model):
    view: str | None
    split: Split
    metric: str
    agent_a: str
    agent_b: str
    diff: Interval  # a minus b
    p_value: float
    separated: bool  # Holm-adjusted across the pairs of this view, split and metric


class HeldoutGap(Model):
    agent: str
    view: str | None
    metric: str
    public: Interval
    heldout: Interval
    diff: Interval  # public minus held-out


class AgentSummary(Model):
    agents: list[str]
    views: list[str | None]
    k: NonNegInt
    n_resamples: NonNegInt
    rows: list[AgentMetrics]
    separation: list[Separation]
    heldout_gap: list[HeldoutGap]
    heldout_present: bool
    agent_cost_usd: dict[str, NonNegFloat]  # true totals per agent (each trial once)
    simulator_cost_usd: NonNegFloat
    n_excluded: dict[str, NonNegInt]  # void / transport trials, each counted once
    notes: list[str]

    @property
    def note(self) -> str:
        return " ".join(self.notes)


def _views(scores: Sequence[AgentScore]) -> list[str | None]:
    sims = sorted({s.simulator for s in scores if s.simulator})
    return list(sims) if sims else [None]


def _in_view(s: AgentScore, view: str | None) -> bool:
    return s.simulator is None or s.simulator == view


def _tasks_for(
    scores: Sequence[AgentScore], agent: str, view: str | None, split: Split
) -> dict[str, Trials]:
    by_task: dict[str, Trials] = defaultdict(list)
    for s in scores:
        if s.agent == agent and s.split == split and s.error not in EXCLUDED and _in_view(s, view):
            by_task[s.task_id].append(s)
    return {t: sorted(v, key=lambda s: s.trial) for t, v in sorted(by_task.items())}


def _p_value(diff: Sequence[float]) -> float:
    big = len(diff) + 1
    le = (sum(d <= 0 for d in diff) + 1) / big
    ge = (sum(d >= 0 for d in diff) + 1) / big
    return min(1.0, 2 * min(le, ge))


def _row(
    agent: str,
    view: str | None,
    split: Split,
    by_task: dict[str, Trials],
    task_ids: Sequence[str],
    full_ids: Sequence[str],
    k: int,
    plan: Plan,
    task_plan: Plan | None,
    unique: dict[tuple[str, str, str | None, int], AgentScore],
) -> tuple[AgentMetrics, dict[str, list[float | None]], dict[str, float]]:
    """One agent's metrics, bootstrap draws and point estimates over `task_ids` (trial-level,
    two-level `plan`) and `full_ids` (pass metrics, tasks-only `task_plan`). Paired callers
    pass the same plans for every agent so comparisons share draws."""
    tasks = [by_task[t] for t in task_ids]
    two_level = plan.samples(tasks)
    metrics: dict[str, Interval] = {}
    boots: dict[str, list[float | None]] = {}
    for m in TRIAL_METRICS:
        boot = [_trial_metric(s, TRIAL_FNS[m]) for s in two_level]
        metrics[m] = interval_from(_trial_metric(tasks, TRIAL_FNS[m]), boot)
        boots[m] = list(boot)
    if task_plan is not None and full_ids:
        full = [by_task[t] for t in full_ids]
        draws = task_plan.task_samples(full)
        for m in TASK_METRICS:
            boot = [_mean([TASK_FNS[m](ts) for ts in s]) for s in draws]
            metrics[m] = interval_from(_mean([TASK_FNS[m](ts) for ts in full]), boot)
            boots[m] = list(boot)
    points = {m: iv.point for m, iv in metrics.items()}
    cps = [_cost_per_success(s) for s in two_level]
    unsolved = sum(c is None for c in cps) / len(cps)
    cps_point = _cost_per_success(tasks)
    defined = [c for c in cps if c is not None]
    boots["cost_per_success"] = cps
    if cps_point is not None:
        points["cost_per_success"] = cps_point
    trials = [t for ts in tasks for t in ts]
    errs = [t for t in trials if t.recovered_from_error is not None]
    in_view = [
        s for s in unique.values() if s.agent == agent and s.split == split and _in_view(s, view)
    ]
    row = AgentMetrics(
        agent=agent,
        view=view,
        split=split,
        n_tasks=len(tasks),
        n_trials=len(trials),
        n_void=sum(s.error == "void" for s in in_view),
        n_transport=sum(s.error == "transport" for s in in_view),
        k=k,
        pass_tasks=len(full_ids) if task_plan is not None else 0,
        pass_dropped=[t for t in task_ids if t not in set(full_ids)],
        metrics=metrics,
        widths={m: iv.hi - iv.lo for m, iv in metrics.items()},
        cost_per_success=interval_from(cps_point, defined)
        if cps_point is not None and defined and unsolved <= UNSTABLE
        else None,
        cost_unsolved_share=unsolved,
        tool_error_trials=len(errs),
        recovered=sum(bool(t.recovered_from_error) for t in errs),
        cost_usd=math.fsum(t.cost_usd for t in trials),
        simulator_cost_usd=math.fsum(t.simulator_cost_usd for t in trials),
    )
    return row, boots, points


def summarize(
    scores: Sequence[AgentScore], *, seed: int = 0, heldout_present: bool
) -> AgentSummary:
    """The agent summary for a run's scores."""
    scores = list(scores)
    agents = sorted({s.agent for s in scores})
    views = _views(scores)
    notes: list[str] = []
    if not heldout_present:
        notes.append("Held-out tasks were not available on this machine: no held-out scores.")
    unique = {(s.task_id, s.agent, s.simulator, s.trial): s for s in scores}
    excluded = {r: sum(1 for s in unique.values() if s.error == r) for r in EXCLUDED}
    if excluded["transport"]:
        notes.append(
            f"{excluded['transport']} trial(s) were interrupted by provider failures and are "
            "excluded; rerun the same command to finish them (finished trials replay free)."
        )
    rows: list[AgentMetrics] = []
    separation: list[Separation] = []
    boots: dict[Key, list[float | None]] = {}
    points: dict[Key, float] = {}
    ks: list[int] = []
    for view in views:
        for split in ("public", "heldout"):
            per_agent = {a: _tasks_for(scores, a, view, split) for a in agents}
            if not agents or not any(per_agent.values()):
                continue
            shared = sorted(set.intersection(*(set(v) for v in per_agent.values())))
            if not shared:
                notes.append(
                    f"No task in view {view or 'none'} / {split} was run by every agent: agents "
                    "are shown on their own tasks and not compared."
                )
                for a in agents:
                    own = per_agent[a]
                    if not own:
                        continue
                    ids = sorted(own)
                    k_own = max(len(own[t]) for t in ids)
                    full_own = [t for t in ids if len(own[t]) == k_own]
                    key = f"evalkit-agents:{seed}:{view}:{split}:{a}"
                    row, _, _ = _row(
                        a,
                        view,
                        split,
                        own,
                        ids,
                        full_own,
                        k_own,
                        Plan(len(ids), k_own, key),
                        Plan(len(full_own), 1, key + ":tasks") if full_own else None,
                        unique,
                    )
                    rows.append(row)
                    ks.append(k_own)
                if split == "heldout" or heldout_present:
                    notes.append(
                        f"No held-out gap for view {view or 'none'}: the agents share no "
                        f"{split} task, so the {split} scores are not comparable."
                    )
                continue
            k = max(len(per_agent[a][t]) for a in agents for t in shared)
            full = [t for t in shared if all(len(per_agent[a][t]) == k for a in agents)]
            seed_key = f"evalkit-agents:{seed}:{view}:{split}"
            plan = Plan(len(shared), k, seed_key)
            task_plan = Plan(len(full), 1, seed_key + ":tasks") if full else None
            ks.append(k)
            for a in agents:
                row, b, pts = _row(
                    a, view, split, per_agent[a], shared, full, k, plan, task_plan, unique
                )
                rows.append(row)
                for m, v in b.items():
                    boots[(a, view, split, m)] = v
                for m, v in pts.items():
                    points[(a, view, split, m)] = v
            for m in SEPARATION_METRICS:
                pairs: dict[tuple[str, str], tuple[Interval, float]] = {}
                for i, a in enumerate(agents):
                    for b in agents[i + 1 :]:
                        ka, kb = (a, view, split, m), (b, view, split, m)
                        if ka not in points or kb not in points:
                            continue
                        diff = [
                            x - y
                            for x, y in zip(boots[ka], boots[kb], strict=True)
                            if x is not None and y is not None
                        ]
                        iv = interval_from(points[ka] - points[kb], diff)
                        pairs[(a, b)] = (iv, _p_value(diff))
                rejected = holm({key: v[1] for key, v in pairs.items()})
                for (a, b), (iv, p) in pairs.items():
                    sep = rejected[(a, b)] and (iv.lo > 0 or iv.hi < 0)
                    separation.append(
                        Separation(
                            view=view,
                            split=split,
                            metric=m,
                            agent_a=a,
                            agent_b=b,
                            diff=iv,
                            p_value=p,
                            separated=sep,
                        )
                    )
    gaps: list[HeldoutGap] = []
    for a in agents:
        for view in views:
            for m in (*METRICS, "cost_per_success"):
                pub, held = (a, view, "public", m), (a, view, "heldout", m)
                if pub not in points or held not in points:
                    continue
                both = [
                    (x, y)
                    for x, y in zip(boots[pub], boots[held], strict=True)
                    if x is not None and y is not None
                ]
                if not both or len(both) < (1 - UNSTABLE) * len(boots[pub]):
                    continue  # cost per success too unstable to compare
                gaps.append(
                    HeldoutGap(
                        agent=a,
                        view=view,
                        metric=m,
                        public=interval_from(points[pub], [x for x, _ in both]),
                        heldout=interval_from(points[held], [y for _, y in both]),
                        diff=interval_from(points[pub] - points[held], [x - y for x, y in both]),
                    )
                )
    if not scores:
        notes.append("No scored trials.")
    return AgentSummary(
        agents=agents,
        views=views,
        k=min(ks) if ks else 0,
        n_resamples=N_RESAMPLES,
        rows=rows,
        separation=separation,
        heldout_gap=gaps,
        heldout_present=heldout_present,
        agent_cost_usd={
            a: math.fsum(s.cost_usd for s in unique.values() if s.agent == a) for a in agents
        },
        simulator_cost_usd=math.fsum(s.simulator_cost_usd for s in unique.values()),
        n_excluded=excluded,
        notes=notes,
    )
