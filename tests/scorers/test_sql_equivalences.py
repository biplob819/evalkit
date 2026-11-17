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


def test_fraction_needs_a_percent_unit_tolerance_on_a_percent_named_column():
    pred = rs(("new_build_pct",), ((0.113892,),))
    assert rescue(pred, PCT_GOLD, PCT_TEXT, tol=(0.005, 0.0001)) == ()
    assert rescue(pred, PCT_GOLD, PCT_TEXT, tol=EXACT) == ()
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
    """agentic-analytics compares the count columns exactly; sql_match has one tolerance for
    every column, so this uses the percent-unit abs alone (no relative slack on counts)."""
    case = next(c for c in CASES if c["question_id"] == "q-085")
    pred, gold, text, _ = case_args(case)
    tol = (0.0, 0.01)
    assert rescue(pred, gold, text, tol=tol) == ("percent_scale",)
    rows = [list(r) for r in case["predicted"]["rows"]]
    rows[0][2] += 1  # one sales count off by one
    assert rescue(rs(case["predicted"]["columns"], rows), gold, text, tol=tol) == ()


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
