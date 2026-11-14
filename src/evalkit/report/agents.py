"""The Agents report (C3.4, ASC-05): a self-contained HTML page for an agent run."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from evalkit.agents.aggregate import AgentMetrics, AgentSummary
from evalkit.report.html import CSS
from evalkit.schemas import AgentScore, Interval

LABELS = {
    "success": "Success",
    "pass_at_k": "pass@k (any trial)",
    "pass_hat_k": "pass^k (every trial)",
    "steps_over_gold": "Steps over minimum",
    "violations": "Violations per trial",
    "valid_tool_calls": "Valid tool calls",
}


def _pct(iv: Interval) -> str:
    return f"{iv.point:.0%} <span class=muted>({iv.lo:.0%}–{iv.hi:.0%})</span>"


def _num(iv: Interval) -> str:
    return f"{iv.point:.2f} <span class=muted>({iv.lo:.2f}–{iv.hi:.2f})</span>"


GAP_LABELS = {**LABELS, "cost_per_success": "Cost per success"}


def _value(metric: str, iv: Interval) -> str:
    if metric in RATES:
        return _pct(iv)
    if metric == "cost_per_success":
        return f"${iv.point:.3f} <span class=muted>(${iv.lo:.3f}–${iv.hi:.3f})</span>"
    return _num(iv)


def _gap(metric: str, iv: Interval) -> str:
    if metric in RATES:
        return _diff(iv)
    if metric == "cost_per_success":
        return f"{iv.point:+.3f} <span class=muted>({iv.lo:+.3f} to {iv.hi:+.3f})</span>"
    return f"{iv.point:+.2f} <span class=muted>({iv.lo:+.2f} to {iv.hi:+.2f})</span>"


def _diff(iv: Interval) -> str:
    return f"{iv.point:+.0%} <span class=muted>({iv.lo:+.0%} to {iv.hi:+.0%})</span>"


SEPARATED = '<span class="badge pass">separated</span>'
RATES = ("success", "pass_at_k", "pass_hat_k", "valid_tool_calls")


def _cell(r: AgentMetrics, m: str) -> str:
    if m not in r.metrics:
        return "<td>–</td>"
    return f"<td>{_pct(r.metrics[m]) if m in RATES else _num(r.metrics[m])}</td>"


def _view(v: str | None) -> str:
    return "no simulator" if v is None else v.split("+")[0]


def _chart(rows: Sequence[AgentMetrics]) -> str:
    """Success with 95% intervals, one row per agent and simulator view (public split)."""
    rows = [r for r in rows if r.split == "public"]
    if not rows:
        return ""
    w, left, top, row_h = 720, 260, 30, 34
    h = top + row_h * len(rows) + 40
    x = lambda v: left + v * (w - left - 30)  # noqa: E731
    parts = [
        f'<svg class=chart viewBox="0 0 {w} {h}" role="img" aria-labelledby="ach-t ach-d">'
        '<title id="ach-t">Agent success with 95% intervals</title>'
        '<desc id="ach-d">Share of trials solved per agent and simulator view on the public '
        "tasks, with two-level bootstrap 95% intervals. Data in the table below.</desc>"
    ]
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(
            f'<line class=grid x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top - 10}" y2="{h - 30}"/>'
        )
        parts.append(
            f'<text class=tick x="{x(t):.1f}" y="{h - 12}" text-anchor="middle">{t:.0%}</text>'
        )
    for i, r in enumerate(rows):
        y = top + row_h * i + row_h / 2
        iv = r.metrics["success"]
        label = f"{r.agent} · {_view(r.view)}"
        tip = f"{escape(label)}: {iv.point:.0%} ({iv.lo:.0%}–{iv.hi:.0%})"
        parts.append(
            f'<text class=tick x="{left - 10}" y="{y + 4:.1f}" text-anchor="end">'
            f"{escape(label)}</text>"
        )
        parts.append(
            f'<line class=whisker x1="{x(iv.lo):.1f}" x2="{x(iv.hi):.1f}" '
            f'y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<circle class="s{i % 4}" cx="{x(iv.point):.1f}" cy="{y:.1f}" r="6">'
            f"<title>{tip}</title></circle>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _metrics_table(summary: AgentSummary) -> str:
    head = "".join(f"<th scope=col>{LABELS[m]}</th>" for m in LABELS)
    body = []
    for r in summary.rows:
        cells = "".join(_cell(r, m) for m in LABELS)
        if r.cost_per_success is not None:
            cps = f"${r.cost_per_success.point:.3f}"
        elif r.cost_unsolved_share > 0:
            cps = f"unstable ({r.cost_unsolved_share:.0%} of resamples solve nothing)"
        else:
            cps = "–"
        rec = f"{r.recovered}/{r.tool_error_trials}" if r.tool_error_trials else "–"
        dropped = (
            f" <span class=muted>(pass@k/pass^k on {r.pass_tasks}; missing trials: "
            f"{escape(', '.join(r.pass_dropped))})</span>"
            if r.pass_dropped
            else ""
        )
        body.append(
            f"<tr><th scope=row>{escape(r.agent)}</th><td>{escape(_view(r.view))}</td>"
            f"<td>{escape(r.split)}</td><td>{r.n_tasks} × {r.k}{dropped}</td>{cells}"
            f"<td>{cps}</td><td>{rec}</td><td>${r.cost_usd:.3f}</td>"
            f"<td>{r.n_void + r.n_transport}</td></tr>"
        )
    return (
        "<table><caption>Per agent, simulator view and split. Intervals are two-level bootstrap "
        "95% (tasks, then trials).</caption><thead><tr><th scope=col>Agent</th>"
        "<th scope=col>Simulator view</th><th scope=col>Split</th><th scope=col>Tasks × k</th>"
        f"{head}<th scope=col>Cost per success</th><th scope=col>Recovered after a tool error"
        "</th><th scope=col>Agent cost (this view)</th><th scope=col>Excluded trials</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
        + "<p class=muted>Unambiguous tasks appear in every simulator view, so view costs "
        "overlap; totals per agent: "
        + escape(", ".join(f"{a} ${c:.3f}" for a, c in summary.agent_cost_usd.items()))
        + f"; simulated users ${summary.simulator_cost_usd:.3f}.</p>"
    )


def _separation_table(summary: AgentSummary) -> str:
    if not summary.separation:
        if len(summary.agents) < 2:
            return "<p>Only one agent: no pairwise comparison.</p>"
        return "<p>No tasks were run by every agent: nothing to compare.</p>"
    rows = "".join(
        f"<tr><td>{escape(_view(s.view))}</td><td>{escape(s.split)}</td>"
        f"<td>{LABELS[s.metric]}</td>"
        f"<th scope=row>{escape(s.agent_a)} − {escape(s.agent_b)}</th>"
        f"<td>{_diff(s.diff)}</td><td>{s.p_value:.3f}</td>"
        f"<td>{SEPARATED if s.separated else 'tie'}</td></tr>"
        for s in summary.separation
    )
    return (
        "<table><caption>A gap is claimed only where the Holm-adjusted paired interval "
        "excludes zero; everything else is a tie.</caption><thead><tr><th scope=col>View</th>"
        "<th scope=col>Split</th><th scope=col>Metric</th><th scope=col>Pair</th>"
        "<th scope=col>Difference (95% CI)</th><th scope=col>p</th><th scope=col>Verdict</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table>"
    )


def _heldout_table(summary: AgentSummary) -> str:
    if not summary.heldout_present:
        return f"<p>{escape(summary.note)}</p>"
    rows = "".join(
        f"<tr><th scope=row>{escape(g.agent)}</th><td>{escape(_view(g.view))}</td>"
        f"<td>{GAP_LABELS.get(g.metric, g.metric)}</td><td>{_value(g.metric, g.public)}</td>"
        f"<td>{_value(g.metric, g.heldout)}</td><td>{_gap(g.metric, g.diff)}</td></tr>"
        for g in summary.heldout_gap
    )
    return (
        "<table><caption>A public score well above the held-out score flags prompts or scorers "
        "tuned to the public tasks.</caption><thead><tr><th scope=col>Agent</th>"
        "<th scope=col>View</th><th scope=col>Metric</th><th scope=col>Public</th>"
        "<th scope=col>Held-out</th><th scope=col>Public − held-out</th></tr></thead><tbody>"
        + rows
        + "</tbody></table>"
    )


def _excluded(scores: Sequence[AgentScore]) -> str:
    out = [s for s in scores if s.error in ("void", "transport")]
    if not out:
        return ""
    rows = "".join(
        f"<tr><td>{escape(s.task_id)}</td><td>{escape(s.agent)}</td><td>{s.trial}</td>"
        f"<td>{escape(s.detail)}</td></tr>"
        for s in sorted(out, key=lambda s: (s.task_id, s.agent, s.trial))
    )
    return (
        "<table><caption>Trials with no agent result (excluded from every metric)</caption>"
        "<thead><tr><th scope=col>Task</th><th scope=col>Agent</th><th scope=col>Trial</th>"
        "<th scope=col>Why</th></tr></thead><tbody>" + rows + "</tbody></table>"
    )


def _failures(scores: Sequence[AgentScore]) -> str:
    failed = [s for s in scores if not s.success and s.error is None]
    if not failed:
        return "<p>No trial failed.</p>" + _excluded(scores)
    rows = "".join(
        f"<tr><td>{escape(s.task_id)}</td><td>{escape(s.agent)}</td>"
        f"<td>{escape(_view(s.simulator)) if s.simulator else '–'}</td><td>{s.trial}</td>"
        f"<td>{escape(s.detail or (s.error or ''))}</td></tr>"
        for s in sorted(failed, key=lambda s: (s.task_id, s.agent, s.simulator or "", s.trial))
    )
    return (
        "<table><caption>Failed trials and why</caption><thead><tr><th scope=col>Task</th>"
        "<th scope=col>Agent</th><th scope=col>Simulator</th><th scope=col>Trial</th>"
        "<th scope=col>Reason</th></tr></thead><tbody>"
        + rows
        + "</tbody></table>"
        + _excluded(scores)
    )


def render_agent_report(summary: AgentSummary, run_id: str, scores: Sequence[AgentScore]) -> str:
    title = f"EvalKit · agents · {run_id}"
    note = f"<p class=stamps>{escape(summary.note)}</p>" if summary.note else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}</style></head><body><main>"
        f"<h1>Agents · run {escape(run_id)}</h1>"
        f"<p class=meta>Agents: {escape(', '.join(summary.agents))} · k = {summary.k} trials per "
        f"task · {summary.n_resamples} bootstrap resamples</p>{note}"
        f"<h2 id=agents-h>Agents</h2><div class=card>{_chart(summary.rows)}</div>"
        f"<div class=card>{_metrics_table(summary)}</div>"
        f"<h2 id=sep-h>Which gaps are real</h2><div class=card>{_separation_table(summary)}</div>"
        f"<h2 id=ho-h>Held-out versus public</h2><div class=card>{_heldout_table(summary)}</div>"
        f"<h2 id=af-h>Failed trials</h2><div class=card>{_failures(scores)}</div>"
        "</main></body></html>\n"
    )
