"""B5.7 efficiency run-to-run variation (EFF-03) and the trend chart (LED-05), across the five
proof dimensions. EvalKit run folders carry summary.json's per-model `efficiency` block (EFF-02,
D-70); the history keeps `price_date` beside the figures so a price-table change is a hint, not
a new series. No network, no keys. Regenerate goldens with EVALKIT_UPDATE_GOLDEN=1.

EFF-03 safety is n/a: the efficiency block holds per-model means, intervals and counts only
(no case data), so there is no held-out content to protect beyond LED-01's folding.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
from pathlib import Path

import pytest

from evalkit import cli
from evalkit import ledger as L
from tests.ledger_helpers import (
    EXAMPLE_RUN,
    FRAUD_HISTORY,
    LEDGER_FIX,
    at,
    efficiency_run,
    fraud_run,
    no_network,  # noqa: F401
    rate,
    write_run,
)

UPDATE = os.environ.get("EVALKIT_UPDATE_GOLDEN") == "1"
COSTS = [0.00100, 0.00104, 0.00098, 0.00101, 0.00103, 0.00099]
CACHE = [0.61, 0.60, 0.62, 0.59, 0.60, 0.61]
P95 = [900.0, 910.0, 905.0, 895.0, 902.0, 908.0]


@pytest.fixture(autouse=True)
def _offline(no_network):  # noqa: F811
    yield


def _eff_ledger(tmp: Path) -> Path:
    ledger = tmp / "runs.jsonl"
    for i, (c, h, p) in enumerate(zip(COSTS, CACHE, P95, strict=True)):
        L.record_run(ledger, efficiency_run(tmp, i, cost=c, cache=h, p95=p), at=at(i + 1))
    return ledger


def _by(bands: list[L.Band]) -> dict[str, L.Band]:
    return {b.metric: b for b in bands}


# ==================================== EFF-03 ====================================


def test_a_price_change_is_marked_price_date_changed(tmp_path):
    """logical: a cost jump after a price-table change is flagged and hinted `price_date
    changed`; the price date is kept beside the figures, so the row stays comparable."""
    ledger = _eff_ledger(tmp_path)
    new = efficiency_run(tmp_path, 9, cost=0.0021, cache=0.60, p95=903.0, price_date="2026-11-20")
    res = L.record_run(ledger, new, at=at(9))
    b = _by(res.bands)["cost_per_case"]
    assert b.status == "alarm" and b.hints == ("price_date changed",)
    assert b.n_history == 6 and "price_date" not in b.labels
    assert res.entry and res.entry["price_dates"] == {"claude-sonnet-5-5": "2026-11-20"}
    same_prices = efficiency_run(tmp_path, 10, cost=0.0021, cache=0.60, p95=903.0)
    b = _by(L.check_run(ledger, same_prices).bands)["cost_per_case"]
    assert b.flagged and "price_date changed" in b.hints  # previous run had 2026-11-20
    other = tmp_path / "other.jsonl"
    for i in range(6):
        L.record_run(other, efficiency_run(tmp_path / "o", i, cost=COSTS[i], cache=0.6, p95=900.0))
    b = _by(
        L.check_run(
            other, efficiency_run(tmp_path / "o", 7, cost=0.0021, cache=0.6, p95=900.0)
        ).bands
    )["cost_per_case"]
    assert b.status == "alarm" and b.hints == ()  # same price table: a real cost regression


def test_a_cache_hit_drop_is_flagged_as_a_cache_regression(tmp_path):
    """logical: a cache-hit share drop is flagged with the `cache regression` hint; latency rows
    name their percentile; hints never clear a flag."""
    ledger = _eff_ledger(tmp_path)
    res = L.check_run(ledger, efficiency_run(tmp_path, 9, cost=0.001, cache=0.05, p95=1400.0))
    by = _by(res.bands)
    assert by["cache_hit_share"].status == "alarm"
    assert by["cache_hit_share"].hints == ("cache regression",)
    assert by["latency_p95"].flagged and by["latency_p95"].hints == ("p95 latency",)
    assert res.exit_code == 1
    up = _by(
        L.check_run(ledger, efficiency_run(tmp_path, 10, cost=0.001, cache=0.95, p95=903.0)).bands
    )["cache_hit_share"]
    assert up.flagged and up.hints == ()  # moved, but up: not a cache regression


def test_the_spread_table_matches_a_direct_computation(tmp_path):
    """logical: runs, median, 1.4826 x MAD, CV, min, max and latest per (model, figure)."""
    ledger = _eff_ledger(tmp_path)
    rows = {r.name: r for r in L.history(ledger).spread}
    for name, xs in (("cost_per_case", COSTS), ("cache_hit_share", CACHE), ("latency_p95", P95)):
        r = rows[name]
        med = statistics.median(xs)
        mad = 1.4826 * statistics.median(abs(x - med) for x in xs)
        assert (r.runs, r.min, r.max, r.latest) == (6, min(xs), max(xs), xs[-1])
        assert r.median == pytest.approx(med) and r.mad_spread == pytest.approx(mad)
        assert r.cv == pytest.approx(mad / abs(med))
        assert r.model == "claude-sonnet-5-5" and r.mode == "live"


@pytest.mark.contract
def test_spread_table_fields_per_model_and_name(tmp_path, capsys):
    """contract: one spread row per (model, efficiency name), with its fields, in `history show
    --json`, --md and --html."""
    ledger = _eff_ledger(tmp_path)
    md, page = tmp_path / "h.md", tmp_path / "h.html"
    assert (
        cli.main(
            [
                "history",
                "show",
                "--ledger",
                str(ledger),
                "--json",
                "--md",
                str(md),
                "--html",
                str(page),
            ]
        )
        == 0
    )
    spread = json.loads(capsys.readouterr().out)["spread"]
    assert {(r["model"], r["name"]) for r in spread} >= {
        ("claude-sonnet-5-5", n)
        for n in (
            "cost_per_case",
            "cache_hit_share",
            "latency_p95",
            "tokens_in_per_case",
            "system_cost",
        )
    }
    for r in spread:
        assert set(r) == {
            "model",
            "mode",
            "name",
            "unit",
            "runs",
            "median",
            "mad_spread",
            "cv",
            "min",
            "max",
            "latest",
            "status",
        }
    assert "Efficiency spread" in md.read_text(
        encoding="utf-8"
    ) and "Efficiency run-to-run" in page.read_text(encoding="utf-8")


@pytest.mark.golden
def test_efficiency_blocks_from_summary_json_across_runs(tmp_path):
    """golden: the spread table for six EvalKit runs' efficiency blocks equals the golden."""
    ledger = _eff_ledger(tmp_path)
    got = json.dumps([r.as_dict() for r in L.history(ledger).spread], indent=1, sort_keys=True)
    golden = LEDGER_FIX / "efficiency-spread.json"
    if UPDATE:
        golden.write_text(got + "\n", encoding="utf-8")
    assert _rounded(json.loads(got)) == _rounded(json.loads(golden.read_text(encoding="utf-8")))


def _rounded(obj):
    if isinstance(obj, float):
        return float(f"{obj:.10g}")
    if isinstance(obj, dict):
        return {k: _rounded(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rounded(v) for v in obj]
    return obj


def test_the_same_ledger_gives_the_same_spread_table(tmp_path):
    """repro: the same ledger gives the same table, and a fresh ledger of the same runs too."""
    a = _eff_ledger(tmp_path / "a")
    b = _eff_ledger(tmp_path / "b")
    ta = [r.as_dict() for r in L.history(a).spread]
    assert (
        ta
        == [r.as_dict() for r in L.history(a).spread]
        == [r.as_dict() for r in L.history(b).spread]
    )


# ==================================== LED-05 trend chart ====================================


def _fraud_ledger(tmp: Path) -> Path:
    ledger = tmp / "runs.jsonl"
    for i, r in enumerate(FRAUD_HISTORY):
        L.record_run(ledger, fraud_run(tmp, i, *r), at=at(i + 1))
    L.reset_band(ledger, metric="cost_per_1000_usd", reason="new prices", at=at(8))
    L.record_run(ledger, fraud_run(tmp, 7, 14, 209, 4.16), at=at(9))
    return ledger


def test_chart_bands_are_drawn_from_the_led_02_numbers(tmp_path):
    """logical: the chart draws, per banded run, the warn and alarm bands of that run's LED-02
    band; learning runs have none; the description states the latest band's numbers."""
    ledger = _fraud_ledger(tmp_path)
    hist = L.history(ledger, metric="leakage")
    (series,) = hist.series
    svg = L.trend_svg(series)
    banded = [p for p in series.points if p.centre is not None]
    assert len(banded) == 2  # runs 6 and 7 have 5 and 6 comparable runs before them
    assert svg.count('class="band-a"') == svg.count('class="band-w"') == len(banded)
    latest = series.latest
    from evalkit.ledger.chart import num

    assert f"centre {num(latest.centre)}" in svg and f"spread {num(latest.spread)}" in svg
    assert latest.status == "alarm"
    assert latest == next(b for b in L.bands(ledger) if b.metric == "leakage")


@pytest.mark.contract
def test_svg_has_title_desc_and_flags_by_shape_and_text(tmp_path):
    """contract: every chart has <title> and <desc>; a flagged run has its own shape and a text
    label; settings changes and band resets are marked; the PSI panel has its 0.1/0.25 lines."""
    ledger = _fraud_ledger(tmp_path)
    rows = [
        {"name": "drift.psi", "unit": "ratio", "value": v, "labels": {"feature": "amount"}}
        for v in (0.02, 0.15, 0.3)
    ]
    for i, row in enumerate(rows):
        L.record_run(
            ledger,
            write_run(tmp_path / f"d{i}", [row], run_id=f"d{i}", project="fraud-review-bedrock"),
        )
    hist = L.history(ledger)
    page = L.history_html(hist)
    for svg in re.findall(r"<svg.*?</svg>", page, re.S):
        assert re.search(r"<title id=\"[^\"]+\">[^<]+</title><desc id=\"[^\"]+\">[^<]+</desc>", svg)
        assert 'role="img"' in svg
    leak = L.trend_svg(next(s for s in hist.series if s.metric == "leakage"))
    assert '<polygon class="alarm"' in leak and ">alarm<" in leak  # diamond + label
    assert ">settings<" in leak
    cost = L.trend_svg(next(s for s in hist.series if s.metric == "cost_per_1000_usd"))
    assert ">band reset<" in cost
    (psi,) = hist.psi
    panel = L.psi_svg(psi)
    assert "warn 0.1" in panel and "alarm 0.25" in panel and ">alarm<" in panel
    assert ">warn<" in panel


@pytest.mark.contract
def test_the_report_has_a_trend_section_only_with_history(tmp_path, capsys):
    """contract: `evalkit report RUN --history LEDGER` adds a Trend section for the run's
    metrics; without --history the report is unchanged."""
    run = tmp_path / "run"
    shutil.copytree(EXAMPLE_RUN, run)
    assert cli.main(["report", str(run)]) == 0
    plain = (run / "report.html").read_text(encoding="utf-8")
    assert 'id="trend-h"' not in plain
    ledger = tmp_path / "runs.jsonl"
    L.record_run(ledger, run, at=at(1))
    capsys.readouterr()
    assert cli.main(["report", str(run), "--history", str(ledger), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["trend_charts"] > 0
    html = (run / "report.html").read_text(encoding="utf-8")
    assert 'id="trend-h"' in html and html.count('<svg class="trend"') > 0
    assert html.endswith("</main><script>" + html.split("</main><script>")[1])


@pytest.mark.golden
def test_trend_svg_bytes_match_the_golden(tmp_path):
    """golden: the SVG bytes of the fixture ledger's leakage chart."""
    ledger = _fraud_ledger(tmp_path)
    svg = L.trend_svg(L.history(ledger, metric="leakage").series[0], chart_id="leakage")
    golden = LEDGER_FIX / "trend-leakage.svg"
    if UPDATE:
        golden.write_text(svg + "\n", encoding="utf-8")
    assert svg + "\n" == golden.read_text(encoding="utf-8")


def test_rendering_twice_gives_identical_bytes(tmp_path):
    """repro: the history page and every chart are pure functions of the ledger."""
    ledger = _fraud_ledger(tmp_path)
    a = L.history_html(L.history(ledger))
    b = L.history_html(L.history(ledger))
    assert a == b
    out1, out2 = tmp_path / "1.html", tmp_path / "2.html"
    for out in (out1, out2):
        assert cli.main(["history", "show", "--ledger", str(ledger), "--html", str(out)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    assert "2026" not in a.split("<h2")[0] or "Run history" in a  # no render timestamp


@pytest.mark.safety
def test_trend_charts_show_heldout_only_at_split_level(tmp_path):
    """safety: charts, tables and the page never show a held-out row below split level."""
    ledger = tmp_path / "runs.jsonl"
    for i in range(6):
        rows = [
            rate("acc", 30 + i % 3, 40, split="heldout", model="m1"),
            rate("acc", 3, 4, split="heldout", model="m1", topic="T-SECRET"),
        ]
        L.record_run(ledger, write_run(tmp_path / f"r{i}", rows, run_id=f"r{i}"), at=at(i + 1))
    page = L.history_html(L.history(ledger))
    assert "T-SECRET" not in page and "topic" not in page
    assert "model=m1, split=heldout" in page
