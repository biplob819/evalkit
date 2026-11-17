"""B5.15 (D-71): opt-in `sql_match` shape equivalences (SQE-01) beside an unchanged strict
match (SQE-02).

Ported from agentic-analytics (MIT, same author), `tests/scoring/test_tolerant.py` at commit
f6abdec (its OWNER D-81), the asking project's failing cases as EvalKit tests (D-61 rule 3).
The positive cases are that baseline's real failures (`fixtures/sql_equivalences/
d81_baseline_cases.json`, vendored verbatim: the predicted rows of agentic-analytics run
run-20261117T083854Z-c89eaa, trial 1, and its gold, aggregate counts and shares over HM Land
Registry Price Paid data, no personal data). Each rule also has negative cases that must stay
wrong. agentic-analytics' `Question` becomes `question=` text and `rel_tol`/`abs_tol`; its
`expects_empty` flag becomes naming `empty_check`; its strict match is EvalKit's own, called
with `allow_extra_columns=True` as agentic-analytics calls it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from evalkit.schemas import ResultSet
from evalkit.scorers import ScoreResult, sql_match
from evalkit.scorers.sql_equivalences import EQUIVALENCES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sql_equivalences"
CASES: list[dict[str, Any]] = json.loads(
    (FIXTURES / "d81_baseline_cases.json").read_text(encoding="utf-8")
)["cases"]
ALL = EQUIVALENCES
PCT = (0.005, 0.01)  # agentic-analytics: rel 0.005, percent-unit abs 0.01
EXACT = (1e-9, 1e-9)  # agentic-analytics' default Tolerance()
REPO = Path(__file__).resolve().parents[2]


def rs(columns: Any, rows: Any, *, ordered: bool = False) -> ResultSet:
    return ResultSet(columns=tuple(columns), rows=tuple(tuple(r) for r in rows), ordered=ordered)


def run(
    predicted: ResultSet,
    gold: ResultSet,
    question: str | None = None,
    *,
    tol: tuple[float, float] = EXACT,
    names: Any = ALL,
) -> ScoreResult:
    rel, abs_ = tol
    return sql_match(
        predicted,
        gold,
        rel_tol=rel,
        abs_tol=abs_,
        allow_extra_columns=True,
        question=question,
        equivalences=names,
    )


def rescue(
    predicted: ResultSet,
    gold: ResultSet,
    question: str | None = None,
    *,
    tol: tuple[float, float] = EXACT,
    names: Any = ALL,
) -> tuple[str, ...]:
    """The equivalences that turned a strict miss into a match, or () (agentic-analytics'
    `tolerant_match`)."""
    res = run(predicted, gold, question, tol=tol, names=names)
    if res.value == 1.0 and res.details["correct_strict"] is False:
        return tuple(res.details["equivalences"])
    return ()


def case_args(c: dict[str, Any]) -> tuple[ResultSet, ResultSet, str, tuple[float, float]]:
    q = c["question"]
    gold = rs(c["gold"]["columns"], c["gold"]["rows"], ordered=q.get("ordered", False))
    pred = rs(c["predicted"]["columns"], c["predicted"]["rows"])
    return pred, gold, q["text"], (q["tolerance"]["rel"], q["tolerance"]["abs"])


def case_names(c: dict[str, Any]) -> tuple[str, ...]:
    """Every equivalence, except empty_check unless the question expects no data."""
    expects_empty = c["question"].get("expects_empty", False)
    return tuple(n for n in ALL if n != "empty_check" or expects_empty)


# The real failing cases


@pytest.mark.golden
@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['backend']}-{c['question_id']}")
def test_baseline_failure_passes_with_its_equivalence_and_stays_wrong_strict(case):
    """SQE-01/SQE-02 golden: each of agentic-analytics' baseline failures."""
    pred, gold, text, (rel, abs_) = case_args(case)
    strict = sql_match(pred, gold, rel_tol=rel, abs_tol=abs_, allow_extra_columns=True)
    assert strict.value == 0.0 and "correct_strict" not in strict.details
    res = run(pred, gold, text, tol=(rel, abs_), names=case_names(case))
    assert res.value == 1.0
    assert res.details["correct_strict"] is False
    assert res.details["equivalences"] == (case["equivalence"],)
    assert res.details["strict_reason"] == strict.details["reason"]


def test_fixture_covers_every_example_the_ruling_names():
    got = {(c["backend"], c["question_id"], c["equivalence"]) for c in CASES}
    assert got == {
        ("cortex_analyst", "q-006", "percent_scale"),
        ("cortex_analyst", "q-047", "percent_scale"),
        ("cortex_analyst", "q-085", "percent_scale"),
        ("gpt_mcp", "q-005", "pivot"),
        ("gpt_mcp", "q-057", "pivot"),
        ("gpt_mcp", "q-020", "empty_check"),
        ("gpt_mcp", "q-040", "empty_check"),
        ("cortex_analyst", "q-025", "period_number"),
    }


# 1. percent_scale

PCT_GOLD = rs(("new_build_pct",), ((11.389191233977966,),))
PCT_TEXT = "What percentage of all property sales in 2023 were new builds?"


def test_fraction_passes_only_when_the_question_asks_for_a_percentage():
    """SQE-01 logical: share and proportion are not percentages."""
    pred = rs(("new_build_pct",), ((0.113892,),))
    assert rescue(pred, PCT_GOLD, PCT_TEXT, tol=PCT) == ("percent_scale",)
    for text in (
        "What share of all property sales in 2023 were new builds?",
        "What proportion of all property sales in 2023 were new builds?",
    ):
        assert rescue(pred, PCT_GOLD, text, tol=PCT) == ()


def test_fraction_needs_a_percent_named_column_and_gets_a_percent_unit_tolerance():
    """The percent column alone is compared with a percent-unit abs (0.01), whatever the
    caller's abs_tol (agentic-analytics' per-column tolerance); no abs_tol >= 0.01 is needed."""
    pred = rs(("new_build_pct",), ((0.113892,),))
    assert rescue(pred, PCT_GOLD, PCT_TEXT, tol=(0.005, 0.0001)) == ("percent_scale",)
    assert rescue(pred, PCT_GOLD, PCT_TEXT, tol=EXACT) == ("percent_scale",)
    # 0.02 percentage points off is outside the percent-unit tolerance
    assert rescue(rs(("new_build_pct",), ((0.113692,),)), PCT_GOLD, PCT_TEXT, tol=EXACT) == ()
    # a column whose name is not a percentage (a median) is never rescaled
    gold = rs(("median_2023", "pct_change"), ((290000, 1.5),))
    text = "Show the median in 2023 and the percentage change."
    assert rescue(rs(("median_2023", "pct_change"), ((2900, 1.5),)), gold, text, tol=PCT) == ()


def test_wrong_fraction_and_already_percent_values_stay_as_they_are():
    assert rescue(rs(("new_build_pct",), ((0.1239,),)), PCT_GOLD, PCT_TEXT, tol=PCT) == ()
    # rounded to whole percent the fraction is outside the tolerance
    assert rescue(rs(("new_build_pct",), ((0.11,),)), PCT_GOLD, PCT_TEXT, tol=PCT) == ()
    # a wrong percent is not rescued by scaling either way
    assert rescue(rs(("new_build_pct",), ((1138.9,),)), PCT_GOLD, PCT_TEXT, tol=PCT) == ()


def test_percent_scale_leaves_other_columns_exact():
    """agentic-analytics compares the count columns exactly; so does sql_match here: the
    percent-unit abs widens the scaled column alone, and the caller's tolerance is exact."""
    case = next(c for c in CASES if c["question_id"] == "q-085")
    pred, gold, text, _ = case_args(case)
    assert rescue(pred, gold, text, tol=EXACT) == ("percent_scale",)
    rows = [list(r) for r in case["predicted"]["rows"]]
    rows[0][2] += 1  # one sales count off by one
    assert rescue(rs(case["predicted"]["columns"], rows), gold, text, tol=EXACT) == ()


def test_percent_unit_tolerance_widens_only_the_scaled_column_on_a_percent_gold_column():
    """A second percent column that was not scaled keeps the caller's tolerance, and a
    percent-named gold column takes the scaled values only where the predicted column maps."""
    gold = rs(("a_pct", "b_pct"), ((11.389191233977966, 50.0),))
    text = "What percentage of sales were new builds, and what percentage freehold?"
    assert rescue(rs(("a_pct", "b_pct"), ((0.113892, 50.0),)), gold, text) == ("percent_scale",)
    assert rescue(rs(("a_pct", "b_pct"), ((0.113892, 50.009),)), gold, text) == ()


# 2. pivot

TENURE = rs(("duration", "sales"), (("freehold", 651962), ("leasehold", 209820)))
TENURE_Q = "How many sales were there in 2023 for each tenure (freehold, leasehold and unknown)?"


def test_pivot_case_insensitive_with_zero_extra_label():
    pred = rs(("FREEHOLD", "Leasehold", "unknown"), ((651962, 209820, 0),))
    assert rescue(pred, TENURE, TENURE_Q) == ("pivot",)
    assert rescue(rs(("freehold", "leasehold"), ((651962, 209820),)), TENURE, TENURE_Q)


def test_pivot_with_a_non_zero_extra_label_or_column_stays_wrong():
    pred = rs(("freehold", "leasehold", "unknown"), ((651962, 209820, 3),))
    assert rescue(pred, TENURE, TENURE_Q) == ()
    total = rs(("freehold", "leasehold", "total"), ((651962, 209820, 861782),))
    assert rescue(total, TENURE, TENURE_Q) == ()


def test_pivot_with_a_wrong_value_or_a_missing_label_stays_wrong():
    wrong = rs(("freehold", "leasehold", "unknown"), ((651962, 209821, 0),))
    assert rescue(wrong, TENURE, TENURE_Q) == ()
    missing = rs(("freehold", "unknown"), ((651962, 0),))
    assert rescue(missing, TENURE, TENURE_Q) == ()
    two_rows = rs(("freehold", "leasehold"), ((651962, 209820), (651962, 209820)))
    assert rescue(two_rows, TENURE, TENURE_Q) == ()


def test_pivot_matches_separator_variants_of_a_label():
    gold = rs(("type", "sales"), (("semi-detached", 10), ("flat", 5)))
    pred = rs(("Semi_Detached", "flat", "other"), ((10, 5, 0),))
    assert rescue(pred, gold, "How many sales of each property type?") == ("pivot",)


def test_pivot_needs_a_long_gold_of_labels_and_numbers():
    wide = rs(("freehold", "leasehold"), ((651962, 209820),))
    pred = rs(("freehold", "leasehold"), ((651962, 209821),))
    assert rescue(pred, wide, TENURE_Q) == ()
    # a swapped long gold (value column first) unpivots into the same order
    swapped = rs(("sales", "duration"), ((651962, "freehold"), (209820, "leasehold")))
    good = rs(("freehold", "leasehold", "unknown"), ((651962, 209820, 0),))
    assert rescue(good, swapped, TENURE_Q) == ("pivot",)
    labels_twice = rs(("duration", "sales"), (("freehold", 1), ("Freehold", 1)))
    assert rescue(rs(("freehold",), ((1,),)), labels_twice, TENURE_Q) == ()


# 3. empty_check

EMPTY = rs(("type", "sales"), ())
EMPTY_Q = "How many sales of each property type were recorded in 1994?"


def test_zero_count_check_on_an_empty_gold_counts_as_no_rows():
    pred = rs(("earliest", "latest", "sales_in_1994"), (("1995-01-01", "2026-09-30", 0),))
    assert rescue(pred, EMPTY, EMPTY_Q) == ("empty_check",)
    nulls = rs(("sales", "median_price"), ((0, None),))
    assert rescue(nulls, EMPTY, EMPTY_Q) == ("empty_check",)
    text_zero = rs(("earliest", "sales_in_1994"), (("1995-01-01", " 0.0 "),))
    assert rescue(text_zero, EMPTY, EMPTY_Q) == ("empty_check",)


def test_empty_check_with_a_non_zero_count_stays_wrong():
    pred = rs(("earliest", "latest", "sales_in_1994"), (("1995-01-01", "2026-09-30", 7),))
    assert rescue(pred, EMPTY, EMPTY_Q) == ()
    text_count = rs(("earliest", "sales_in_1994"), (("1995-01-01", "7"),))
    assert rescue(text_count, EMPTY, EMPTY_Q) == ()
    year = rs(("year", "sales"), ((1994, 0),))  # a non-zero number is not a no-data check
    assert rescue(year, EMPTY, EMPTY_Q) == ()


def test_empty_check_needs_to_be_named_one_row_and_a_count():
    zero = rs(("sales",), ((0,),))
    no_empty = tuple(n for n in ALL if n != "empty_check")  # not an expects_empty question
    assert rescue(zero, EMPTY, EMPTY_Q, names=no_empty) == ()
    two = rs(("sales",), ((0,), (0,)))
    assert rescue(two, EMPTY, EMPTY_Q) == ()
    dates_only = rs(("earliest", "latest"), (("1995-01-01", "2026-09-30"),))
    assert rescue(dates_only, EMPTY, EMPTY_Q) == ()
    boolean = rs(("any_sales", "sales"), ((False, 0),))
    assert rescue(boolean, EMPTY, EMPTY_Q) == ()


# 4. period_number

MONTHS = rs(("month", "sales"), ((3, 70), (4, 80)))
MONTH_Q = "How many sales were there in March 2016 and in April 2016, by month number (1–12)?"
QUARTERS = rs(("quarter", "sales"), ((1, 10), (2, 20), (3, 30), (4, 40)))
QUARTER_Q = "How many sales were recorded in each calendar quarter of 2022?"
QDATES = ("2022-01-01", "2022-04-01", "2022-07-01", "2022-10-01")


def test_month_start_dates_match_month_numbers():
    pred = rs(("month", "sales"), (("2016-03-01", 70), ("2016-04-01 00:00:00", 80)))
    assert rescue(pred, MONTHS, MONTH_Q) == ("period_number",)


def test_quarter_start_dates_match_quarter_numbers():
    """Columns in another order: sql_match maps equal-width tables by name (else by position;
    agentic-analytics maps them by value), so they carry the gold's names in any case."""
    dates = ("2022-01-01", "2022-04-01", "2022-07-01T00:00:00Z", "2022-10-01")
    pred = rs(("SALES", "Quarter"), list(zip((10, 20, 30, 40), dates, strict=True)))
    assert rescue(pred, QUARTERS, QUARTER_Q) == ("period_number",)


def test_dates_not_at_period_starts_stay_wrong():
    mid = rs(("month", "sales"), (("2016-03-15", 70), ("2016-04-01", 80)))
    assert rescue(mid, MONTHS, MONTH_Q) == ()
    months = ("2022-01-01", "2022-02-01", "2022-03-01", "2022-05-01")
    not_quarters = rs(("q", "sales"), list(zip(months, (10, 20, 30, 40), strict=True)))
    assert rescue(not_quarters, QUARTERS, QUARTER_Q) == ()


def test_dates_over_two_years_never_become_month_numbers():
    pred = rs(("month", "sales"), (("2016-03-01", 70), ("2017-04-01", 80)))
    assert rescue(pred, MONTHS, MONTH_Q) == ()


def test_period_number_needs_the_question_to_name_the_period_and_right_counts():
    pred = rs(("month", "sales"), (("2016-03-01", 70), ("2016-04-01", 80)))
    assert rescue(pred, MONTHS, "How many sales were there on those two dates in 2016?") == ()
    wrong = rs(("month", "sales"), (("2016-03-01", 70), ("2016-04-01", 81)))
    assert rescue(wrong, MONTHS, MONTH_Q) == ()


def test_percent_scale_and_period_number_apply_together():
    gold = rs(("month", "pct_change"), ((1, 5.0), (2, -2.5)))
    text = "For each month of 2024, show the percentage change on 2023."
    pred = rs(("month", "pct_change"), (("2024-01-01", 0.05), ("2024-02-01", -0.025)))
    assert rescue(pred, gold, text, tol=PCT) == ("percent_scale", "period_number")
    alone = ("percent_scale",)
    assert rescue(pred, gold, text, tol=PCT, names=alone) == ()


# Off unless named; the question gates two rules; a miss keeps its strict reason


@pytest.mark.safety
def test_no_equivalence_applies_unless_named():
    """SQE-02 safety (agentic-analytics: never on clarify-expected questions): with nothing
    named, every baseline failure stays a failure, with exactly the strict details."""
    for case in CASES:
        pred, gold, text, (rel, abs_) = case_args(case)
        strict = sql_match(pred, gold, rel_tol=rel, abs_tol=abs_, allow_extra_columns=True)
        for names in ((), [], frozenset()):
            res = sql_match(
                pred,
                gold,
                rel_tol=rel,
                abs_tol=abs_,
                allow_extra_columns=True,
                question=text,
                equivalences=names,
            )
            assert res.model_dump_json() == strict.model_dump_json()
            assert res.value == 0.0 and "equivalences" not in res.details


def test_rules_that_read_the_question_never_apply_without_one():
    """agentic-analytics: never on probes. percent_scale and period_number need the text."""
    pct = rs(("new_build_pct",), ((0.113892,),))
    assert rescue(pct, PCT_GOLD, None, tol=PCT) == ()
    quarters = rs(("q", "sales"), list(zip(QDATES, (10, 20, 30, 40), strict=True)))
    assert rescue(quarters, QUARTERS, None) == ()
    # pivot and empty_check never read it
    tenure = rs(("freehold", "leasehold", "unknown"), ((651962, 209820, 0),))
    assert rescue(tenure, TENURE, None) == ("pivot",)


def test_a_miss_keeps_the_strict_failure_and_records_no_equivalence():
    """agentic-analytics: errors and truncated results are not rescued."""
    pred = rs(("freehold", "leasehold", "unknown"), ((651962, 209820, 5),))
    strict = sql_match(pred, TENURE, allow_extra_columns=True)
    res = run(pred, TENURE, TENURE_Q)
    assert res.value == 0.0
    assert dict(res.details) == {**strict.details, "correct_strict": False, "equivalences": ()}


def test_a_strict_match_records_no_equivalence_and_equal_scores():
    pred = rs(("duration", "sales"), (("leasehold", 209820), ("freehold", 651962)))
    strict = sql_match(pred, TENURE, allow_extra_columns=True)
    res = run(pred, TENURE, TENURE_Q)
    assert res.value == strict.value == 1.0
    assert res.details["correct_strict"] is True and res.details["equivalences"] == ()
    assert "strict_reason" not in res.details


# The result format


@pytest.mark.golden
def test_default_call_reproduces_pre_b515_outputs_byte_for_byte():
    """SQE-02 golden: every sql_match_cases.json case and every baseline case, with and without
    allow_extra_columns, gives the JSON sql_match gave before B5.15 (snapshot made on ca50e38).
    Failure details were regenerated once after B5.15 made them independent of the hash seed;
    values and reasons are those of ca50e38, and the bytes now match under several seeds."""
    snap = json.loads((FIXTURES / "strict_snapshot.json").read_text(encoding="utf-8"))["cases"]
    assert len(snap) == 58
    for c in snap:
        res = sql_match(ResultSet(**c["predicted"]), ResultSet(**c["gold"]), **c["kwargs"])
        old = json.loads(c["result"])
        assert res.value == old["value"], c["name"]
        assert res.details.get("reason") == old["details"].get("reason"), c["name"]
    script = (
        "import json,sys\n"
        "from evalkit.schemas import ResultSet\n"
        "from evalkit.scorers import sql_match\n"
        "snap=json.load(open(sys.argv[1],encoding='utf-8'))['cases']\n"
        "bad=[c['name'] for c in snap if sql_match(ResultSet(**c['predicted']),"
        "ResultSet(**c['gold']),**c['kwargs']).model_dump_json()!=c['result']]\n"
        "print(json.dumps(bad))\n"
    )
    for seed in ("0", "1", "12345"):  # details no longer depend on the hash seed
        out = _python(script, str(FIXTURES / "strict_snapshot.json"), seed=seed)
        assert json.loads(out) == [], seed


@pytest.mark.contract
def test_details_record_strict_and_equivalences_side_by_side():
    """SQE-02 contract: the keys, their JSON, and a round trip through ScoreResult."""
    pred = rs(("freehold", "leasehold", "unknown"), ((651962, 209820, 0),))
    res = run(pred, TENURE, TENURE_Q)
    data = json.loads(res.model_dump_json())
    assert data["value"] == 1.0
    assert data["details"]["correct_strict"] is False
    assert data["details"]["equivalences"] == ["pivot"]
    assert data["details"]["strict_reason"] == "no_matching_columns"
    assert ScoreResult.model_validate_json(res.model_dump_json()) == res
    strict_pass = run(rs(("duration", "sales"), TENURE.rows), TENURE, TENURE_Q)
    assert json.loads(strict_pass.model_dump_json())["details"] == {
        "columns": "by_name",
        "ordered": False,
        "correct_strict": True,
        "equivalences": [],
    }


@pytest.mark.contract
@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"equivalences": "pivot"}, TypeError),  # a bare string is not a list of names
        ({"equivalences": ("pivots",)}, ValueError),  # unknown name
        ({"equivalences": ("PIVOT",)}, ValueError),  # names are exact
        ({"equivalences": ("pivot", None)}, ValueError),
        ({"equivalences": ("pivot",), "question": b"How many?"}, TypeError),
    ],
)
def test_bad_equivalence_arguments_are_rejected(kwargs, error):
    """SQE-01 contract."""
    with pytest.raises(error):
        sql_match(TENURE, TENURE, **kwargs)


# Guards whose behaviour matters (agentic-analytics' D-81 verifier)


def test_percent_words_percent_sign_counts_and_percentile_does_not():
    pred = rs(("new_build_pct",), ((0.113892,),))
    sign = "What % of all property sales in 2023 were new builds?"
    assert rescue(pred, PCT_GOLD, sign, tol=PCT) == ("percent_scale",)
    pctl = "What was the 90th percentile of new build share in 2023?"
    assert rescue(pred, PCT_GOLD, pctl, tol=PCT) == ()


def test_rescued_rows_keep_their_order_on_an_ordered_question():
    dates = ("2022-04-01", "2022-01-01", "2022-07-01", "2022-10-01")  # quarter 2 first
    pred = rs(("q", "sales"), list(zip(dates, (20, 10, 30, 40), strict=True)))
    ordered = rs(QUARTERS.columns, QUARTERS.rows, ordered=True)
    assert rescue(pred, ordered, QUARTER_Q) == ()
    assert rescue(pred, QUARTERS, QUARTER_Q) == ("period_number",)


def test_quarter_dates_need_the_question_to_name_quarters():
    pred = rs(("q", "sales"), list(zip(QDATES, (10, 20, 30, 40), strict=True)))
    assert rescue(pred, QUARTERS, "How many sales were recorded in each period of 2022?") == ()
    timed = rs(
        ("q", "sales"),
        [(d + " 09:30:00", n) for d, n in zip(QDATES, (10, 20, 30, 40), strict=True)],
    )
    assert rescue(timed, QUARTERS, QUARTER_Q) == ()


def test_a_strict_pass_is_never_relabelled_as_an_equivalence():
    """SQE-02 logical: strict runs first."""
    exact = rs(("new_build_pct",), ((11.389191233977966,),))
    res = run(exact, PCT_GOLD, PCT_TEXT, tol=PCT)
    assert res.value == 1.0 and res.details["correct_strict"] is True
    assert res.details["equivalences"] == ()
    # a fraction that already fits unscaled (|0.1| within abs 0.2) is a strict pass, not a rescue
    small = rs(("pct",), ((0.1,),))
    res = run(small, rs(("pct",), ((0.2,),)), PCT_TEXT, tol=(0.0, 0.2))
    assert res.value == 1.0 and res.details["correct_strict"] is True


# Stricter than the reference (B5.15 verifier): an equivalence never makes a wrong answer right


def test_percent_columns_are_named_by_whole_words_not_percentile_or_points():
    """`percentile` is not a percent column, and `points` is one only when the question asks
    for percentage points: a latency or a loyalty average 100x off stays wrong."""
    gold = rs(("pct_over", "latency_percentile_95"), ((12.5, 1.23),))
    pred = rs(gold.columns, ((12.5, 0.0123),))
    text = "What percentage of requests exceed the SLA and what is the 95th percentile latency?"
    assert rescue(pred, gold, text, tol=PCT) == ()
    gold = rs(("pct", "avg_loyalty_points"), ((40.0, 1230.0),))
    pred = rs(gold.columns, ((40.0, 12.3),))
    text = "What percent of members are active and their avg loyalty points?"
    assert rescue(pred, gold, text, tol=PCT) == ()
    # percentage points asked for: a points column is a percent column
    gold = rs(("year", "change_points"), ((2024, 1.5),))
    text = "By how many percentage points did the new-build share change in 2024?"
    assert rescue(rs(gold.columns, ((2024, 0.015),)), gold, text) == ("percent_scale",)
    # whole words, any case; a camelCase name is one word (agentic-analytics D-91, stricter)
    for name in ("NEW_BUILD_PCT", "new-build percent", "Percentage", "pct2023"):
        gold = rs((name,), ((11.389191233977966,),))
        assert rescue(rs((name,), ((0.113892,),)), gold, PCT_TEXT) == ("percent_scale",), name
    not_percent = ("newBuildPct", "pctile", "pcnt", "percentile_90", "rate", "points")
    not_percent += ("appointments",)
    for name in not_percent:
        gold = rs((name,), ((11.389191233977966,),))
        assert rescue(rs((name,), ((0.113892,),)), gold, PCT_TEXT) == (), name


def test_a_question_that_negates_percent_does_not_ask_for_one():
    gold = rs(("ratio_pct",), ((25.0,),))
    pred = rs(("ratio_pct",), ((0.25,),))
    for text in (
        "Give the ratio as a fraction (not %)",
        "Give the ratio as a fraction, not a percentage.",
        "Return the share as a decimal rather than as a percent.",
        "Return the share as a decimal instead of a percentage.",
    ):
        assert rescue(pred, gold, text, tol=PCT) == (), text
    # a negation elsewhere does not hide a request for a percentage
    text = "Not counting refunds, what percentage of orders shipped late?"
    assert rescue(pred, gold, text, tol=PCT) == ("percent_scale",)


def test_quarter_reading_needs_a_gold_column_named_quarter():
    """April read as quarter 2 must not match a gold month 2 (February)."""
    gold = rs(("month", "sales"), ((2, 10),))
    pred = rs(("month", "sales"), (("2024-04-01", 10),))
    assert rescue(pred, gold, "Which month of the first quarter had the most sales?") == ()
    # the month reading still applies to the gold month column
    feb = rs(("month", "sales"), (("2024-02-01", 10),))
    text = "Which month of the first quarter of 2024 had the most sales?"
    assert rescue(feb, gold, text) == ("period_number",)


def test_month_dates_in_a_year_the_question_does_not_name_stay_wrong():
    gold = rs(("month",), ((3,),))
    pred = rs(("month",), (("2023-03-01",),))
    assert rescue(pred, gold, "Which month of 2024 had the highest revenue?") == ()
    assert rescue(pred, gold, "Which month of 2023 had the highest revenue?") == ("period_number",)
    # no year named: any one calendar year
    assert rescue(pred, gold, "Which month had the highest revenue?") == ("period_number",)


def test_period_numbers_land_only_on_a_gold_column_named_month_or_quarter():
    """A count of 1..12 is not a month number."""
    gold = rs(("region_count", "n"), ((5, 3),))
    pred = rs(("d", "n"), (("2024-05-01", 3),))
    assert rescue(pred, gold, "How many regions per month?") == ()
    gold = rs(("qtr", "n"), ((2, 3),))
    assert rescue(rs(("d", "n"), (("2024-04-01", 3),)), gold, "Sales per quarter?") == (
        "period_number",
    )
    # the converted column must be the one compared with the month column
    gold = rs(("month", "stores"), ((5, 3),))
    pred = rs(("stores", "month"), (("2024-03-01", 5),))  # by name: dates land on stores
    assert rescue(pred, gold, "How many stores opened per month?") == ()


def test_empty_check_needs_count_columns_and_no_stray_text():
    """An error message beside a zero, or a zero average, is not a no-data check."""
    error = rs(("msg", "n"), (("ERROR: table missing", 0),))
    assert rescue(error, rs(("n",), ()), "Sales in 1994?") == ()
    avg = rs(("avg_price",), ((0.0,),))
    assert rescue(avg, rs(("avg_price",), ()), "Average price in 1994?") == ()
    for name in ("total", "total_revenue", "median_price", "value", "year", "SalesIn1994"):
        assert rescue(rs((name,), ((0,),)), EMPTY, EMPTY_Q) == (), name
    # an excluding word is found at a case change too; a leading total is not a count
    assert rescue(rs(("sales_avgPrice",), ((0,),)), EMPTY, EMPTY_Q) == ()
    assert rescue(rs(("total_sales",), ((0,),)), EMPTY, EMPTY_Q) == ()
    for name in ("n", "count", "num_sales", "sales_count", "SALES_IN_1994", "rows"):
        assert rescue(rs((name,), ((0,),)), EMPTY, EMPTY_Q) == ("empty_check",), name
    stamp = rs(("earliest", "n"), (("1995-01-01T00:00:00Z", 0),))
    assert rescue(stamp, EMPTY, EMPTY_Q) == ("empty_check",)


def test_pivot_extra_zero_column_must_look_like_an_omitted_label():
    """`total = 0` (a summary), the gold's own column names, and a name unlike the gold's
    labels are not labels the GROUP BY left out."""
    gold = rs(("region", "n"), (("east", 1), ("west", 2)))
    for extra in ("total", "grand_total", "grandTotal", "n", "count", "region", "avg", "pct"):
        pred = rs(("east", "west", extra), ((1, 2, 0),))
        assert rescue(pred, gold, "by region") == (), extra
    assert rescue(rs(("East", "West", "unknown"), ((1, 2, 0),)), gold, "by region") == ("pivot",)
    years = rs(("year", "n"), (("2023", 4), ("2024", 5)))
    assert rescue(rs(("2023", "2024", "2025"), ((4, 5, 0),)), years, "by year") == ("pivot",)
    assert rescue(rs(("2023", "2024", "unknown"), ((4, 5, 0),)), years, "by year") == ()


def test_a_rows_differ_failure_always_names_a_gold_row():
    """Equal row counts mean a gold group fails before any predicted-only group is reached."""
    gold = rs(("k", "v"), (("a", 1), ("b", 2)))
    res = sql_match(rs(("k", "v"), (("a", 1), ("c", 2))), gold)
    assert res.details["reason"] == "rows_differ"
    assert tuple(res.details["example_gold_row"]) == ("b", 2)
    assert "example_predicted_row" not in res.details


# agentic-analytics D-91 (a638db9): its tests for the same false passes, ported


def test_d91_percentile_and_points_columns_are_not_percent_columns():
    p95 = rs(("latency_percentile_95",), ((1.23,),))
    pred = rs(("latency_percentile_95",), ((0.0123,),))
    assert rescue(pred, p95, "What percentage latency was the 95th?", tol=PCT) == ()
    loyalty = rs(("avg_loyalty_points",), ((1230.0,),))
    pred = rs(("avg_loyalty_points",), ((12.3,),))
    assert rescue(pred, loyalty, "What % of points?", tol=PCT) == ()
    change = rs(("change_points",), ((1.5,),))
    pred = rs(("change_points",), ((0.015,),))
    assert rescue(pred, change, "By how many percentage points?", tol=PCT) == ("percent_scale",)


def test_d91_a_question_saying_not_percent_does_not_ask_for_one():
    text = "What share of 2023 sales were new builds (as a fraction, not %)?"
    assert rescue(rs(("new_build_pct",), ((0.113892,),)), PCT_GOLD, text, tol=PCT) == ()


def test_d91_quarter_reading_needs_a_gold_quarter_column():
    gold = rs(("month", "sales"), ((2, 10),))
    text = "Which month of the first quarter of 2024 had the most sales?"
    assert rescue(rs(("month", "sales"), (("2024-04-01", 10),)), gold, text) == ()


def test_d91_dates_must_be_in_a_year_the_question_names():
    gold = rs(("month", "sales"), ((3, 70),))
    text = "How many sales in each month of 2024?"
    assert rescue(rs(("month", "sales"), (("2023-03-01", 70),)), gold, text) == ()
    ok = rs(("month", "sales"), (("2024-03-01", 70),))
    assert rescue(ok, gold, text) == ("period_number",)


def test_d91_month_numbers_must_land_on_a_gold_month_column():
    gold = rs(("rank", "sales"), ((3, 70),))
    pred = rs(("month", "sales"), (("2024-03-01", 70),))
    assert rescue(pred, gold, "Sales by month of 2024?") == ()


def test_d91_empty_check_rejects_error_text_and_zero_measures():
    empty = rs(("month", "median_price"), ())
    text = "What was the median price in each month of 1993?"
    assert rescue(rs(("status", "n"), (("ERROR: table missing", 0),)), empty, text) == ()
    assert rescue(rs(("avg_price",), ((0.0,),)), empty, text) == ()
    ok = rs(("earliest_sale_date", "sales_in_1993"), (("1995-01-01", 0),))
    assert rescue(ok, empty, text) == ("empty_check",)


def test_d91_pivot_rejects_a_zero_total_column():
    gold = rs(("duration", "sales"), (("freehold", 5), ("leasehold", 3)))
    text = "How many sales for each tenure?"
    assert rescue(rs(("freehold", "leasehold", "total"), ((5, 3, 0),)), gold, text) == ()
    ok = rs(("freehold", "leasehold", "unknown"), ((5, 3, 0),))
    assert rescue(ok, gold, text) == ("pivot",)


# Reproducibility and hostile inputs


def test_equivalence_match_is_deterministic_and_order_free():
    """SQE-01 repro: the names' order or collection type, the gold's row order and repeated
    calls do not change the result."""
    for case in CASES:
        pred, gold, text, tol = case_args(case)
        names = case_names(case)
        want = run(pred, gold, text, tol=tol, names=names).model_dump_json()
        shuffled = rs(gold.columns, list(reversed(gold.rows)), ordered=gold.ordered)
        for variant in (tuple(reversed(names)), list(names), frozenset(names), names + names):
            assert run(pred, gold, text, tol=tol, names=variant).model_dump_json() == want
        assert run(pred, shuffled, text, tol=tol, names=names).model_dump_json() == want


def test_strict_and_lenient_results_are_byte_reproducible_across_processes():
    """SQE-02 repro: the baseline results with equivalences named are the same bytes in fresh
    interpreters under different hash seeds."""
    script = (
        "import json,sys\n"
        "from evalkit.schemas import ResultSet\n"
        "from evalkit.scorers import sql_match\n"
        "from evalkit.scorers.sql_equivalences import EQUIVALENCES\n"
        "cases=json.load(open(sys.argv[1],encoding='utf-8'))['cases']\n"
        "out=[]\n"
        "for c in cases:\n"
        "    q=c['question'];t=q['tolerance']\n"
        "    p=ResultSet(columns=c['predicted']['columns'],rows=c['predicted']['rows'])\n"
        "    g=ResultSet(columns=c['gold']['columns'],rows=c['gold']['rows'],"
        "ordered=q['ordered'])\n"
        "    r=sql_match(p,g,rel_tol=t['rel'],abs_tol=t['abs'],allow_extra_columns=True,"
        "question=q['text'],equivalences=EQUIVALENCES)\n"
        "    out.append(r.model_dump_json())\n"
        "print(json.dumps(out))\n"
    )
    path = str(FIXTURES / "d81_baseline_cases.json")
    outs = {_python(script, path, seed=s) for s in ("0", "1", "7", "12345")}
    assert len(outs) == 1
    assert all(json.loads(r)["value"] == 1.0 for r in json.loads(outs.pop()))


@pytest.mark.safety
def test_hostile_inputs_never_raise_never_false_pass_and_are_fast():
    """SQE-01 safety: odd cells, huge tables and near-misses are handled in bounded time and
    never turn a wrong answer into a match."""
    t0 = time.perf_counter()
    odd: list[Any] = [None, True, False, [0], {"a": 0}, "", "٠", "0x0", "1e3", 10**40, -0.0]
    for cell in odd:
        for gold in (EMPTY, TENURE, MONTHS, PCT_GOLD):
            pred = rs(("freehold", "leasehold", "month"), ((cell, cell, cell),))
            res = run(pred, gold, PCT_TEXT + " by month and quarter", tol=PCT)
            assert res.value in (0.0, 1.0)
    # -0.0 alone is a zero count; a nested zero or a boolean beside it is not a no-data check,
    # and a non-ASCII digit is text, not a count
    assert rescue(rs(("n",), ((-0.0,),)), EMPTY, EMPTY_Q) == ("empty_check",)
    for cell in ([0], {"a": 0}, False):
        assert rescue(rs(("n", "m"), ((0, cell),)), EMPTY, EMPTY_Q) == ()
    assert rescue(rs(("n",), (("٠",),)), EMPTY, EMPTY_Q) == ()
    # a fraction too large to scale finitely fits nothing
    assert rescue(rs(("new_build_pct",), ((1e307,),)), PCT_GOLD, PCT_TEXT, tol=PCT) == ()
    # malformed period starts never convert
    for bad in ("2022-13-01", "2022-00-01", "22-01-01", "2022-01-01T00:00:01", "2022-1-01"):
        pred = rs(("q", "sales"), [(bad, 10), ("2022-04-01", 20), ("2022-07-01", 30), (None, 40)])
        assert rescue(pred, QUARTERS, QUARTER_Q) == ()
    # big: a 5,000-label wide row and a 20,000-row percent table
    labels = [f"label {i}" for i in range(5_000)]
    gold = rs(("label", "n"), [(lab, i + 1) for i, lab in enumerate(labels)])
    wide = rs([lab.replace(" ", "_") for lab in labels] + ["other"], [[*range(1, 5_001), 0]])
    assert rescue(wide, gold, "How many of each?") == ("pivot",)
    off = rs(wide.columns, [[*range(1, 5_000), 4_999, 0]])
    assert rescue(off, gold, "How many of each?") == ()
    big_gold = rs(("pct",), [(i / 100,) for i in range(20_000)])
    big_pred = rs(("pct",), [(i / 10_000,) for i in range(20_000)])
    assert rescue(big_pred, big_gold, PCT_TEXT, tol=PCT) == ("percent_scale",)
    assert time.perf_counter() - t0 < 20


def _python(script: str, *args: str, seed: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHONHASHSEED")}
    env["PYTHONHASHSEED"] = seed
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        check=True,
        timeout=120,
    )
    return proc.stdout.strip()


# ---- B5.15 verifier pass 2 ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["week_of_month", "day_of_month", "month_count"])
def test_sub_period_and_count_columns_are_not_month_numbers(name):
    from evalkit.scorers import sql_match

    gold = rs((name,), ((3,),))
    pred = rs(("d",), (("2024-03-01",),))
    res = sql_match(pred, gold, question="Which month of 2024?", equivalences=["period_number"])
    assert res.value == 0.0


@pytest.mark.parametrize(
    "question",
    [
        "What share bought? Do not express it as a percentage.",
        "What share bought, without converting to a percentage?",
        "What share bought? Avoid percentages.",
    ],
)
def test_any_negation_beside_a_percent_turns_percent_scale_off(question):
    from evalkit.scorers.sql_equivalences import asks_percent

    assert asks_percent(question) is False


def test_a_percentile_column_is_not_a_percent_column():
    from evalkit.scorers import sql_match

    gold = rs(("latency_pct95",), ((1.23,),))
    pred = rs(("latency_pct95",), ((0.0123,),))
    res = sql_match(pred, gold, question="What percentage of time?", equivalences=["percent_scale"])
    assert res.value == 0.0


# ---- agentic-analytics 5a203a7: text period labels (OWNER D-89) and leading-word counts --------

Q035 = "What was the average (mean) price paid in each quarter of 2023?"
Q035_TOL = (0.005, 1e-9)  # agentic-analytics: rel 0.005 on avg_price
Q035_GOLD = rs(
    ("quarter", "avg_price"),
    (
        (1, 402525.04774426745),
        (2, 414916.01938011934),
        (3, 410081.28997829),
        (4, 400829.5861065616),
    ),
)
Q035_VALUES = (402525.05, 414916.02, 410081.29, 400829.59)


def _quarters(labels: tuple[str, ...]) -> ResultSet:
    return rs(("quarter", "mean_price_gbp"), list(zip(labels, Q035_VALUES, strict=True)))


def test_gpts_text_quarter_labels_match_quarter_numbers():
    """The real case: GPT-6.1 Sol, q-035, every trial of agentic-analytics' baseline."""
    for labels in (
        ("2023 Q1", "2023 Q2", "2023 Q3", "2023 Q4"),
        ("Q1 2023", "Q2 2023", "Q3 2023", "Q4 2023"),
        ("2023-Q1", "2023-q2", "2023-Q3", "2023-Q4"),
        ("Q1", "Q2", "Q3", "Q4"),
    ):
        assert rescue(_quarters(labels), Q035_GOLD, Q035, tol=Q035_TOL) == ("period_number",)
        assert run(_quarters(labels), Q035_GOLD, Q035, tol=Q035_TOL, names=()).value == 0.0


def test_text_quarter_labels_stay_wrong_when_they_should():
    two_years = ("2022 Q4", "2023 Q1", "2023 Q2", "2023 Q3")
    assert rescue(_quarters(two_years), Q035_GOLD, Q035, tol=Q035_TOL) == ()
    swapped = ("2023 Q2", "2023 Q1", "2023 Q3", "2023 Q4")
    assert rescue(_quarters(swapped), Q035_GOLD, Q035, tol=Q035_TOL) == ()
    assert rescue(_quarters(("Q1", "Q2", "Q3", "Q5")), Q035_GOLD, Q035, tol=Q035_TOL) == ()
    ordinals = ("first", "second", "third", "fourth")
    assert rescue(_quarters(ordinals), Q035_GOLD, Q035, tol=Q035_TOL) == ()
    unnamed = "What was the average price paid in each period of 2023?"
    labels = ("2023 Q1", "2023 Q2", "2023 Q3", "2023 Q4")
    assert rescue(_quarters(labels), Q035_GOLD, unnamed, tol=Q035_TOL) == ()


def test_text_month_labels_match_month_numbers_and_quarter_labels_never_do():
    for labels in (("2016-03", "2016-04"), ("March 2016", "April 2016"), ("Mar 2016", "Apr 2016")):
        pred = rs(("month", "sales"), list(zip(labels, (70, 80), strict=True)))
        assert rescue(pred, MONTHS, MONTH_Q) == ("period_number",), labels
    # gold months 1 and 4 are what Q1 and Q2 would map to: only the guard keeps them apart
    jan_apr = rs(("month", "sales"), ((1, 70), (4, 80)))
    quarterly = rs(("month", "sales"), (("Q1 2016", 70), ("Q2 2016", 80)))
    assert rescue(quarterly, jan_apr, MONTH_Q) == ()
    as_months = rs(("month", "sales"), (("Jan 2016", 70), ("Apr 2016", 80)))
    assert rescue(as_months, jan_apr, MONTH_Q) == ("period_number",)
    two_years = rs(("month", "sales"), (("March 2016", 70), ("April 2017", 80)))
    assert rescue(two_years, MONTHS, MONTH_Q) == ()
    not_months = rs(("month", "sales"), (("Marchy 2016", 70), ("Aprilish 2016", 80)))
    assert rescue(not_months, MONTHS, MONTH_Q) == ()


def test_text_period_labels_in_a_year_the_question_does_not_name_stay_wrong():
    labels = ("2022 Q1", "2022 Q2", "2022 Q3", "2022 Q4")
    assert rescue(_quarters(labels), Q035_GOLD, Q035, tol=Q035_TOL) == ()
    months = rs(("month", "sales"), (("March 2015", 70), ("April 2015", 80)))
    assert rescue(months, MONTHS, MONTH_Q) == ()
    numbered = rs(("month", "sales"), (("2015-03", 70), ("2015-04", 80)))
    assert rescue(numbered, MONTHS, MONTH_Q) == ()


def test_a_bare_quarter_label_needs_the_question_to_name_exactly_one_year():
    """A bare "Q1" states no year: it is read as the one year the question names, and never
    when the question names none or several (it could be any of them)."""
    bare = _quarters(("Q1", "Q2", "Q3", "Q4"))
    no_year = "What was the average (mean) price paid in each quarter?"
    assert rescue(bare, Q035_GOLD, no_year, tol=Q035_TOL) == ()
    two = "What was the average (mean) price paid in each quarter of 2023, against 2022?"
    assert rescue(bare, Q035_GOLD, two, tol=Q035_TOL) == ()
    q2024 = Q035.replace("2023", "2024")  # read as 2024, the one year named
    assert rescue(bare, Q035_GOLD, q2024, tol=Q035_TOL) == ("period_number",)
    assert rescue(_quarters(("Q1", "2023 Q2", "Q3", "Q4")), Q035_GOLD, q2024, tol=Q035_TOL) == ()
    # a bare label beside a stated one reads as the named year; a different stated year fails
    mixed = _quarters(("Q1", "2023 Q2", "Q3", "Q4"))
    assert rescue(mixed, Q035_GOLD, Q035, tol=Q035_TOL) == ("period_number",)
    other = _quarters(("Q1", "2022 Q2", "Q3", "Q4"))
    assert rescue(other, Q035_GOLD, Q035, tol=Q035_TOL) == ()
    # the year stated in the labels is enough without a named year, as for dates
    stated = _quarters(("2023 Q1", "2023 Q2", "2023 Q3", "2023 Q4"))
    assert rescue(stated, Q035_GOLD, no_year, tol=Q035_TOL) == ("period_number",)


def test_mixed_years_in_text_labels_never_convert():
    mixed = _quarters(("2023 Q1", "Q2 2024", "2023-Q3", "2023 Q4"))
    text = "Average price in each quarter of 2023 and 2024?"
    assert rescue(mixed, Q035_GOLD, text, tol=Q035_TOL) == ()
    months = rs(("month", "sales"), (("2016-03", 70), ("Apr 2017", 80)))
    assert rescue(months, MONTHS, "Sales by month in 2016 and 2017?") == ()


def test_a_quarter_label_never_lands_on_a_month_column():
    """Q1 and Q2 are not months 1 and 4, nor 1 and 2, even when the question names months
    and quarters."""
    text = "Sales by month and quarter of 2016?"
    for gold_months in ((1, 2), (1, 4)):
        gold = rs(("month", "sales"), list(zip(gold_months, (70, 80), strict=True)))
        for labels in (("Q1", "Q2"), ("2016 Q1", "2016 Q2"), ("2016-Q1", "2016-Q2")):
            pred = rs(("month", "sales"), list(zip(labels, (70, 80), strict=True)))
            assert rescue(pred, gold, text) == (), (gold_months, labels)
    # a quarter label anywhere among the converted columns turns the month reading off
    gold = rs(("month", "q", "sales"), ((1, 1, 70),))
    pred = rs(("month", "q", "sales"), (("Jan 2016", "Q1", 70),))
    assert rescue(pred, gold, "Sales by month of 2016?") == ()


@pytest.mark.parametrize(
    "labels",
    [
        ("Q1", "Q2", "Q3", "Q5"),
        ("2023 Q0", "2023 Q2", "2023 Q3", "2023 Q4"),
        ("2023-Q1", "2023-Q2", "2023-Q3", "2023-W05"),
        ("2023_Q1", "2023_Q2", "2023_Q3", "2023_Q4"),
        ("Quarter 1", "Quarter 2", "Quarter 3", "Quarter 4"),
        ("Q1 23", "Q2 23", "Q3 23", "Q4 23"),
    ],
)
def test_malformed_quarter_labels_stay_wrong(labels):
    assert rescue(_quarters(labels), Q035_GOLD, Q035, tol=Q035_TOL) == ()


@pytest.mark.parametrize(
    "labels",
    [
        ("2016-03", "2016-13"),
        ("2016-W10", "2016-W14"),
        ("2016-3", "2016-4"),
        ("Mars 2016", "Avril 2016"),
        ("2016-03-15", "2016-04-15"),
        ("03/2016", "04/2016"),
    ],
)
def test_malformed_month_labels_stay_wrong(labels):
    pred = rs(("month", "sales"), list(zip(labels, (70, 80), strict=True)))
    assert rescue(pred, MONTHS, MONTH_Q) == ()


def test_text_labels_keep_every_period_number_refusal():
    """The conditions on dates hold for labels: the question names the period, a gold column
    named as that period (not a sub-period or count), the converted column bound to it, no
    fiscal periods."""
    labels = rs(("month", "sales"), (("Mar 2016", 70), ("Apr 2016", 80)))
    assert rescue(labels, MONTHS, "How many sales on those two dates in 2016?") == ()
    for name in ("week_of_month", "month_count", "rank"):
        gold = rs((name, "sales"), ((3, 70), (4, 80)))
        assert rescue(labels, gold, MONTH_Q) == (), name
    swapped = rs(("sales", "month"), (("Mar 2016", 3), ("Apr 2016", 4)))
    assert rescue(swapped, rs(("month", "sales"), ((3, 3), (4, 4))), MONTH_Q) == ()
    fiscal = "What was the average (mean) price paid in each quarter of FY2023?"
    stated = _quarters(("2023 Q1", "2023 Q2", "2023 Q3", "2023 Q4"))
    assert rescue(stated, Q035_GOLD, fiscal, tol=Q035_TOL) == ()
    fiscal_dates = rs(("month", "sales"), (("2016-03-01", 70), ("2016-04-01", 80)))
    assert rescue(fiscal_dates, MONTHS, MONTH_Q + " (fiscal months)") == ()


def test_evalkit_stricter_empty_and_pivot_rules():
    """agentic-analytics D-91 / 5a203a7: a zero in a column that leads with a period, id,
    rank or total word is not a zero count; ``sales_in_requested_year`` is one."""
    empty = rs(("month", "median_price"), ())
    text = "What was the median price in each month of 1993?"
    for col in ("grand_total", "year", "q"):
        assert rescue(rs(("earliest", col), (("1995-01-01", 0),)), empty, text) == (), col
    ok = rs(("earliest_sale_date", "sales_in_requested_year"), (("1995-01-01", 0),))
    assert rescue(ok, empty, text) == ("empty_check",)
    gold = rs(("duration", "sales"), (("freehold", 5), ("leasehold", 3)))
    tenure = rs(("freehold", "leasehold", "n"), ((5, 3, 0),))
    assert rescue(tenure, gold, "Sales by tenure?") == ()
    years = rs(("year", "sales"), (("2022", 5), ("2023", 3)))
    pred = rs(("2022", "2023", "unknown"), ((5, 3, 0),))
    assert rescue(pred, years, "Sales by year?") == ()


@pytest.mark.parametrize(
    "name",
    [
        "total_sales",
        "totalSales",
        "TOTAL_ORDERS",
        "grand_total_count",
        "year_count",
        "q1_sales",
        "quarter_sales",
        "month_count",
        "day_count",
        "id_count",
        "rank_n",
    ],
)
def test_empty_check_refuses_a_count_column_with_a_leading_period_or_total_word(name):
    assert rescue(rs(("earliest", name), (("1995-01-01", 0),)), EMPTY, EMPTY_Q) == ()


@pytest.mark.parametrize(
    "name", ["sales_in_requested_year", "sales_in_1994", "n_sales_per_month", "count_total"]
)
def test_empty_check_keeps_a_count_column_whose_period_or_total_word_comes_later(name):
    pred = rs(("earliest", name), (("1995-01-01", 0),))
    assert rescue(pred, EMPTY, EMPTY_Q) == ("empty_check",)


def test_pivot_extra_label_beside_digit_labels_must_start_with_a_digit():
    """agentic-analytics 5a203a7: ``unknown`` is no omitted label beside date-like labels,
    even when those labels hold letters (``T``, ``Z``, ``Q``) as well as digits."""
    for labels in (("2023 Q1", "2023 Q2"), ("2023-03-01T00:00:00Z", "2023-04-01T00:00:00Z")):
        gold = rs(("period", "n"), ((labels[0], 4), (labels[1], 5)))
        for extra in ("unknown", "Unknown", "other", "n_a"):
            pred = rs((*labels, extra), ((4, 5, 0),))
            assert rescue(pred, gold, "Sales by period?") == (), (labels, extra)
    gold = rs(("period", "n"), (("2023 Q1", 4), ("2023 Q2", 5)))
    pred = rs(("2023 Q1", "2023 Q2", "2023 Q3"), ((4, 5, 0),))
    assert rescue(pred, gold, "Sales by period?") == ("pivot",)
