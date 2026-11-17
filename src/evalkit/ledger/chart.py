"""Trend charts, the PSI panel and the history page (B5.7 LED-05, spec §5).

Pure functions of the ledger: no randomness, no timestamps, fixed number formatting, so the
same ledger renders the same bytes. Every chart has a <title> and <desc>; a flagged run is
drawn with its own shape and a text label (never colour alone); learning runs have no band;
settings changes and band resets are vertical markers. Built on `evalkit.report.svg`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from html import escape

from evalkit.ledger.runs import History, PsiSeries, Series, SpreadRow, TrendPoint, _shown
from evalkit.report.svg import nice_ticks

W, H = 720, 300
PAD_L, PAD_R, PAD_T, PAD_B = 76, 24, 28, 64
PSI_WARN_LINE, PSI_ALARM_LINE = 0.1, 0.25
SHAPE_OF = {
    "ok": "circle",
    "learning": "hollow",
    "degenerate": "circle",
    "warn": "triangle",
    "alarm": "diamond",
    "improved": "square",
    "stopped": "cross",
}
LABEL_OF = {"warn": "warn", "alarm": "alarm", "improved": "improved", "stopped": "stopped"}

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5f5e58;--card:#fff;--line:#dedad0;--grid:#ebe7de;
--warn:#8a5a00;--alarm:#a1261b;--ok:#2f5d50;--band-w:#f3e7c4;--band-a:#f7dcd6}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--fg:#ecebe6;--muted:#aeada6;
--card:#1e1e1b;--line:#3a3934;--grid:#2c2b27;--warn:#f5c56b;--alarm:#ff9d92;--ok:#8cc4b0;
--band-w:#3b3112;--band-a:#45201b}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;
margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
.trend{width:100%;height:auto;max-width:760px}
.trend .grid{stroke:var(--grid)}.trend text{fill:var(--fg);font-size:12px}
.trend .band-a{fill:var(--band-a)}.trend .band-w{fill:var(--band-w)}
.trend .median{stroke:var(--ok);stroke-width:2}.trend .whisker{stroke:var(--fg)}
.trend .mark{fill:var(--fg)}.trend .hollow{fill:none;stroke:var(--fg);stroke-width:1.5}
.trend .warn{fill:var(--warn)}.trend .alarm{fill:var(--alarm)}
.trend .settings{stroke:var(--muted);stroke-dasharray:4 3}.trend .reset{stroke:var(--fg)}
.trend .psi-warn{stroke:var(--warn);stroke-dasharray:6 4}
.trend .psi-alarm{stroke:var(--alarm);stroke-dasharray:6 4}.trend .line{stroke:var(--fg);fill:none}
"""


def _f(v: float) -> str:
    return f"{v:.2f}"


def num(v: object) -> str:
    """A figure as shown: bools as true/false, numbers to 6 significant digits."""
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{v:.6g}"
    return escape(str(v))


def _labels(labels: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items()) or "(no labels)"


def _mark(kind: str, cx: float, cy: float, cls: str, r: float = 5.5) -> str:
    if kind == "hollow":
        return f'<circle class="hollow" cx="{_f(cx)}" cy="{_f(cy)}" r="{r}"/>'
    if kind == "circle":
        return f'<circle class="{cls}" cx="{_f(cx)}" cy="{_f(cy)}" r="{r}"/>'
    if kind == "square":
        return (
            f'<rect class="{cls}" x="{_f(cx - r)}" y="{_f(cy - r)}" width="{2 * r}" '
            f'height="{2 * r}"/>'
        )
    if kind == "cross":
        return (
            f'<path class="hollow" d="M{_f(cx - r)},{_f(cy - r)}L{_f(cx + r)},{_f(cy + r)}'
            f'M{_f(cx - r)},{_f(cy + r)}L{_f(cx + r)},{_f(cy - r)}"/>'
        )
    if kind == "diamond":
        pts = [(cx, cy - r - 1.5), (cx + r + 1.5, cy), (cx, cy + r + 1.5), (cx - r - 1.5, cy)]
    else:  # triangle
        pts = [(cx, cy - r - 1.5), (cx + r + 1.5, cy + r), (cx - r - 1.5, cy + r)]
    return f'<polygon class="{cls}" points="' + " ".join(f"{_f(a)},{_f(b)}" for a, b in pts) + '"/>'


def _y_range(values: Iterable[float]) -> tuple[float, float, list[float]]:
    vs = [v for v in values if math.isfinite(v)]
    lo, hi = (min(vs), max(vs)) if vs else (0.0, 1.0)
    if hi == lo:
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    ticks = nice_ticks(lo, hi)
    return ticks[0], ticks[-1], ticks


def _frame(
    lo: float, hi: float, ticks: Sequence[float], y_label: str
) -> tuple[str, Callable[[float], float]]:
    plot_h = H - PAD_T - PAD_B

    def sy(v: float) -> float:
        return PAD_T + (hi - v) / (hi - lo) * plot_h

    parts = []
    for t in ticks:
        y = sy(t)
        parts.append(
            f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{_f(y)}" y2="{_f(y)}"/>'
            f'<text x="{PAD_L - 8}" y="{_f(y + 4)}" text-anchor="end">{num(t)}</text>'
        )
    parts.append(
        f'<text x="16" y="{_f(PAD_T + plot_h / 2)}" text-anchor="middle" '
        f'transform="rotate(-90 16 {_f(PAD_T + plot_h / 2)})">{escape(y_label)}</text>'
    )
    return "".join(parts), sy


def _x(i: int, n: int) -> float:
    return PAD_L + (i + 0.5) / n * (W - PAD_L - PAD_R)


def _x_axis(points: Sequence[tuple[int, str]], n: int) -> str:
    step = max(1, math.ceil(n / 12))
    out = []
    for i, (index, date) in enumerate(points):
        if i % step and i != n - 1:
            continue
        x = _x(i, n)
        out.append(
            f'<text x="{_f(x)}" y="{H - PAD_B + 18}" text-anchor="middle">#{index}</text>'
            f'<text x="{_f(x)}" y="{H - PAD_B + 34}" text-anchor="middle">{escape(date)}</text>'
        )
    out.append(
        f'<text x="{_f((PAD_L + W - PAD_R) / 2)}" y="{H - 8}" text-anchor="middle">'
        "run (order in the ledger, date)</text>"
    )
    return "".join(out)


def _domain(unit: str) -> tuple[float, float]:
    """Where a unit's values can lie: a band is drawn only inside it (a rate in [0, 1], counts,
    times and money at or above 0)."""
    if unit in ("rate", "bool"):
        return 0.0, 1.0
    if unit in ("count", "ms", "s") or unit.isupper():
        return 0.0, math.inf
    return -math.inf, math.inf


def _value(p: TrendPoint) -> float:
    return float(p.value)


def trend_svg(series: Series, *, chart_id: str = "trend") -> str:
    """One deterministic inline SVG for a (metric, labels): values with interval whiskers, the
    median of each run's band, its warn (±2 spreads) and alarm (±3 spreads) bands shaded,
    flagged runs by shape and text label, settings changes and band resets as markers."""
    pts = series.points
    n = len(pts)
    shown = _shown(series.labels)
    floor, ceiling = _domain(series.unit)

    def clip(v: float) -> float:
        return min(max(v, floor), ceiling)

    ys: list[float] = []
    for p in pts:
        ys.append(_value(p))
        ys += [float(v) for v in (p.ci_low, p.ci_high) if v is not None]
        if p.centre is not None and p.spread is not None:
            ys += [clip(p.centre - 3 * p.spread), clip(p.centre + 3 * p.spread)]
    lo, hi, ticks = _y_range(ys)
    grid, sy = _frame(lo, hi, ticks, f"{series.metric} ({series.unit})")
    half = (W - PAD_L - PAD_R) / n / 2
    body: list[str] = []
    for i, p in enumerate(pts):
        if p.centre is None or p.spread is None:
            continue
        x = _x(i, n)
        c, s = p.centre, p.spread
        for cls, k in (("band-a", 3), ("band-w", 2)):
            top, bottom = sy(clip(c + k * s)), sy(clip(c - k * s))
            body.append(
                f'<rect class="{cls}" x="{_f(x - half)}" y="{_f(top)}" width="{_f(2 * half)}" '
                f'height="{_f(max(bottom - top, 0.0))}"/>'
            )
        ym = sy(c)
        body.append(
            f'<line class="median" x1="{_f(x - half)}" x2="{_f(x + half)}" '
            f'y1="{_f(ym)}" y2="{_f(ym)}"/>'
        )
    for i, p in enumerate(pts):
        x0 = _x(i, n) - half
        if p.reset_before:
            body.append(
                f'<line class="reset" x1="{_f(x0)}" x2="{_f(x0)}" y1="{PAD_T}" '
                f'y2="{H - PAD_B}"/><text x="{_f(x0 + 3)}" y="{H - PAD_B - 4}">band reset</text>'
            )
        elif p.settings_changed:
            body.append(
                f'<line class="settings" x1="{_f(x0)}" x2="{_f(x0)}" y1="{PAD_T}" '
                f'y2="{H - PAD_B}"/><text x="{_f(x0 + 3)}" y="{H - PAD_B - 4}">settings</text>'
            )
    for i, p in enumerate(pts):
        x, y = _x(i, n), sy(_value(p))
        if p.ci_low is not None and p.ci_high is not None:
            body.append(
                f'<line class="whisker" x1="{_f(x)}" x2="{_f(x)}" '
                f'y1="{_f(sy(float(p.ci_high)))}" y2="{_f(sy(float(p.ci_low)))}"/>'
            )
        cls = "alarm" if p.status == "alarm" else "warn" if p.status == "warn" else "mark"
        body.append(_mark(SHAPE_OF.get(p.status, "circle"), x, y, cls))
        label = LABEL_OF.get(p.status)
        if label:
            text = label + (f" ({', '.join(p.hints)})" if p.hints else "")
            body.append(
                f'<text x="{_f(x)}" y="{_f(y - 11)}" text-anchor="middle">{escape(text)}</text>'
            )
    latest = series.latest
    title = f"{series.metric} · {_labels(shown)}"
    desc = (
        f"{n} run(s) of {series.metric} ({_labels(shown)}). Latest {num(latest.value)}: "
        f"status {latest.status}"
        + (
            f", band centre {num(latest.centre)}, spread {num(latest.spread)}, warn beyond "
            f"{num(2 * latest.spread)} and alarm beyond {num(3 * latest.spread)} from the centre"
            if latest.centre is not None and latest.spread is not None
            else ""
        )
        + ". Shapes: circle ok, hollow circle learning, triangle warn, diamond alarm, square "
        "improved, cross stopped at the cost cap."
    )
    return (
        f'<svg class="trend" id="{escape(chart_id)}" viewBox="0 0 {W} {H}" role="img" '
        f'aria-labelledby="{escape(chart_id)}-t {escape(chart_id)}-d" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<title id="{escape(chart_id)}-t">{escape(title)}</title>'
        f'<desc id="{escape(chart_id)}-d">{escape(desc)}</desc>'
        + grid
        + "".join(body)
        + _x_axis([(p.index, p.at[:10]) for p in pts], n)
        + "</svg>"
    )


def psi_svg(series: PsiSeries, *, chart_id: str = "psi") -> str:
    """The PSI of one feature across drift checks recorded in the history, with lines at the
    default warn (0.1) and alarm (0.25) levels; each point's level by shape and label."""
    pts = series.points
    n = len(pts)
    lo, hi, ticks = _y_range([0.0, PSI_ALARM_LINE * 1.2, *(v for _, _, v in pts)])
    grid, sy = _frame(lo, hi, ticks, "PSI")
    body = []
    for cls, level, name in (
        ("psi-warn", PSI_WARN_LINE, "warn 0.1"),
        ("psi-alarm", PSI_ALARM_LINE, "alarm 0.25"),
    ):
        y = sy(level)
        body.append(
            f'<line class="{cls}" x1="{PAD_L}" x2="{W - PAD_R}" y1="{_f(y)}" y2="{_f(y)}"/>'
            f'<text x="{W - PAD_R - 4}" y="{_f(y - 4)}" text-anchor="end">{name}</text>'
        )
    if n > 1:
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{_f(_x(i, n))},{_f(sy(v))}" for i, (_, _, v) in enumerate(pts)
        )
        body.append(f'<path class="line" d="{path}"/>')
    for i, (_, _, v) in enumerate(pts):
        level = "alarm" if v > PSI_ALARM_LINE else "warn" if v >= PSI_WARN_LINE else "ok"
        x, y = _x(i, n), sy(v)
        cls = level if level != "ok" else "mark"
        body.append(_mark(SHAPE_OF[level], x, y, cls))
        if level != "ok":
            body.append(f'<text x="{_f(x)}" y="{_f(y - 11)}" text-anchor="middle">{level}</text>')
    title = f"PSI · {series.feature}"
    desc = (
        f"PSI of feature {series.feature} ({series.metric}) over {n} drift check(s); latest "
        f"{num(pts[-1][2]) if pts else '–'}. Lines at 0.1 (warn) and 0.25 (alarm); triangle "
        "warn, diamond alarm."
    )
    return (
        f'<svg class="trend" id="{escape(chart_id)}" viewBox="0 0 {W} {H}" role="img" '
        f'aria-labelledby="{escape(chart_id)}-t {escape(chart_id)}-d" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<title id="{escape(chart_id)}-t">{escape(title)}</title>'
        f'<desc id="{escape(chart_id)}-d">{escape(desc)}</desc>'
        + grid
        + "".join(body)
        + _x_axis([(i, r) for i, r, _ in pts], n)
        + "</svg>"
    )


# ---- tables and pages ---------------------------------------------------------------------------


def _band_text(s: Series) -> str:
    b = s.latest
    if b.centre is None or b.spread is None:
        return "–"
    return f"{num(b.centre)} ± {num(2 * b.spread)} / {num(3 * b.spread)}"


def summary_table(series: Sequence[Series]) -> str:
    rows = []
    for s in series:
        b = s.latest
        ci = f" [{num(b.ci_low)}, {num(b.ci_high)}]" if b.ci_low is not None else ""
        rows.append(
            f"<tr><th scope=row>{escape(s.metric)}</th><td>{escape(_labels(_shown(s.labels)))}"
            f"</td><td>{len(s.points)}</td><td>{num(b.value)}{ci}</td><td>{_band_text(s)}</td>"
            f"<td>{escape(b.status)}</td><td>{escape(', '.join(b.hints))}</td></tr>"
        )
    return (
        "<table><caption>Latest run per metric against its history (centre ± warn / alarm "
        "half-widths)</caption><thead><tr><th scope=col>Metric</th><th scope=col>Labels</th>"
        "<th scope=col>Runs</th><th scope=col>Latest [95% CI]</th><th scope=col>Band</th>"
        "<th scope=col>Status</th><th scope=col>Hints</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def spread_table(spread: Sequence[SpreadRow]) -> str:
    rows = "".join(
        f"<tr><th scope=row>{escape(r.model)}</th><td>{escape(r.mode or '–')}</td>"
        f"<td>{escape(r.name)}</td><td>{r.runs}</td><td>{num(r.median)}</td>"
        f"<td>{num(r.mad_spread)}</td><td>{num(r.cv)}</td><td>{num(r.min)}</td>"
        f"<td>{num(r.max)}</td><td>{num(r.latest)}</td><td>{escape(r.status)}</td></tr>"
        for r in spread
    )
    return (
        "<table><caption>Efficiency run-to-run variation (EFF-03): MAD spread = 1.4826 × MAD, "
        "CV = spread / |median|</caption><thead><tr><th scope=col>Model</th>"
        "<th scope=col>Mode</th><th scope=col>Figure</th><th scope=col>Runs</th>"
        "<th scope=col>Median</th><th scope=col>MAD spread</th><th scope=col>CV</th>"
        "<th scope=col>Min</th><th scope=col>Max</th><th scope=col>Latest</th>"
        "<th scope=col>Status</th></tr></thead><tbody>" + rows + "</tbody></table>"
    )


def _points_table(s: Series) -> str:
    rows = "".join(
        f"<tr><td>#{p.index}</td><td>{escape(p.run_id)}</td><td>{escape(p.at[:10])}</td>"
        f"<td>{num(p.value)}</td><td>{num(p.ci_low)}</td><td>{num(p.ci_high)}</td>"
        f"<td>{num(p.centre)}</td><td>{num(p.spread)}</td><td>{escape(p.status)}</td></tr>"
        for p in s.points
    )
    return (
        "<details><summary>Data for the chart</summary><div><table><thead><tr>"
        "<th scope=col>Run</th><th scope=col>Run id</th><th scope=col>Date</th>"
        "<th scope=col>Value</th><th scope=col>CI low</th><th scope=col>CI high</th>"
        "<th scope=col>Centre</th><th scope=col>Spread</th><th scope=col>Status</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table></div></details>"
    )


def trend_section(hist: History, series: Sequence[Series] | None = None) -> str:
    """The "Trend" section: one chart and data table per series, then the PSI panel."""
    chosen = hist.series if series is None else list(series)
    charts = "".join(
        f'<div class="card">{trend_svg(s, chart_id=f"trend-{i}")}{_points_table(s)}</div>'
        for i, s in enumerate(chosen, start=1)
    )
    psi = "".join(
        f'<div class="card">{psi_svg(p, chart_id=f"psi-{i}")}</div>'
        for i, p in enumerate(hist.psi, start=1)
    )
    return (
        '<h2 id="trend-h">Trend</h2>'
        + (charts or '<div class="card"><p>No history for these metrics yet.</p></div>')
        + ('<h3 id="psi-h">PSI drift</h3>' + psi if psi else "")
    )


def history_html(hist: History, *, title: str = "Run history") -> str:
    """The whole history page (`evalkit history show --html`)."""
    project = escape(hist.project or "(empty ledger)")
    spread = (
        f'<h2 id="eff-h">Efficiency spread</h2><div class="card">{spread_table(hist.spread)}</div>'
        if hist.spread
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} · {project}</title><style>{CSS}</style></head><body><main>"
        f"<h1>{escape(title)} · {project}</h1>"
        f"<p>{hist.runs} run(s). Bands: median ± 2 (warn) and ± 3 (alarm) spreads, spread = "
        "max(1.4826 × MAD, median SE of the history, SE of the run), from at least 5 "
        "comparable runs (the last 20, after any band reset).</p>"
        f'<h2 id="latest-h">Latest</h2><div class="card">{summary_table(hist.series)}</div>'
        + spread
        + trend_section(hist)
        + "</main></body></html>\n"
    )


def history_markdown(hist: History) -> str:
    lines = [
        f"# Run history · {hist.project or '(empty ledger)'}",
        "",
        f"{hist.runs} run(s).",
        "",
        "| Metric | Labels | Runs | Latest | Band (centre ± warn / alarm) | Status | Hints |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in hist.series:
        b = s.latest
        lines.append(
            f"| {s.metric} | {_labels(_shown(s.labels))} | {len(s.points)} | {num(b.value)} | "
            f"{_band_text(s)} | {b.status} | {', '.join(b.hints)} |"
        )
    if hist.spread:
        lines += [
            "",
            "## Efficiency spread (EFF-03)",
            "",
            "| Model | Mode | Figure | Runs | Median | MAD spread | CV | Min | Max | Latest "
            "| Status |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in hist.spread:
            lines.append(
                f"| {r.model} | {r.mode or '–'} | {r.name} | {r.runs} | {num(r.median)} | "
                f"{num(r.mad_spread)} | {num(r.cv)} | {num(r.min)} | {num(r.max)} | "
                f"{num(r.latest)} | {r.status} |"
            )
    return "\n".join(lines) + "\n"
