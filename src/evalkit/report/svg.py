"""Accessible inline-SVG charts for the run report (D-42).

Charts are pure functions of their data: no randomness, no timestamps, fixed number
formatting, so the same summary always renders the same bytes. Every chart carries a
<title> and <desc>, marks differ by shape as well as colour, and the report places a data
table next to each chart.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

W, H = 720, 420
PAD_L, PAD_R, PAD_T, PAD_B = 70, 170, 30, 60
SHAPES = ("circle", "square", "diamond", "triangle")
SERIES_GAP = 12  # px between one model's judge marks
MODEL_GAP = 14  # extra px between models that share a cost column
LABEL_MAX = 22  # characters shown in labels; full names live in the tooltips and table


@dataclass(frozen=True)
class Point:
    label: str  # model name
    series: str  # judge name
    x: float  # cost per 100 cases (USD)
    y: float  # headline point
    lo: float
    hi: float
    n: int | None = None  # cases behind the point (for the GAT-07 wording at 0 and 1)


def bounded_rate(point: float, lo: float, hi: float, n: int | None) -> str | None:
    """GAT-07: a share of n cases that is exactly 0 or 1 is never written as "0.0% (0.0% to
    0.0%)". With n it reads "0 of n, below X% at 95%" (exact one-sided bound; a mean of values
    in [0, 1] that is 0 over n cases obeys the same bound); without n, "none observed (no
    interval)". None when the point is strictly between 0 and 1 (the bootstrap CI applies)."""
    if point not in (0.0, 1.0) or lo != hi:
        return None
    if n:
        from evalkit.stats import rate_wording

        return rate_wording(0 if point == 0.0 else n, n)
    return "none observed (no interval)" if point == 0.0 else "all observed (no interval)"


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def money(v: float) -> str:
    """Readable dollars at any magnitude: never scientific notation."""
    if v == 0:
        return "$0"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.3f}".rstrip("0").rstrip(".")
    digits = max(2, -math.floor(math.log10(v)) + 1)
    return f"${v:.{digits}f}".rstrip("0").rstrip(".")


def short(name: str) -> str:
    return name if len(name) <= LABEL_MAX else name[: LABEL_MAX - 1] + "…"


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """Round tick values covering [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    if ticks[-1] < hi:  # always cover the maximum
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def log_ticks(lo: float, hi: float) -> list[float]:
    out = []
    for e in range(math.floor(math.log10(lo)), math.ceil(math.log10(hi)) + 1):
        for m in (1, 2, 5):
            v = m * 10**e
            if lo <= v <= hi:
                out.append(v)
    return out or [lo, hi]


def use_log_scale(xs: Sequence[float]) -> bool:
    """Log only when every cost is positive and they span more than 10x. A free (local)
    model has cost 0, which a log axis cannot place honestly, so it keeps the axis linear."""
    return bool(xs) and min(xs) > 0 and max(xs) / min(xs) > 10


def _shape(kind: str, cx: float, cy: float, r: float = 6) -> str:
    if kind == "circle":
        return f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{r}"/>'
    if kind == "square":
        return f'<rect x="{_fmt(cx - r)}" y="{_fmt(cy - r)}" width="{2 * r}" height="{2 * r}"/>'
    if kind == "diamond":
        pts = [(cx, cy - r - 1), (cx + r + 1, cy), (cx, cy + r + 1), (cx - r - 1, cy)]
    else:
        pts = [(cx, cy - r - 1), (cx + r + 1, cy + r), (cx - r - 1, cy + r)]
    return '<polygon points="' + " ".join(f"{_fmt(a)},{_fmt(b)}" for a, b in pts) + '"/>'


def scatter(
    points: Sequence[Point],
    *,
    title: str,
    desc: str,
    x_label: str,
    y_label: str,
    threshold: float | None = None,
    chart_id: str = "scatter",
) -> str:
    """Quality (y, 0..1 with CI whiskers) against cost (x). One shape per series (judge) with
    a legend; models that share a cost column are spread side by side, and every model is
    labelled next to its own marks, so colour is never the only cue."""
    if not points:
        return ""
    xs = [p.x for p in points]
    log_x = use_log_scale(xs)
    if log_x:
        x_lo, x_hi = min(xs) / 1.5, max(xs) * 1.5
        ticks_x = log_ticks(x_lo, x_hi)

        def sx(v: float) -> float:
            span = math.log10(x_hi) - math.log10(x_lo)
            return PAD_L + (math.log10(v) - math.log10(x_lo)) / span * (W - PAD_L - PAD_R)

    else:
        top = max(xs)
        ticks_x = nice_ticks(0.0, top * 1.3 if top > 0 else 0.01)
        x_lo, x_hi = ticks_x[0], ticks_x[-1]

        def sx(v: float) -> float:
            return PAD_L + (v - x_lo) / ((x_hi - x_lo) or 1) * (W - PAD_L - PAD_R)

    def sy(v: float) -> float:
        return PAD_T + (1 - min(1.0, max(0.0, v))) * (H - PAD_T - PAD_B)

    series = sorted({p.series for p in points})
    shape_of = {s: SHAPES[i % len(SHAPES)] for i, s in enumerate(series)}
    n_series = len(series)
    models = sorted({p.label for p in points})
    base = {m: sx(next(p.x for p in points if p.label == m)) for m in models}

    # Models within 20px of each other share a column: give each a slot side by side.
    width = n_series * SERIES_GAP + MODEL_GAP
    slot: dict[str, float] = {}
    groups: list[list[str]] = []
    for m in sorted(models, key=lambda k: (base[k], k)):
        if groups and base[m] - base[groups[-1][0]] < 20:
            groups[-1].append(m)
        else:
            groups.append([m])
    half_series = (n_series - 1) / 2 * SERIES_GAP + 8  # room for the outermost mark
    for g in groups:
        for i, m in enumerate(g):
            slot[m] = (i - (len(g) - 1) / 2) * width
        lo = min(base[m] + slot[m] for m in g) - half_series
        hi = max(base[m] + slot[m] for m in g) + half_series
        shift = max(0.0, PAD_L - lo) - max(0.0, hi - (W - PAD_R))
        for m in g:  # keep every mark of the group inside the plot area
            slot[m] += shift

    def cx_of(p: Point) -> float:
        offset = (series.index(p.series) - (n_series - 1) / 2) * SERIES_GAP
        return base[p.label] + slot[p.label] + offset

    out = [
        f'<svg id="{chart_id}" class="chart" viewBox="0 0 {W} {H}" role="img" '
        f'aria-labelledby="{chart_id}-t {chart_id}-d" xmlns="http://www.w3.org/2000/svg">',
        f'<title id="{chart_id}-t">{escape(title)}</title>',
        f'<desc id="{chart_id}-d">{escape(desc)}</desc>',
    ]
    for v in nice_ticks(0, 1):  # y grid
        y = sy(v)
        out.append(
            f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{_fmt(y)}" y2="{_fmt(y)}"/>'
            f'<text class="tick" x="{PAD_L - 8}" y="{_fmt(y + 4)}" text-anchor="end">'
            f"{v:.0%}</text>"
        )
    for v in ticks_x:
        x = sx(v)
        out.append(
            f'<line class="grid" x1="{_fmt(x)}" x2="{_fmt(x)}" y1="{PAD_T}" y2="{H - PAD_B}"/>'
            f'<text class="tick" x="{_fmt(x)}" y="{H - PAD_B + 18}" text-anchor="middle">'
            f"{escape(money(v))}</text>"
        )
    out.append(
        f'<text class="axis" x="{(PAD_L + W - PAD_R) / 2}" y="{H - 12}" text-anchor="middle">'
        f"{escape(x_label)}{' (log scale)' if log_x else ''}</text>"
        f'<text class="axis" transform="translate(18 {(PAD_T + H - PAD_B) / 2}) rotate(-90)" '
        f'text-anchor="middle">{escape(y_label)}</text>'
    )
    legend_bottom = PAD_T + 10 + 22 * len(series) + 8  # last legend line ("shape = judge")
    if threshold is not None and 0 <= threshold <= 1:
        y = sy(threshold)
        # The label lives in the right margin (never on top of marks), below the legend.
        label_y = max(y + 4, legend_bottom + 22)
        out.append(
            f'<line class="threshold" x1="{PAD_L}" x2="{W - PAD_R}" y1="{_fmt(y)}" '
            f'y2="{_fmt(y)}"/><text class="tick" x="{W - PAD_R + 8}" y="{_fmt(label_y)}">'
            f"– – bar {threshold:.0%}</text>"
        )
    for p in points:
        cx, cy = cx_of(p), sy(p.y)
        cls = f"s{series.index(p.series) % len(SHAPES)}"
        shown = bounded_rate(p.y, p.lo, p.hi, p.n) or f"{p.y:.1%} (95% CI {p.lo:.1%} to {p.hi:.1%})"
        tip = f"{p.label} by {p.series}: {shown}, {money(p.x)} per 100 cases"
        out.append(
            f'<g class="pt {cls}" data-model="{escape(p.label)}" data-judge="{escape(p.series)}" '
            f'data-x="{p.x!r}" data-y="{p.y!r}" data-lo="{p.lo!r}" data-hi="{p.hi!r}" '
            f'data-cx="{_fmt(cx)}">'
            f"<title>{escape(tip)}</title>"
            f'<line class="whisker" x1="{_fmt(cx)}" x2="{_fmt(cx)}" y1="{_fmt(sy(p.lo))}" '
            f'y2="{_fmt(sy(p.hi))}"/>'
            f'<line class="whisker" x1="{_fmt(cx - 4)}" x2="{_fmt(cx + 4)}" '
            f'y1="{_fmt(sy(p.lo))}" y2="{_fmt(sy(p.lo))}"/>'
            f'<line class="whisker" x1="{_fmt(cx - 4)}" x2="{_fmt(cx + 4)}" '
            f'y1="{_fmt(sy(p.hi))}" y2="{_fmt(sy(p.hi))}"/>'
            f"{_shape(shape_of[p.series], cx, cy)}</g>"
        )
    # One label per model right of its own marks, nudged apart only where labels would
    # collide; a short leader line keeps a nudged label tied to its marks.
    wanted = []
    for m in models:
        mine = [p for p in points if p.label == m]
        x = max(cx_of(p) for p in mine) + 10
        y = sum(sy(p.y) for p in mine) / len(mine) + 4
        wanted.append((y, x, m))
    placed: list[tuple[float, float, float, str]] = []  # (y, x, wanted y, model)
    for y, x, m in sorted(wanted):
        y0 = y
        for py, px, _, _ in placed:
            if abs(px - x) < 7 * LABEL_MAX and y - py < 14:
                y = py + 14
        placed.append((y, x, y0, m))
    for y, x, y0, m in placed:
        if abs(y - y0) > 3:
            out.append(
                f'<line class="leader" x1="{_fmt(x - 8)}" y1="{_fmt(y0 - 4)}" '
                f'x2="{_fmt(x - 2)}" y2="{_fmt(y - 4)}"/>'
            )
        out.append(
            f'<text class="ptlabel" x="{_fmt(x)}" y="{_fmt(y)}"><title>{escape(m)}</title>'
            f"{escape(short(m))}</text>"
        )
    ly = PAD_T + 10
    for i, s in enumerate(series):
        out.append(
            f'<g class="legend s{i % len(SHAPES)}">{_shape(shape_of[s], W - PAD_R + 20, ly)}'
            f'<text class="tick" x="{W - PAD_R + 32}" y="{ly + 4}"><title>{escape(s)}</title>'
            f"{escape(short(s))}</text></g>"
        )
        ly += 22
    out.append(f'<text class="tick" x="{W - PAD_R + 12}" y="{ly + 8}">shape = judge</text></svg>')
    return "".join(out)
