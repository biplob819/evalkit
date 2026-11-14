import asyncio
import itertools
import json
import re
from html import escape

import pytest

from evalkit.pipeline.aggregate import stamp_lines
from evalkit.report import render_report, vega_lite_specs, write_report
from evalkit.report.html import BANNER, failures
from evalkit.report.svg import PAD_B, PAD_T, H
from evalkit.schemas import Answer
from tests.golden.test_golden_run import _run, reply
from tests.pipeline.fakes import ScriptedBatchAdapter


@pytest.fixture(scope="module")
def run_inputs(tmp_path_factory):
    store = tmp_path_factory.mktemp("store")
    summary, records, _judging, cases = asyncio.run(_run(store, ScriptedBatchAdapter(reply)))
    return summary, records, cases


def page(run_inputs, **kw):
    summary, records, cases = run_inputs
    from evalkit.tasks import builtin_task

    return render_report(
        summary, records, cases, thresholds=builtin_task("pubmedqa").thresholds, **kw
    )


# ---- DSH-01 self-contained and deterministic ---------------------------------------------------


@pytest.mark.safety
def test_report_loads_nothing_from_the_network(run_inputs):
    html = page(run_inputs)
    assert not re.search(r"""src\s*=\s*["']?(https?:)?//""", html, re.I)  # no remote resources
    links = re.findall(r"""href\s*=\s*["']([^"']+)""", html)
    assert all(h.startswith("https://github.com/biplob819/evalkit/") for h in links)  # nav only
    assert "<link" not in html.lower() and "@import" not in html
    assert len(re.findall(r"<script", html)) == 1  # only our inline script


def test_report_and_specs_are_deterministic(run_inputs, tmp_path):
    a = write_report(tmp_path / "a", *run_inputs)
    b = write_report(tmp_path / "b", *run_inputs)
    assert a.read_bytes() == b.read_bytes()
    spec = json.loads((tmp_path / "a" / "charts.vl.json").read_text())
    assert spec["headline"]["data"]["values"]
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == ["charts.vl.json", "report.html"]


# ---- DSH-02 scatter ---------------------------------------------------------------------------


def _sy(v):
    return PAD_T + (1 - v) * (H - PAD_T - PAD_B)


def test_scatter_marks_and_whiskers_match_the_summary(run_inputs):
    summary = run_inputs[0]
    html = page(run_inputs)
    marks = re.findall(
        r'<g class="pt s\d" data-model="([^"]+)" data-judge="([^"]+)" data-x="([^"]+)" '
        r'data-y="([^"]+)" data-lo="([^"]+)" data-hi="([^"]+)" data-cx="[^"]+">.*?'
        r'<line class="whisker" '
        r'x1="[^"]+" x2="[^"]+" y1="([^"]+)" y2="([^"]+)"/>',
        html,
    )
    expected = {
        (m.model, j): (m.cost_per_100_usd, iv)
        for m in summary.models
        for j, iv in m.headline.items()
    }
    assert len(marks) == len(expected) == 6
    for model, judge, x, y, lo, _hi, y1, y2 in marks:
        cost, iv = expected[model, judge]
        assert float(x) == cost and float(y) == iv.point and float(lo) == iv.lo
        assert float(y1) == pytest.approx(_sy(iv.lo), abs=0.01)
        assert float(y2) == pytest.approx(_sy(iv.hi), abs=0.01)
    assert 'class="threshold"' in html and "bar 60%" in html
    assert html.count("<tr><td>") >= 6  # data table rows for the points


# ---- DSH-03 model and pairwise tables ---------------------------------------------------------


def test_model_table_badges_and_failed_thresholds(run_inputs):
    summary = run_inputs[0]
    html = page(run_inputs)
    table = html.split("<table id=models>")[1].split("</table>")[0]
    for m in summary.models:
        assert m.model in table
        for f in m.failed_thresholds:
            assert escape(f) in table
    assert table.count("✗ FAIL") == sum(not m.passes_threshold for m in summary.models)
    pairs = html.split("<table id=pairs>")[1].split("</table>")[0]
    assert "unadjusted 95%" in pairs
    assert pairs.count("<tr><td>") == len(summary.comparisons)
    assert pairs.count(">separated<") == sum(c.separated for c in summary.comparisons)


# ---- DSH-04 failure explorer ------------------------------------------------------------------


def test_failure_explorer_lists_exactly_the_failures(run_inputs):
    summary, records, _ = run_inputs
    html = page(run_inputs)
    judges = sorted({j for m in summary.models for j in m.headline})
    assert html.count("<details ") == len(failures(records, judges))
    assert 'id="f-model"' in html and 'id="f-judge"' in html


@pytest.mark.safety
def test_model_text_is_escaped(run_inputs):
    summary, records, cases = run_inputs
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    r0 = next(
        r
        for r in records
        if r.completion.answer is not None and not all(r.scores.correct_and_grounded.values())
    )  # type: ignore[union-attr]
    bad_comp = r0.completion.model_copy(
        update={"answer": Answer(label="yes", answer=evil, citations=(1,))}
    )
    bad = [r0.model_copy(update={"completion": bad_comp}) if r is r0 else r for r in records]
    html = render_report(summary, bad, cases)
    assert evil not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert len(re.findall(r"<script", html)) == 1


# ---- DSH-05 calibration and self-preference ----------------------------------------------------


def test_uncalibrated_run_shows_the_stamp_and_guide(run_inputs):
    html = page(run_inputs)
    assert "Judges not calibrated for this task" in html
    assert "docs/calibrate-your-task.md" in html
    assert "<table id=selfpref>" in html  # dual mode


def test_calibrated_run_shows_kappa_and_confusion(run_inputs):
    from evalkit.pipeline.judge import JUDGE_PROMPT_HASH
    from evalkit.schemas import Interval, JudgeAgreement

    summary, records, cases = run_inputs
    iv = Interval(point=0.71, lo=0.6, hi=0.8)
    agreements = tuple(
        JudgeAgreement(
            judge=j,
            task=summary.task,
            judge_prompt_hash=JUDGE_PROMPT_HASH,
            n_claims=150,
            coverage=1.0,
            kappa=iv,
            agreement=0.88,
            confusion=((80, 6), (8, 56)),
            by_family={"anthropic": iv},
        )
        for j in sorted({j for m in summary.models for j in m.headline})
    )
    cal = summary.model_copy(update={"calibration_status": "calibrated", "judges": agreements})
    html = render_report(cal, records, cases)
    table = html.split("<table id=calibration>")[1].split("</table>")[0]
    assert "TP 80 · FN 6 · FP 8 · TN 56" in table and "0.71" in table and "71.0%" not in table
    assert "Judges not calibrated" not in html


def test_single_judge_run_has_no_self_preference_table(run_inputs):
    summary, records, cases = run_inputs
    one = summary.model_copy(update={"judge_mode": "single", "self_preference": {}})
    html = render_report(one, records, cases)
    assert "<table id=selfpref>" not in html and "self-preference not measured" in html


# ---- DSH-06 caveats, pending, accessibility ---------------------------------------------------


def test_banner_and_every_stamp_sit_above_the_headline(run_inputs):
    summary = run_inputs[0]
    html = page(run_inputs)
    top = html.split("<h2 id=headline>")[0]
    assert escape(BANNER) in top
    for line in stamp_lines(summary):
        assert escape(line) in top


def test_pending_run_shows_no_headline(run_inputs):
    summary, records, cases = run_inputs
    pending = summary.model_copy(
        update={
            "judging_status": "pending",
            "finished_at": None,
            "self_preference": {},
            "comparisons": tuple(c for c in summary.comparisons if c.metric == "accuracy"),
            "models": tuple(
                m.model_copy(update={"headline": {}, "groundedness_mean": {}})
                for m in summary.models
            ),
        }
    )
    html = render_report(pending, records, cases)
    assert "Judging pending." in html and "<svg" not in html
    assert "judging pending" in html.split("<h2 id=headline>")[0]


def test_charts_and_controls_are_accessible(run_inputs):
    html = page(run_inputs)
    svg = html.split("<svg")[1].split("</svg>")[0]
    assert 'role="img"' in svg and "<title id=" in svg and "<desc id=" in svg
    assert "✓ PASS" in html or "✗ FAIL" in html  # text, not colour alone
    assert '<label for="f-model">' in html and "<summary>" in html
    assert ":focus-visible" in html and "prefers-color-scheme:dark" in html
    assert vega_lite_specs(run_inputs[0])["headline"]["layer"]


# ---- B2.1 verifier regressions (L-28) ---------------------------------------------------------


def _variant(summary, costs=None, latency=None):
    models = []
    for i, m in enumerate(summary.models):
        upd = {}
        if costs is not None:
            upd["cost_per_100_usd"] = costs[i]
        if latency is not None:
            upd["latency_p50_ms"], upd["latency_p95_ms"] = latency
        models.append(m.model_copy(update=upd))
    return summary.model_copy(update={"models": tuple(models)})


def _marks(html):
    return {
        (m, j): float(cx)
        for m, j, cx in re.findall(
            r'<g class="pt s\d" data-model="([^"]+)" data-judge="([^"]+)" [^>]*data-cx="([^"]+)"',
            html,
        )
    }


def test_log_scale_when_costs_span_widely_and_marks_follow_cost(run_inputs):
    from evalkit.report.svg import PAD_L

    summary, records, cases = run_inputs
    wide = _variant(summary, costs=[2.4, 0.11, 0.05])
    html = render_report(wide, records, cases)
    assert "(log scale)" in html
    xs = _marks(html)
    order = sorted({m for m, _ in xs}, key=lambda m: min(v for (mm, _), v in xs.items() if mm == m))
    assert order == ["gpt-6-luna", "gpt-6.1-sol", "claude-sonnet-5-5"]  # cheapest left
    assert all(v >= PAD_L for v in xs.values())


def test_a_free_model_keeps_the_axis_linear_and_on_canvas(run_inputs):
    from evalkit.report.svg import PAD_L

    summary, records, cases = run_inputs
    html = render_report(_variant(summary, costs=[0.0, 0.2, 5.0]), records, cases)
    assert "(log scale)" not in html
    xs = _marks(html)
    free = [v for (m, _), v in xs.items() if m == "claude-sonnet-5-5"]
    assert free and all(v >= PAD_L for v in free)  # never left of the y-axis
    assert ">$0<" in html  # the zero tick is labelled


def test_equal_cost_models_get_their_own_columns(run_inputs):
    html = page(run_inputs)  # every golden model costs the same
    xs = _marks(html)
    per_model = {}
    for (m, _), v in xs.items():
        per_model.setdefault(m, []).append(v)
    spans = sorted((min(v), max(v)) for v in per_model.values())
    assert all(a[1] < b[0] for a, b in itertools.pairwise(spans))  # no shared x positions


def test_every_model_summary_figure_is_rendered(run_inputs):
    summary, records, cases = run_inputs
    html = render_report(_variant(summary, latency=(812, 2950)), records, cases)
    table = html.split("<table id=models>")[1].split("</table>")[0]
    for m in summary.models:
        for j, iv in m.groundedness_mean.items():
            assert f"Groundedness · {j}" in table and f"{iv.point:.1%}" in table
        assert f"{m.citation_validity.point:.1%}" in table and f"{m.coverage.point:.1%}" in table
    assert "812 ms / 2,950 ms" in table


def test_money_and_p_value_formats():
    from evalkit.report.html import _p
    from evalkit.report.svg import money

    assert money(0) == "$0" and money(0.00004) == "$0.00004" and money(12345.678) == "$12,345.68"
    assert money(0.25) == "$0.25" and "e" not in money(1e-7)
    assert _p(0.0001) == "&lt;0.001" and _p(0.0412) == "0.041"


def test_pending_threshold_shows_pending_not_fail(run_inputs):
    summary, records, cases = run_inputs
    models = tuple(
        m.model_copy(
            update={
                "passes_threshold": False,
                "failed_thresholds": ("headline >= 0.6 (judging pending)",),
                "headline": {},
                "groundedness_mean": {},
            }
        )
        for m in summary.models
    )
    pending = summary.model_copy(
        update={
            "models": models,
            "judging_status": "pending",
            "finished_at": None,
            "self_preference": {},
            "comparisons": tuple(c for c in summary.comparisons if c.metric == "accuracy"),
        }
    )
    html = render_report(pending, records, cases)
    assert "… PENDING" in html and "✗ FAIL" not in html


def test_calibration_report_details_are_shown(run_inputs):
    from datetime import UTC, datetime

    from evalkit.pipeline.judge import JUDGE_PROMPT_HASH
    from evalkit.schemas import CalibrationReport, Interval, JudgeAgreement

    summary, records, cases = run_inputs
    iv = Interval(point=0.64, lo=0.5, hi=0.77)
    judges = sorted({j for m in summary.models for j in m.headline})
    agreements = tuple(
        JudgeAgreement(
            judge=j,
            task=summary.task,
            judge_prompt_hash=JUDGE_PROMPT_HASH,
            n_claims=140,
            coverage=0.98,
            kappa=iv,
            agreement=0.86,
            confusion=((70, 9), (10, 51)),
            by_family={
                "anthropic": Interval(point=0.58, lo=0.4, hi=0.74),
                "openai": Interval(point=0.69, lo=0.52, hi=0.83),
            },
        )
        for j in judges
    )
    report = CalibrationReport(
        schema_version="1.0",
        task=summary.task,
        judge_prompt_hash=JUDGE_PROMPT_HASH,
        scored_at=datetime(2026, 11, 14, tzinfo=UTC),
        n_answers=50,
        n_claims=150,
        heldout_digest="d" * 64,
        agreements=agreements,
        extraction_error_rate=0.04,
        self_consistency=Interval(point=0.91, lo=0.8, hi=0.98),
    )
    cal = summary.model_copy(update={"calibration_status": "calibrated", "judges": agreements})
    html = render_report(cal, records, cases, calibration=report)
    panel = html.split("<table id=calibration>")[1]
    assert "self-consistency kappa 0.91" in panel and "extraction error rate 4.0%" in panel.lower()
    assert "anthropic 0.58" in panel and "openai 0.69" in panel and "(98% covered)" in panel


@pytest.mark.safety
def test_every_string_field_is_escaped(run_inputs):
    summary, records, cases = run_inputs
    evil = '"><script>alert(1)</script>'
    models = tuple(
        m.model_copy(update={"failed_thresholds": (evil,), "model": m.model + evil})
        for m in summary.models
    )
    nasty = summary.model_copy(update={"models": models, "task": evil, "run_id": evil})
    bad_records = []
    for r in records:
        claims = tuple(c.model_copy(update={"text": evil}) for c in r.claims)
        verdicts = tuple(v.model_copy(update={"quote": evil}) for v in r.verdicts)
        bad_records.append(r.model_copy(update={"claims": claims, "verdicts": verdicts}))
    bad_cases = {
        k: c.model_copy(update={"question": evil, "context": tuple(evil for _ in c.context)})
        for k, c in cases.items()
    }
    html = render_report(nasty, bad_records, bad_cases)
    from html.parser import HTMLParser

    class Audit(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = 0
            self.handlers: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.scripts += 1
            self.handlers += [k for k, _ in attrs if k.startswith("on")]

    audit = Audit()
    audit.feed(html)
    assert audit.scripts == 1 and audit.handlers == []
    assert "<script>alert(1)" not in html


def test_explorer_count_is_independent_of_the_helper(run_inputs):
    summary, records, _ = run_inputs
    judges = {j for m in summary.models for j in m.headline}
    expected = sum(
        1
        for r in records
        if r.scores is not None and any(not r.scores.correct_and_grounded[j] for j in judges)
    )
    assert page(run_inputs).count("<details ") == expected


def test_invalid_citations_and_missing_cases_are_explained(run_inputs):
    summary, records, cases = run_inputs
    target = next(
        r for r in records if r.claims and not all(r.scores.correct_and_grounded.values())
    )  # type: ignore[union-attr]
    bad = target.model_copy(
        update={"claims": (target.claims[0].model_copy(update={"cited": (99,)}),)}
    )
    recs = [bad if r is target else r for r in records]
    html = render_report(summary, recs, cases)
    assert "not in the source" in html and "an invalid citation" in html
    missing = {k: v for k, v in cases.items() if k != target.case_id}
    assert "Case not found" in render_report(summary, recs, missing)
