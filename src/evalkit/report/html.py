"""Run report (B2.1, D-42): one self-contained HTML file per run, plus `charts.vl.json`.

The page loads nothing from the network, escapes every piece of model text, and is a pure
function of its inputs (same inputs, same bytes). Caveats from `stamp_lines` always sit at
the top; a pending run shows deterministic metrics only.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any

from evalkit.pipeline.aggregate import stamp_lines
from evalkit.report.svg import Point, bounded_rate, money, scatter, use_log_scale
from evalkit.schemas import Case, Interval, ModelSummary, Record, RunSummary, SliceSummary
from evalkit.schemas.calibration import CalibrationReport, Labeller

BANNER = (
    "Research benchmark, not medical advice. Scores describe how models answered PubMedQA-style "
    "questions from abstracts; they are not evidence about any treatment."
)

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5f5e58;--card:#ffffff;--line:#dedad0;
--accent:#2f5d50;--warn-bg:#fff4d6;--warn-fg:#5c4400;--pass:#1f6b3a;--fail:#a1261b;
--s0:#2f5d50;--s1:#8a4b0f;--s2:#3d4f9a;--s3:#7a2d6b;--grid:#ebe7de;--focus:#1a56db}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--fg:#ecebe6;--muted:#aeada6;
--card:#1e1e1b;--line:#3a3934;--accent:#8cc4b0;--warn-bg:#3b3112;--warn-fg:#f5dc9a;
--pass:#7fd39a;--fail:#ff9d92;--s0:#8cc4b0;--s1:#f0a868;--s2:#9fb0f5;--s3:#e09ad2;
--grid:#2c2b27;--focus:#8ab4ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,
"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.2rem;margin:2em 0 .6em}
.banner{background:var(--warn-bg);color:var(--warn-fg);padding:10px 14px;border-radius:8px}
.stamps{border-left:4px solid var(--warn-fg);padding:6px 14px;margin:12px 0}
.meta{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;
margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600}caption{text-align:left;color:var(--muted);padding-bottom:6px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid currentColor;
font-weight:600;font-size:.85em}
.pass{color:var(--pass)}.fail{color:var(--fail)}
.chart{width:100%;height:auto;max-width:760px}
.chart .grid{stroke:var(--grid)}.chart .tick,.chart .axis,.chart .ptlabel{fill:var(--fg);
font-size:12px}.chart .axis{font-weight:600}
.chart .threshold{stroke:var(--fail);stroke-dasharray:6 4}
.chart .whisker{stroke:var(--fg);stroke-width:1.4}
.chart .ptlabel{paint-order:stroke;stroke:var(--bg);stroke-width:4px;stroke-linejoin:round}
.s0{fill:var(--s0)}.s1{fill:var(--s1)}.s2{fill:var(--s2)}.s3{fill:var(--s3)}
details{border:1px solid var(--line);border-radius:8px;margin:8px 0;background:var(--card)}
summary{cursor:pointer;padding:8px 12px}details>div{padding:0 12px 12px}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.sent{margin:4px 0}.n{color:var(--muted);margin-right:6px}
label{margin-right:12px}select{font:inherit;padding:2px 6px}
.muted{color:var(--muted)}
.chart .leader{stroke:var(--muted)}
@media print{select,label{display:none}details{break-inside:avoid}}
"""

JS = """
(function(){
  var m=document.getElementById('f-model'), j=document.getElementById('f-judge');
  function apply(){
    var items=document.querySelectorAll('#failures details');
    for(var i=0;i<items.length;i++){
      var d=items[i], okM=!m.value||d.dataset.model===m.value,
          okJ=!j.value||d.dataset.judges.split('|').indexOf(j.value)>=0;
      d.hidden=!(okM&&okJ);
    }
  }
  if(m&&j){m.addEventListener('change',apply);j.addEventListener('change',apply);}
  window.addEventListener('beforeprint',function(){
    var ds=document.querySelectorAll('details');for(var i=0;i<ds.length;i++){ds[i].open=true}});
})();
"""


GUIDE_URL = "https://github.com/biplob819/evalkit/blob/main/docs/calibrate-your-task.md"
PENDING_SUFFIX = "(judging pending)"


def _pct(iv: Interval | None, n: int | None = None) -> str:
    """A share with its 95% CI; exactly 0 or 1 over n cases reads "0 of n, below X% at 95%"
    (GAT-07), never "0.0% (0.0%–0.0%)"."""
    if iv is None:
        return "—"
    bounded = bounded_rate(iv.point, iv.lo, iv.hi, n)
    if bounded is not None:
        return escape(bounded)
    return f"{iv.point:.1%} <span class=muted>({iv.lo:.1%}–{iv.hi:.1%})</span>"


def _kappa(iv: Interval | None) -> str:
    """Kappa is a coefficient in [-1, 1], never a percent."""
    if iv is None:
        return "—"
    return f"{iv.point:.2f} <span class=muted>({iv.lo:.2f} to {iv.hi:.2f})</span>"


def _usd(v: float | None) -> str:
    return "—" if v is None else money(v)


def _ms(v: int | None) -> str:
    return "—" if v is None else f"{v:,} ms"


def _efficiency(summary: RunSummary, records: Sequence[Record]) -> str:
    """Per-model efficiency block (EFF-02, D-70): the `efficiency` each model carries in
    summary.json (computed from the records for a summary written before 0.3): cost per case
    and per correct answer and latency p50/p95/p99, each with a seeded 95% bootstrap interval
    over cases; token means per case; the error rate (k of n); generation (system) cost
    against the extractor and judges (eval overhead)."""
    from evalkit.metrics import efficiency_block
    from evalkit.stats import Resamples, rate_wording

    blocks = {m.model: m.efficiency for m in summary.models if m.efficiency is not None}
    if not blocks and records:
        by: dict[str, list[Record]] = {}
        for r in records:
            by.setdefault(r.completion.model, []).append(r)
        for model in sorted(by):
            rows_ = sorted(by[model], key=lambda r: r.case_id)
            blocks[model] = efficiency_block(rows_, Resamples(len(rows_), seed=rows_[0].seed))
    if not blocks:
        return ""

    def ci(iv: Interval | None, fmt: Any) -> str:
        if iv is None:
            return "—"
        return f"{fmt(iv.point)} <span class=muted>({fmt(iv.lo)} to {fmt(iv.hi)})</span>"

    def ms(v: float) -> str:
        return f"{round(v):,} ms"

    rows = []
    for model, e in blocks.items():
        lat = " / ".join(
            ci(iv, ms) for iv in (e.latency_p50_ms, e.latency_p95_ms, e.latency_p99_ms)
        )
        tokens = f"{e.tokens_in_per_case:,.0f} / {e.tokens_out_per_case:,.0f}"
        overhead = "—" if e.eval_overhead_cost_usd is None else money(e.eval_overhead_cost_usd)
        rows.append(
            f"<tr><th scope=row>{escape(model)}</th>"
            f"<td>{ci(e.cost_per_case_usd, money)}</td>"
            f"<td>{ci(e.cost_per_correct_usd, money)}</td>"
            f"<td>{lat}</td><td>{tokens}</td>"
            f"<td>{escape(rate_wording(e.n_errored, e.n))}</td>"
            f"<td>{money(e.system_cost_usd)} / {overhead}</td></tr>"
        )
    return (
        "<h2 id=eff-h>Efficiency</h2><div class=card><table id=efficiency><caption>Per model, "
        "generation calls (live). 95% bootstrap intervals over cases, seeded. System cost is "
        "generation; eval overhead is the extractor and judges for that model's answers."
        "</caption><thead><tr>"
        "<th scope=col>Model</th><th scope=col>Cost per case</th>"
        "<th scope=col>Cost per correct</th><th scope=col>Latency p50 / p95 / p99</th>"
        "<th scope=col>Tokens in / out per case</th><th scope=col>Errors</th>"
        "<th scope=col>System cost / eval overhead</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _point_ci(p: Point) -> str:
    bounded = bounded_rate(p.y, p.lo, p.hi, p.n)
    return escape(bounded) if bounded else f"{p.y:.1%} ({p.lo:.1%}–{p.hi:.1%})"


def _p(p: float) -> str:
    return "&lt;0.001" if p < 0.001 else f"{p:.3f}"


def headline_points(summary: RunSummary) -> list[Point]:
    return [
        Point(m.model, judge, m.cost_per_100_usd, iv.point, iv.lo, iv.hi, m.n)
        for m in summary.models
        for judge, iv in sorted(m.headline.items())
    ]


def _threshold(summary: RunSummary, thresholds: Sequence[object]) -> float | None:
    """The strictest headline bar: all bars must hold, so the highest `>=` one binds."""
    bars = [
        float(t.value)  # type: ignore[attr-defined]
        for t in thresholds
        if getattr(t, "metric", None) == "headline" and getattr(t, "op", None) == ">="
    ]
    return max(bars) if bars else None


def _badge(m: object) -> str:
    failed = getattr(m, "failed_thresholds", ())
    if getattr(m, "passes_threshold", False):
        return '<span class="badge pass">✓ PASS</span>'
    if failed and all(f.endswith(PENDING_SUFFIX) for f in failed):
        return '<span class="badge">… PENDING</span>'  # no verdict on a figure not yet known
    return '<span class="badge fail">✗ FAIL</span>'


def _num(iv: Interval | None) -> str:
    """A task score: any finite number, so not a percent."""
    if iv is None:
        return "—"
    return f"{iv.point:.3g} <span class=muted>({iv.lo:.3g} to {iv.hi:.3g})</span>"


def _cite_head(cite: Sequence[str]) -> str:
    """B5.12 column headers, only for tasks with citation figures."""
    head = "".join(f"<th scope=col>Citation precision · {escape(j)}</th>" for j in cite)
    return head + "".join(f"<th scope=col>Aim-citation rate · {escape(j)}</th>" for j in cite)


def _cite_cells(row: ModelSummary | SliceSummary, cite: Sequence[str]) -> str:
    cells = "".join(f"<td>{_pct(row.citation_precision.get(j))}</td>" for j in cite)
    return cells + "".join(f"<td>{_pct(row.aim_citation_rate.get(j))}</td>" for j in cite)


def _model_table(summary: RunSummary) -> str:
    judges = sorted({j for m in summary.models for j in m.headline})
    tasks = sorted({t for m in summary.models for t in m.task_scores})
    cite = sorted({j for m in summary.models for j in m.citation_precision})
    head = "".join(f"<th scope=col>Headline · {escape(j)}</th>" for j in judges)
    head += "".join(f"<th scope=col>Groundedness · {escape(j)}</th>" for j in judges)
    head += _cite_head(cite)
    tail = "".join(f"<th scope=col>{escape(t)}</th>" for t in tasks)  # v0.2 task scorers
    rows = []
    for m in summary.models:
        failed = "".join(f"<div class=muted>{escape(f)}</div>" for f in m.failed_thresholds)
        errors = ", ".join(f"{escape(k)} {v}" for k, v in sorted(m.n_errors.items())) or "none"
        cells = "".join(f"<td>{_pct(m.headline.get(j), m.n)}</td>" for j in judges)
        cells += "".join(f"<td>{_pct(m.groundedness_mean.get(j), m.n)}</td>" for j in judges)
        cells += _cite_cells(m, cite)
        rows.append(
            f"<tr><th scope=row>{escape(m.model)}<div class=muted>{escape(m.family)}</div></th>"
            f"<td>{_badge(m)}{failed}</td>{cells}<td>{_pct(m.accuracy, m.n)}</td>"
            f"<td>{_pct(m.citation_validity, m.n)}</td><td>{_pct(m.coverage, m.n)}</td>"
            f"<td>{_usd(m.cost_per_correct_usd)}</td><td>{_usd(m.cost_per_100_usd)}</td>"
            f"<td>{_ms(m.latency_p50_ms)} / {_ms(m.latency_p95_ms)}</td>"
            f"<td>{m.n} cases<div class=muted>errors: {errors}</div></td>"
            + "".join(f"<td>{_num(m.task_scores.get(t))}</td>" for t in tasks)
            + "</tr>"
        )
    return (
        "<table id=models><caption>Every rate is over all cases asked; failed calls count as "
        "incorrect and ungrounded. Intervals are 95% bootstrap CIs.</caption><thead><tr>"
        f"<th scope=col>Model</th><th scope=col>Bar</th>{head}<th scope=col>Accuracy</th>"
        "<th scope=col>Citation validity</th><th scope=col>Coverage</th>"
        "<th scope=col>Cost per correct</th><th scope=col>Cost per 100</th>"
        f"<th scope=col>Latency p50 / p95</th><th scope=col>Cases</th>{tail}</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _slices(summary: RunSummary) -> str:
    """v0.2 (D-59): one table per slice field, one row per value and model."""
    if not summary.slices:
        return ""
    judges = sorted({j for s in summary.slices for j in s.headline})
    tasks = sorted({t for s in summary.slices for t in s.task_scores})
    cite = sorted({j for s in summary.slices for j in s.citation_precision})
    tables = []
    for field in dict.fromkeys(s.field for s in summary.slices):
        head = "".join(f"<th scope=col>Headline · {escape(j)}</th>" for j in judges)
        head += "".join(f"<th scope=col>Groundedness · {escape(j)}</th>" for j in judges)
        head += _cite_head(cite)
        tail = "".join(f"<th scope=col>{escape(t)}</th>" for t in tasks)
        rows = []
        for s in (s for s in summary.slices if s.field == field):
            errors = ", ".join(f"{escape(k)} {v}" for k, v in sorted(s.n_errors.items())) or "none"
            cells = "".join(f"<td>{_pct(s.headline.get(j), s.n)}</td>" for j in judges)
            cells += "".join(f"<td>{_pct(s.groundedness_mean.get(j), s.n)}</td>" for j in judges)
            cells += _cite_cells(s, cite)
            cells += "".join(f"<td>{_num(s.task_scores.get(t))}</td>" for t in tasks)
            rows.append(
                f"<tr><th scope=row>{escape(s.value)}</th><td>{escape(s.model)}</td>"
                f"<td>{s.n}<div class=muted>errors: {errors}</div></td>{cells}"
                f"<td>{_pct(s.accuracy, s.n)}</td><td>{_pct(s.citation_validity, s.n)}</td>"
                f"<td>{_pct(s.coverage, s.n)}</td><td>{_usd(s.cost_per_100_usd)}</td>"
                f"<td>{_ms(s.latency_p50_ms)} / {_ms(s.latency_p95_ms)}</td></tr>"
            )
        tables.append(
            f"<table class=slices><caption>By {escape(field)}: every rate is over that slice's "
            "cases only, with a 95% bootstrap CI drawn over those cases.</caption><thead><tr>"
            f"<th scope=col>{escape(field)}</th><th scope=col>Model</th><th scope=col>Cases</th>"
            f"{head}{tail}<th scope=col>Accuracy</th><th scope=col>Citation validity</th>"
            "<th scope=col>Coverage</th><th scope=col>Cost per 100</th>"
            "<th scope=col>Latency p50 / p95</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return f"<h2 id=slices-h>Slices</h2><div class=card>{''.join(tables)}</div>"


def _pairs_table(summary: RunSummary) -> str:
    if not summary.comparisons:
        return "<p class=muted>Only one model: no comparisons.</p>"
    rows = []
    for c in summary.comparisons:
        verdict = (
            '<span class="badge pass">separated</span>'
            if c.separated
            else '<span class="badge">tie</span>'
        )
        metric = c.metric if c.judge is None else f"{c.metric} · {c.judge}"
        rows.append(
            f"<tr><td>{escape(metric)}</td><td>{escape(c.model_a)} − {escape(c.model_b)}</td>"
            f"<td>{c.diff.point:+.1%} ({c.diff.lo:+.1%} to {c.diff.hi:+.1%})</td>"
            f"<td>{_p(c.p_value)}</td><td>{verdict}</td></tr>"
        )
    return (
        "<table id=pairs><caption>Paired bootstrap on shared cases. Difference intervals are "
        "unadjusted 95%; “separated” survives a Holm correction across the model pairs of each "
        "metric and judge, so an interval that excludes 0 can still be a tie.</caption><thead>"
        "<tr><th scope=col>Metric</th><th scope=col>Pair</th>"
        "<th scope=col>Difference (unadjusted 95%)</th><th scope=col>p</th>"
        "<th scope=col>Call</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _calibration(
    summary: RunSummary,
    calibration: CalibrationReport | None,
    labeller: Labeller | None = None,
) -> str:
    if summary.calibration_status != "calibrated":
        return (
            "<p><strong>Judges not calibrated for this task.</strong> Groundedness figures have "
            "not been checked against hand labels for this task and rubric. See "
            f'<a href="{GUIDE_URL}"><code>docs/calibrate-your-task.md</code></a>.</p>'
        )
    rows = []
    for a in summary.judges:
        (tp, fn), (fp, tn) = a.confusion
        fams = ", ".join(f"{escape(f)} {_kappa(iv)}" for f, iv in sorted(a.by_family.items()))
        same = (
            "<br><small>same model as the labeller: not independent</small>"
            if labeller is not None and labeller.same_as(a.judge)
            else ""
        )
        rows.append(
            f"<tr><th scope=row>{escape(a.judge)}{same}</th><td>{_kappa(a.kappa)}</td>"
            f"<td>{a.agreement:.1%}</td><td>{a.n_claims} ({a.coverage:.0%} covered)</td>"
            f"<td>TP {tp} · FN {fn} · FP {fp} · TN {tn}</td><td>{fams or '—'}</td></tr>"
        )
    extra = ""
    if calibration is not None:
        extra = (
            f"<p>Extraction error rate {calibration.extraction_error_rate:.1%} · "
            f"{'labeller' if labeller is not None and labeller.kind == 'model' else 'owner'} "
            f"self-consistency kappa {_kappa(calibration.self_consistency)} · rubric "
            f"{escape(calibration.judge_prompt_hash)}</p>"
        )
    return (
        "<table id=calibration><caption>Agreement with "
        + (
            f"the held-out {escape(labeller.labels())}"
            if labeller is not None and labeller.kind == "model"
            else "the owner's held-out labels"
        )
        + " (“supported” is positive). Kappa is a coefficient from −1 to 1; its intervals "
        "resample whole answers.</caption><thead><tr>"
        "<th scope=col>Judge</th><th scope=col>Kappa</th><th scope=col>Agreement</th>"
        "<th scope=col>Claims</th><th scope=col>Confusion</th><th scope=col>Kappa by family</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + extra
    )


def _self_preference(summary: RunSummary) -> str:
    if summary.judge_mode != "dual" or not summary.self_preference:
        return "<p class=muted>Single judge (or judging pending): self-preference not measured.</p>"
    fams = sorted({f for row in summary.self_preference.values() for f in row})
    head = "".join(f"<th scope=col>{escape(f)} answers</th>" for f in fams)
    n_cases = summary.models[0].n if summary.models else None  # every model answers every case
    rows = "".join(
        f"<tr><th scope=row>{escape(j)}</th>"
        + "".join(f"<td>{_pct(row.get(f), n_cases)}</td>" for f in fams)
        + "</tr>"
        for j, row in sorted(summary.self_preference.items())
    )
    return (
        "<table id=selfpref><caption>Mean groundedness each judge gave each generator family. "
        "A judge rating its own family higher than the other judge does is a warning sign."
        f"</caption><thead><tr><th scope=col>Judge</th>{head}</tr></thead><tbody>{rows}"
        "</tbody></table>"
    )


def failures(records: Sequence[Record], judges: Sequence[str]) -> list[tuple[Record, list[str]]]:
    """Answers that are not correct-and-grounded, with the judges they failed for. A judge
    with no score entry for a record counts as failed (not grounded)."""
    out = []
    for r in records:
        if r.scores is None:
            continue
        failed = [j for j in judges if not r.scores.correct_and_grounded.get(j, False)]
        if failed:
            out.append((r, failed))
    return out


def _explorer_item(r: Record, failed: list[str], judges: list[str], case: Case | None) -> str:
    answer = r.completion.answer
    label = answer.label if answer else f"no answer ({r.completion.error})"
    ref = (case.reference_label or "unlabelled") if case else "unknown"
    by_claim: dict[str, dict[str, str]] = {}
    for v in r.verdicts:
        ev = f" · evidence {escape(str(list(v.evidence)))}" if v.evidence else ""
        quote = f" · “{escape(v.quote)}”" if v.quote else ""
        by_claim.setdefault(v.claim_id, {})[v.judge] = f"{escape(v.verdict)}{ev}{quote}"
    jh = "".join(f"<th scope=col>{escape(j)}</th>" for j in judges)
    claim_rows = "".join(
        f"<tr><th scope=row>{escape(c.text)}</th><td>{escape(str(list(c.cited)))}</td>"
        + "".join(f"<td>{by_claim.get(c.id, {}).get(j, '—')}</td>" for j in judges)
        + "</tr>"
        for c in r.claims
    )
    claims = (
        "<table><caption>Claims and each judge's verdict</caption><thead><tr>"
        f"<th scope=col>Claim</th><th scope=col>Cites</th>{jh}</tr></thead><tbody>"
        f"{claim_rows}</tbody></table>"
        if r.claims
        else "<p class=muted>No claims (failed call or extraction).</p>"
    )
    cited = sorted({n for c in r.claims for n in c.cited} | set(answer.citations if answer else ()))
    n_ctx = len(case.context) if case else 0
    sents = "".join(
        f"<div class=sent><span class=n>[{n}]</span>{escape(case.context[n - 1])}</div>"
        if case and 1 <= n <= n_ctx
        else f"<div class=sent><span class=n>[{n}]</span><em>not in the source "
        f"({n_ctx} sentences): an invalid citation</em></div>"
        for n in cited
    )
    question = (
        f"<p><strong>Question.</strong> {escape(case.question)}</p>"
        if case
        else "<p class=muted>Case not found in the dataset passed to the report.</p>"
    )
    return (
        f'<details data-model="{escape(r.completion.model)}" '
        f'data-judges="{escape("|".join(failed))}"><summary>{escape(r.completion.model)} · '
        f"{escape(r.case_id)} · answered {escape(label)}, reference {escape(ref)} · "
        f"failed for {escape(', '.join(failed))}</summary><div>{question}"
        f"<p><strong>Answer.</strong> {escape(answer.answer if answer else '')}</p>{claims}"
        + (f"<p><strong>Cited sentences</strong></p>{sents}" if sents else "")
        + "</div></details>"
    )


def _failure_explorer(
    summary: RunSummary, records: Sequence[Record], cases: Mapping[str, Case]
) -> str:
    judges = sorted({j for m in summary.models for j in m.headline})
    if summary.judging_status != "complete":
        return "<p class=muted>Judging pending: the explorer fills in once judging ends.</p>"
    items = failures(records, judges)
    unscored = sum(1 for r in records if r.scores is None)
    models = sorted({r.completion.model for r, _ in items})
    opts_m = "".join(f'<option value="{escape(m)}">{escape(m)}</option>' for m in models)
    opts_j = "".join(f'<option value="{escape(j)}">{escape(j)}</option>' for j in judges)
    note = f" {unscored} records have no scores yet and are not listed." if unscored else ""
    return (
        f"<p>{len(items)} answers are not correct-and-grounded for at least one judge.{note}</p>"
        f'<label for="f-model">Model <select id="f-model"><option value="">all</option>{opts_m}'
        f'</select></label><label for="f-judge">Failed for judge <select id="f-judge">'
        f'<option value="">any</option>{opts_j}</select></label><div id="failures">'
        + "".join(_explorer_item(r, f, judges, cases.get(r.case_id)) for r, f in items)
        + "</div>"
    )


def render_report(
    summary: RunSummary,
    records: Sequence[Record],
    cases: Mapping[str, Case],
    *,
    thresholds: Sequence[object] = (),
    calibration: CalibrationReport | None = None,
    labeller: Labeller | None = None,
) -> str:
    stamps = stamp_lines(summary)
    stamp_html = (
        "<ul class=stamps>" + "".join(f"<li>{escape(s)}</li>" for s in stamps) + "</ul>"
        if stamps
        else ""
    )
    finished = summary.finished_at.isoformat() if summary.finished_at else "judging pending"
    meta = (
        f"<p class=meta>Judges: {escape(summary.judge_mode)} · calibration: "
        f"{escape(summary.calibration_status)} · judging: {escape(summary.judging_status)} · "
        f"started {escape(summary.started_at.isoformat())} · finished {escape(finished)}</p>"
    )
    if summary.judging_status == "complete":
        points = headline_points(summary)
        chart = scatter(
            points,
            title="Headline quality against cost",
            desc=(
                "Share of cases answered correctly and fully grounded, per model and judge, "
                "against cost per 100 cases, with 95% confidence intervals."
            ),
            x_label="Cost per 100 cases (USD)",
            y_label="Correct and fully grounded",
            threshold=_threshold(summary, thresholds),
        )
        table = (
            "<table id=points><caption>Data for the chart</caption><thead><tr>"
            "<th scope=col>Model</th><th scope=col>Judge</th><th scope=col>Headline (95% CI)</th>"
            "<th scope=col>Cost per 100</th></tr></thead><tbody>"
            + "".join(
                f"<tr><th scope=row>{escape(p.label)}</th><td>{escape(p.series)}</td>"
                f"<td>{_point_ci(p)}</td><td>{money(p.x)}</td></tr>"
                for p in points
            )
            + "</tbody></table>"
        )
        headline = f"<div class=card>{chart}</div><div class=card>{table}</div>"
    else:
        headline = (
            "<div class=card><p><strong>Judging pending.</strong> Headline and groundedness "
            "figures appear once every judge finishes. Deterministic metrics are below.</p></div>"
        )
    title = f"EvalKit · {summary.task} · {summary.run_id}"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}</style></head><body><main>"
        f'<p class=banner role="note">{escape(BANNER)}</p>'
        f"<h1>{escape(summary.task)} · run {escape(summary.run_id)}</h1>{meta}{stamp_html}"
        f"<h2 id=headline>Headline</h2>{headline}"
        f"<h2 id=models-h>Models</h2><div class=card>{_model_table(summary)}</div>"
        f"{_efficiency(summary, records)}"
        f"{_slices(summary)}"
        f"<h2 id=pairs-h>Pairwise comparisons</h2><div class=card>{_pairs_table(summary)}</div>"
        f"<h2 id=cal-h>Judge calibration</h2><div class=card>"
        f"{_calibration(summary, calibration, labeller)}</div>"
        f"<h2 id=sp-h>Self-preference</h2><div class=card>{_self_preference(summary)}</div>"
        f"<h2 id=fail-h>Failure explorer</h2>"
        f"<div class=card>{_failure_explorer(summary, records, cases)}</div>"
        f"</main><script>{JS}</script></body></html>\n"
    )


def vega_lite_specs(summary: RunSummary, thresholds: Sequence[object] = ()) -> dict[str, Any]:
    """The same headline chart as a Vega-Lite spec, for anyone who wants interactivity."""
    data = [
        {
            "model": p.label,
            "judge": p.series,
            "cost_per_100_usd": p.x,
            "headline": p.y,
            "lo": p.lo,
            "hi": p.hi,
        }
        for p in headline_points(summary)
    ]
    enc_x = {
        "field": "cost_per_100_usd",
        "type": "quantitative",
        "scale": {
            "type": "log" if use_log_scale([d["cost_per_100_usd"] for d in data]) else "linear"
        },
        "title": "Cost per 100 cases (USD)",
    }
    bar = _threshold(summary, thresholds)
    spec = _vl_spec(data, enc_x)
    if bar is not None:
        spec["headline"]["layer"].append(
            {"mark": {"type": "rule", "strokeDash": [6, 4]}, "encoding": {"y": {"datum": bar}}}
        )
    return spec


def _vl_spec(data: list[dict[str, Any]], enc_x: dict[str, Any]) -> dict[str, Any]:
    return {
        "headline": {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Headline (correct and fully grounded) against cost, 95% CIs",
            "data": {"values": data},
            "layer": [
                {
                    "mark": "rule",
                    "encoding": {
                        "x": enc_x,
                        "y": {
                            "field": "lo",
                            "type": "quantitative",
                            "title": "Correct and grounded",
                        },
                        "y2": {"field": "hi"},
                    },
                },
                {
                    "mark": {"type": "point", "filled": True, "size": 80},
                    "encoding": {
                        "x": enc_x,
                        "y": {
                            "field": "headline",
                            "type": "quantitative",
                            "scale": {"domain": [0, 1]},
                        },
                        "shape": {"field": "judge", "type": "nominal"},
                        "color": {"field": "model", "type": "nominal"},
                        "tooltip": [
                            {"field": f}
                            for f in ("model", "judge", "headline", "lo", "hi", "cost_per_100_usd")
                        ],
                    },
                },
            ],
        }
    }


def write_report(
    out_dir: Path,
    summary: RunSummary,
    records: Sequence[Record],
    cases: Mapping[str, Case],
    *,
    thresholds: Sequence[object] = (),
    calibration: CalibrationReport | None = None,
    labeller: Labeller | None = None,
) -> Path:
    """Write `report.html` and `charts.vl.json` atomically; return the report path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "report.html": render_report(
            summary,
            records,
            cases,
            thresholds=thresholds,
            calibration=calibration,
            labeller=labeller,
        ),
        "charts.vl.json": json.dumps(vega_lite_specs(summary, thresholds), indent=2, sort_keys=True)
        + "\n",
    }
    for name, text in files.items():
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{name}-")
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; outputs are meant to be shared
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, out_dir / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return out_dir / "report.html"
