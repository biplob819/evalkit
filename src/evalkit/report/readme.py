"""README headline (B2.3, REL-01/02): one chart and three sentences generated from a run's
`summary.json`, never typed by hand. The caveats live inside the chart image itself, so the
picture cannot travel without them.
"""

from __future__ import annotations

from html import escape

from evalkit.pipeline.aggregate import stamp_lines
from evalkit.report.svg import bounded_rate, short
from evalkit.schemas import Interval, RunSummary

START = "<!-- evalkit:headline:start -->"
END = "<!-- evalkit:headline:end -->"

W = 760
ROW = 34
TOP = 64
LEFT = 200
RIGHT = 40
SHAPES = ("circle", "square")
CSS = """
.bg{fill:#ffffff}.fg{fill:#1d1d1b}.muted{fill:#5f5e58}.grid{stroke:#e3e0d8}
.ci{stroke:#1d1d1b;stroke-width:1.5}.j0{fill:#2f5d50}.j1{fill:#8a4b0f}.warn{fill:#7a4f00}
@media (prefers-color-scheme:dark){.bg{fill:#0d1117}.fg{fill:#e6edf3}.muted{fill:#9da7b1}
.grid{stroke:#30363d}.ci{stroke:#e6edf3}.j0{fill:#8cc4b0}.j1{fill:#f0a868}.warn{fill:#f5dc9a}}
text{font:13px -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.title{font-size:16px;font-weight:600}
"""


def _headline(iv: Interval, n: int) -> str:
    """GAT-07: exactly 0 or 1 reads "0 of n, below X% at 95%", never "0.0% (0.0%–0.0%)"."""
    return bounded_rate(iv.point, iv.lo, iv.hi, n) or (
        f"{iv.point:.1%} (95% CI {iv.lo:.1%}–{iv.hi:.1%})"
    )


def _x(v: float) -> float:
    return LEFT + max(0.0, min(1.0, v)) * (W - LEFT - RIGHT)


def _mark(kind: str, cx: float, cy: float, cls: str) -> str:
    if kind == "circle":
        return f'<circle class="{cls}" cx="{cx:.2f}" cy="{cy:.2f}" r="6"/>'
    return f'<rect class="{cls}" x="{cx - 6:.2f}" y="{cy - 6:.2f}" width="12" height="12"/>'


def readme_chart(summary: RunSummary) -> str:
    """Headline per model (rows) and judge (shape), with 95% CIs and the run's caveats."""
    judges = sorted({j for m in summary.models for j in m.headline})
    models = sorted(
        summary.models,
        key=lambda m: -min((iv.point for iv in m.headline.values()), default=m.accuracy.point),
    )
    stamps = stamp_lines(summary)
    h = TOP + ROW * len(models) + 54 + 18 * (len(stamps) + 1)
    title = f"{summary.task}: correct and fully grounded, by model and judge (95% CI)"
    desc = (
        "; ".join(
            f"{m.model}: "
            + ", ".join(
                f"{j} {iv.point:.0%} ({iv.lo:.0%}-{iv.hi:.0%})"
                for j, iv in sorted(m.headline.items())
            )
            for m in models
        )
        or "judging pending"
    )
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" height="{h}" '
        'role="img" aria-labelledby="t d">',
        f'<title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>',
        f"<style>{CSS}</style>",
        f'<rect class="bg" width="{W}" height="{h}" rx="8"/>',
        f'<text class="fg title" x="20" y="30">{escape(title)}</text>',
    ]
    axis_y = TOP + ROW * len(models)
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        x = _x(v)
        out.append(
            f'<line class="grid" x1="{x:.2f}" x2="{x:.2f}" y1="{TOP - 14}" y2="{axis_y}"/>'
            f'<text class="muted" x="{x:.2f}" y="{axis_y + 18}" text-anchor="middle">{v:.0%}</text>'
        )
    for i, m in enumerate(models):
        y = TOP + ROW * i + ROW / 2 - 6
        out.append(
            f'<text class="fg" x="{LEFT - 14}" y="{y + 4:.2f}" text-anchor="end">'
            f"{escape(short(m.model))}</text>"
        )
        for k, j in enumerate(judges):
            iv = m.headline.get(j)
            if iv is None:
                continue
            yy = y + (k - (len(judges) - 1) / 2) * 12
            shown = bounded_rate(iv.point, iv.lo, iv.hi, m.n)
            tip = f"{m.model} by {j}: {shown or f'{iv.point:.1%} ({iv.lo:.1%} to {iv.hi:.1%})'}"
            mark = _mark(SHAPES[k % 2], _x(iv.point), yy, f"j{k % 2}")
            out.append(
                f'<g><title>{escape(tip)}</title><line class="ci" x1="{_x(iv.lo):.2f}" '
                f'x2="{_x(iv.hi):.2f}" y1="{yy:.2f}" y2="{yy:.2f}"/>{mark}</g>'
            )
    ly = axis_y + 40
    lx = 20.0
    for k, j in enumerate(judges):
        out.append(f"{_mark(SHAPES[k % 2], lx + 6, ly - 4, f'j{k % 2}')}")
        out.append(f'<text class="fg" x="{lx + 18}" y="{ly}">judge: {escape(j)}</text>')
        lx += 30 + 8 * len(f"judge: {j}")
    for n, line in enumerate(stamps):
        out.append(f'<text class="warn" x="20" y="{ly + 22 + 18 * n}">⚠ {escape(line)}</text>')
    if not stamps:
        out.append(
            f'<text class="muted" x="20" y="{ly + 22}">Both judges calibrated on held-out labels '
            "for this task and rubric.</text>"
        )
    out.append("</svg>")
    return "".join(out) + "\n"


def readme_sentences(summary: RunSummary, n_cases: int) -> list[str]:
    """Three sentences: what was measured, the result with intervals, the caveats."""
    judges = sorted({j for m in summary.models for j in m.headline})
    models = [m.model for m in summary.models]
    s1 = (
        f"On {n_cases} PubMedQA questions, {len(models)} models ({', '.join(models)}) answered "
        "from the abstract and cited its sentences; "
        + (
            f"two judges from different families ({' and '.join(judges)}) checked every claim."
            if summary.judge_mode == "dual"
            else f"one judge ({', '.join(judges)}) checked every claim."
        )
    )
    if summary.judging_status != "complete" or not judges:
        s2 = "Judging is still pending, so no headline figure is reported yet."
    else:
        best = max(summary.models, key=lambda m: min(iv.point for iv in m.headline.values()))
        ivs = ", ".join(
            f"{_headline(iv, best.n)} by {j}" for j, iv in sorted(best.headline.items())
        )
        ties = [
            c
            for c in summary.comparisons
            if c.metric == "headline" and best.model in (c.model_a, c.model_b) and not c.separated
        ]
        rivals = sorted({c.model_b if c.model_a == best.model else c.model_a for c in ties})
        tie_text = (
            f"; it is statistically tied with {', '.join(rivals)} for at least one judge"
            if rivals
            else "; it is separated from every other model after a Holm correction"
        )
        s2 = (
            f"The most grounded model was {best.model}, correct and fully grounded on "
            f"{ivs}{tie_text}"
        )
        others = [j for j in best.headline if j != best.model]
        if best.model in best.headline:  # it also judged its own answers: say so
            s2 += f" ({best.model} also judged its own answers" + (
                f"; {', '.join(others)} agrees)" if others else ")"
            )
        most_accurate = max(summary.models, key=lambda m: m.accuracy.point)
        if most_accurate.model != best.model:
            s2 += (
                f"; the most accurate model was {most_accurate.model} "
                f"({most_accurate.accuracy.point:.1%} correct labels); the headline also "
                "requires every claim to be supported by the sentences the answer cites"
            )
        s2 += "."
    stamps = stamp_lines(summary)
    if stamps:
        s3 = "Caveats: " + "; ".join(stamps) + ". Treat these numbers as provisional."
    else:
        kappas = ", ".join(f"{a.judge} κ={a.kappa.point:.2f}" for a in summary.judges)
        s3 = f"Both judges were calibrated against held-out hand labels ({kappas})."
    return [s1, s2, s3]


# Absolute links: the README is also the PyPI project page, where relative paths break.
REPO = "https://github.com/biplob819/evalkit"
REPO_RAW = "https://raw.githubusercontent.com/biplob819/evalkit/main"


def readme_block(summary: RunSummary, n_cases: int, report_url: str) -> str:
    sentences = " ".join(readme_sentences(summary, n_cases))
    return (
        f"{START}\n"
        f'<p align="center"><img src="{REPO_RAW}/docs/assets/headline.svg" '
        f'alt="{escape(sentences)}" width="760"></p>\n\n'
        f"{sentences}\n\n"
        f"[Full report]({report_url}) · results in [`docs/results/`]({REPO}/tree/main/docs/results)"
        "\n\n"
        "```bash\n"
        "pip install 'evalkit-harness[anthropic,openai]'\n"
        "evalkit init   # scaffold a project and a three-case sample task\n"
        "evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run   # estimate\n"
        "evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3   # needs keys in .env\n"
        "```\n\n"
        "Questions from [PubMedQA](https://pubmedqa.github.io/) (Jin et al., 2019, MIT licence); "
        f"see [DATA_CARD.md]({REPO}/blob/main/DATA_CARD.md). Research benchmark, not medical "
        "advice.\n"
        f"{END}"
    )


def splice(readme: str, block: str) -> str:
    """Replace the generated block, or insert it after the first heading."""
    if START in readme and END in readme:
        head, rest = readme.split(START, 1)
        _, tail = rest.split(END, 1)
        return head + block + tail
    first, _, remainder = readme.partition("\n")
    return f"{first}\n\n{block}\n{remainder}"
