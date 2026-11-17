"""B5.13 agent and decision scorers: answer from rows (AFR-01), clarification (CLR-01), rule
compliance (RUL-01) and structured evidence (SEV-01). The asking projects' cases are the tests
(D-61 rule 3): agentic-analytics' number fixtures and question bank, fraud-review-bedrock's
policy and router cases."""

from __future__ import annotations

import ast
import asyncio
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

import pytest

from evalkit.schemas import Answer, Case, Completion, ResultSet
from evalkit.scorers import (
    ClarificationRates,
    Rule,
    RuleError,
    RuleSet,
    ScoreResult,
    answer_from_rows,
    clarification_rates,
    get_scorer,
    render_evidence,
    rule_compliance,
    rule_compliance_scorer,
    unsupported_numbers,
)
from evalkit.scorers import base as scorer_base

# D-74: value_check is not public in 0.3.0; it is tested from its internal module until B5.16.
from evalkit.scorers.evidence import (
    ValuePhrase,
    value_check,
    value_check_scorer,
)
from evalkit.scorers.rules import decision_fields

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "agent_scorers"
SRC = Path(__file__).resolve().parents[2] / "src" / "evalkit" / "scorers"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


AFR = load("answer_from_rows.json")
CLR = load("clarification.json")
POL = load("fraud_policy.json")
SEV = load("fraud_evidence.json")

ROWS = ResultSet(columns=tuple(AFR["rows"]["columns"]), rows=tuple(map(tuple, AFR["rows"]["rows"])))
Q = AFR["question"]
YEARS = tuple(AFR["year_range"])
RULES = RuleSet.model_validate(POL["rule_set"])
BASE = POL["base_record"]


def case(meta: dict[str, Any] | None = None, *, question: str = Q, label: str | None = None):
    return Case(
        id="c1",
        question=question,
        context=("ctx",),
        reference_label=label,
        source="test",
        meta=meta or {},
    )


def completion(label: str = "answer", text: str = "", *, error: str | None = None) -> Completion:
    return Completion(
        model="m",
        model_snapshot="",
        params={},
        raw_text=text,
        answer=None if error else Answer(label=label, answer=text, citations=()),
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        cost_usd=0.0,
        error=error,  # type: ignore[arg-type]
    )


@pytest.fixture
def registry(monkeypatch):
    """Factory scorers register names; keep each test's registrations to itself."""
    monkeypatch.setattr(scorer_base, "_REGISTRY", dict(scorer_base._REGISTRY))
    monkeypatch.setattr(scorer_base, "_BUILTIN", dict(scorer_base._BUILTIN))


# ================================ AFR-01 answer from rows ================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [(c["answer"], tuple(c["unsupported"])) for c in AFR["cases"]],
    ids=[c["answer"][:40] for c in AFR["cases"]],
)
def test_afr_agentic_analytics_fixtures(text, expected):
    """agentic-analytics' fixture table, vendored (golden)."""
    assert unsupported_numbers(text, ROWS, Q, year_range=YEARS) == expected


def test_afr_fixture_count_covers_the_asking_project():
    assert len(AFR["cases"]) >= 70


def test_afr_planted_number_fails_and_derivations_pass():
    clean = "Kensington £1.2m, Manchester £190k, Leeds £254k; 812 sales in Kensington."
    assert unsupported_numbers(clean, ROWS, Q) == ()
    assert unsupported_numbers(clean.replace("812", "912"), ROWS, Q) == ("912",)
    # percent and fraction forms by column name
    assert unsupported_numbers("Leeds took 12.5% of sales", ROWS, Q) == ()
    # a multiple derivable from two cells of one column: 1,249,000 / 190,312 = 6.56
    assert unsupported_numbers("Kensington is 6.6x Manchester", ROWS, Q) == ()
    assert unsupported_numbers("Kensington is 10x Manchester", ROWS, Q) == ("10x",)
    # thousands separators and dates in words
    assert (
        unsupported_numbers(
            "On 31 December 2024 Leeds sold 4,100 homes.", ROWS, Q, year_range=YEARS
        )
        == ()
    )
    res = answer_from_rows("Leeds sold 4,200 homes.", ROWS, Q)
    assert res.value == 0.0 and res.details["unsupported"] == ("4,200",)
    assert res.details["n_numbers"] == 1


def test_afr_years_are_claims_unless_a_range_or_the_data_backs_them():
    assert unsupported_numbers("In 2024 Leeds sold 4100.", ROWS, "Leeds sales?") == ("2024",)
    assert unsupported_numbers("In 2024 Leeds sold 4100.", ROWS, Q) == ()  # in the question
    dated = ResultSet(columns=("day", "n"), rows=(("2024-03-31", 7),))
    assert unsupported_numbers("In March 2024 there were 7.", dated, "How many?") == ()


def test_afr_top_n_question_numbers_and_row_count():
    text = "The top 10 districts by median price are listed."
    assert unsupported_numbers(text, ROWS, "Most expensive?") == ("10",)
    assert unsupported_numbers(text, ROWS, "Most expensive?", top_n=10) == ()
    q = "Which districts had more than 500 sales and a median over £1m?"
    assert (
        unsupported_numbers("More than 500 sales and over £1,000,000: Kensington.", ROWS, q) == ()
    )
    assert unsupported_numbers("All 3 districts are listed.", ROWS, Q) == ()
    assert unsupported_numbers("Manchester: £190,312.", None, Q) == ("£190,312",)
    assert unsupported_numbers("There were 0 sales.", None, Q) == ()


TEXTY = ResultSet(columns=("paon", "street", "n"), rows=(("FLAT 12", "10 DOWNING STREET", 3),))


@pytest.mark.parametrize(
    ("text", "rs", "expected"),
    [
        ("PAON: FLAT 12, N: 3", ResultSet(columns=("paon", "n"), rows=(("FLAT 12", 3),)), ()),
        ("Sold at 10 DOWNING STREET for 3.", TEXTY, ()),
        ("SALES 5000: 7", ResultSet(columns=("SALES 5000",), rows=((7,),)), ()),
        ("Flat 12 at 10 Downing Street, 3 sales, 45 rooms.", TEXTY, ("45",)),
        ("FLAT 13", TEXTY, ("13",)),
    ],
    ids=["paon", "address", "column-name", "planted", "near-miss"],
)
def test_afr_numbers_in_text_cells_and_column_names(text, rs, expected):
    assert unsupported_numbers(text, rs, Q) == expected


def test_afr_percent_matches_fractions_only_in_ratio_columns():
    unnamed = ResultSet(columns=("district", "v"), rows=(("LEEDS", 0.125),))
    assert unsupported_numbers("Leeds rose 12.5%", unnamed, Q) == ("12.5%",)
    rate = ResultSet(columns=("district", "growth_rate"), rows=(("LEEDS", 0.125),))
    assert unsupported_numbers("Leeds rose 12.5%", rate, Q) == ()
    assert unsupported_numbers("Leeds rose 0.125%", rate, Q) == ("0.125%",)
    pct = ResultSet(columns=("district", "pct_change"), rows=(("LEEDS", 0.5),))
    assert unsupported_numbers("Leeds rose 0.5%", pct, Q) == ()
    assert unsupported_numbers("Leeds rose 50%", pct, Q) == ()


def test_afr_boolean_and_numeric_string_cells():
    flags = ResultSet(columns=("flag", "n"), rows=((True, 7), (True, 7)))
    assert unsupported_numbers("1 flag", flags, Q) == ("1",)
    strings = ResultSet(columns=("median",), rows=(("254312.50",), ("190,312",)))
    assert unsupported_numbers("£254k and £190k", strings, Q) == ()


def test_afr_task_scorer_runs_from_case_meta():
    s = get_scorer("answer_from_rows")
    meta = {"result": AFR["rows"], "year_range": list(YEARS)}
    good = s(case(meta), completion(text="Leeds was £254k in 2024."), model="m")
    assert isinstance(good, ScoreResult) and good.value == 1.0
    bad = s(case(meta), completion(text="Leeds was £260k."))
    assert bad.value == 0.0 and bad.details["unsupported"] == ("£260k",)
    top = s(case({"result": AFR["rows"], "top_n": 10}), completion(text="Top 10 shown."))
    assert top.value == 1.0
    none = s(case(), completion(text="Leeds was £254k."))
    assert none.value == 0.0  # no rows: nothing backs the number
    failed = s(case(meta), completion(error="schema"))
    assert failed.value == 0.0 and failed.details["reason"] == "schema"
    assert s.name == "answer_from_rows" and s.version == "1"


def test_afr_is_deterministic_and_row_order_free():
    text = "Kensington £1.2m, Leeds 4,100 sales, Manchester -3.2%, and 999 others, £260k."
    first = unsupported_numbers(text, ROWS, Q)
    shuffled = list(ROWS.rows)
    random.Random(7).shuffle(shuffled)
    again = ResultSet(columns=ROWS.columns, rows=tuple(reversed(shuffled)))
    assert first == unsupported_numbers(text, again, Q) == ("999", "£260k")
    assert answer_from_rows(text, ROWS, Q) == answer_from_rows(text, again, Q)


@pytest.mark.safety
def test_afr_large_and_hostile_inputs_are_fast():
    big = ResultSet(columns=("v",), rows=tuple((i * 1.5,) for i in range(10_000)))
    text = " ".join(f"{i * 1.5:.1f}" for i in range(0, 10_000, 50)) + " 99999999"
    t0 = time.perf_counter()
    assert unsupported_numbers(text, big, Q) == ("99999999",)
    hostile = "1," * 5_000 + "." * 5_000 + "9" * 5_000 + " ```" + "x" * 5_000
    unsupported_numbers(hostile, ROWS, Q)
    assert time.perf_counter() - t0 < 10
    with pytest.raises(ValueError):
        unsupported_numbers("1", ROWS, Q, rel_tol=1.5)


# ================================ CLR-01 clarification ===================================


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def test_clr_twenty_ambiguous_questions():
    """agentic-analytics' 20 ambiguous questions and 20 clear ones, scripted behaviour (golden)."""
    qs = CLR["questions"]
    assert sum(q["expected"] == "clarify" for q in qs) == 20
    rates = clarification_rates([q["expected"] == "clarify" for q in qs], [q["asked"] for q in qs])
    assert isinstance(rates, ClarificationRates)
    assert (rates.n_ambiguous, rates.asked, rates.n_clear, rates.false_clarifications) == (
        20,
        17,
        20,
        2,
    )
    assert rates.ask_rate is not None and rates.false_clarification_rate is not None
    assert rates.ask_rate.point == pytest.approx(0.85)
    assert (rates.ask_rate.lo, rates.ask_rate.hi) == pytest.approx((0.6211, 0.9679), abs=1e-4)
    f = rates.false_clarification_rate
    assert (f.point, f.lo, f.hi) == pytest.approx((0.10, 0.01235, 0.3170), abs=1e-4)
    assert rates.method == "clopper-pearson"


def test_clr_rates_count_each_side_separately():
    expected = [True] * 5 + [False] * 5
    always = clarification_rates(expected, [True] * 10)
    assert always.ask_rate and always.ask_rate.point == 1.0
    assert always.false_clarification_rate and always.false_clarification_rate.point == 1.0
    never = clarification_rates(expected, [False] * 10)
    assert never.ask_rate and never.ask_rate.point == 0.0
    assert never.false_clarification_rate and never.false_clarification_rate.point == 0.0
    only_clear = clarification_rates([False, False], [False, True])
    assert only_clear.ask_rate is None and only_clear.n_ambiguous == 0


def test_clr_exact_intervals_reproduce_the_binomial_tails():
    """Clopper-Pearson bounds: closed forms at 0 and n, tail areas of alpha/2 elsewhere."""
    zero = clarification_rates([False] * 30, [False] * 30).false_clarification_rate
    assert zero and zero.lo == 0.0 and zero.hi == pytest.approx(1 - 0.025 ** (1 / 30), abs=1e-9)
    full = clarification_rates([True] * 12, [True] * 12).ask_rate
    assert full and full.hi == 1.0 and full.lo == pytest.approx(0.025 ** (1 / 12), abs=1e-9)
    for k, n in [(1, 6), (17, 20), (3, 50), (250, 1000)]:
        r = clarification_rates([True] * n, [True] * k + [False] * (n - k)).ask_rate
        assert r is not None
        assert 1 - _binom_cdf(k - 1, n, r.lo) == pytest.approx(0.025, abs=1e-7)
        assert _binom_cdf(k, n, r.hi) == pytest.approx(0.025, abs=1e-7)
    a = clarification_rates([True] * 9, [True] * 4 + [False] * 5, alpha=0.1).ask_rate
    b = clarification_rates([True] * 9, [True] * 4 + [False] * 5).ask_rate
    assert a and b and b.lo < a.lo < a.hi < b.hi


def test_clr_smoke_run_reproduces():
    """agentic-analytics' recorded smoke run: one ask-back on six ambiguous answers (repro)."""
    run = CLR["smoke_run"]
    rates = clarification_rates(
        [r["expected"] == "clarify" for r in run], [r["asked"] for r in run]
    )
    assert (rates.asked, rates.n_ambiguous, rates.false_clarifications, rates.n_clear) == (
        1,
        6,
        0,
        30,
    )
    assert rates.ask_rate and (rates.ask_rate.lo, rates.ask_rate.hi) == pytest.approx(
        (0.00421, 0.64123), abs=1e-4
    )


def test_clr_asked_back_scorer_and_slices():
    s = get_scorer("asked_back")
    asked = s(case({"expected": "clarify"}), completion(label="Clarify"), model="m")
    assert asked.value == 1.0 and asked.details["false_clarification"] is False
    false = s(case({"expected": "answer"}), completion(label="clarify"))
    assert false.value == 1.0 and false.details["false_clarification"] is True
    answered = s(case({"expected": "clarify"}), completion(label="answer"))
    assert answered.value == 0.0 and answered.details["expected"] == "clarify"
    by_label = s(case(label="clarify"), completion(label="answer"))
    assert by_label.details["expected"] == "clarify"
    custom = s(case({"clarify_label": "ask", "expected": "clarify"}), completion(label="ASK"))
    assert custom.value == 1.0
    failed = s(case({"expected": "clarify"}), completion(error="transport"))
    assert failed.value == 0.0 and failed.details["asked"] is False


@pytest.mark.safety
def test_clr_refuses_misaligned_or_invalid_inputs():
    with pytest.raises(ValueError):
        clarification_rates([True, False], [True])
    with pytest.raises(ValueError):
        clarification_rates([True], [True], alpha=0.0)
    with pytest.raises(ValueError):
        clarification_rates([True], [1])  # type: ignore[list-item]
    s = get_scorer("asked_back")
    weird = s(case({"expected": "maybe"}), completion(label="clarify"))
    assert weird.details["expected"] is None and weird.details["false_clarification"] is False


# ================================ RUL-01 rule compliance =================================


@pytest.mark.parametrize(
    "row",
    POL["clearing_cases"],
    ids=[f"{r['cited']}-{r['changes']}" for r in POL["clearing_cases"]],
)
def test_rul_fraud_review_clearing_table(row):
    """fraud-review-bedrock's clearing_allowed and router cases (logical)."""
    decision = {"action": "clear", "policy_clauses": row["cited"]}
    res = rule_compliance(
        decision,
        {**BASE, **row["changes"]},
        RULES,
        cited_key="policy_clauses",
        allow='action == "clear"',
    )
    assert (res.value == 1.0) is row["ok"]
    assert list(res.details["reasons"]) == row["reasons"]


def test_rul_non_allow_decisions_check_only_ids():
    hold = {"action": "hold", "policy_clauses": ["P3.1", "P4.2"]}
    risky = {**BASE, "amt_vs_usual": 12.0, "merchant_first_seen": True}
    kw: dict[str, Any] = {"cited_key": "policy_clauses", "allow": 'action == "clear"'}
    ok = rule_compliance(hold, risky, RULES, **kw)
    assert ok.value == 1.0 and ok.details["allow"] is False
    bad = rule_compliance({**hold, "policy_clauses": ["P99.9"]}, risky, RULES, **kw)
    assert list(bad.details["reasons"]) == ["unknown_rule:P99.9"]
    # allow=None treats every decision as an allow
    assert rule_compliance({"rule_ids": ["P7.1"]}, BASE, RULES).value == 1.0
    assert rule_compliance({"rule_ids": []}, BASE, RULES).details["reasons"] == (
        "no_allow_rule_cited",
    )


def test_rul_per_rule_details_and_fail_safe_missing_values():
    res = rule_compliance({"rule_ids": ["P7.1"]}, {**BASE, "amt_vs_usual": None}, RULES)
    rules = res.details["rules"]
    assert rules["P7.1"]["holds"] is False and rules["P7.1"]["missing"] == ("amt_vs_usual",)
    assert rules["P3.9"]["kind"] == "never" and rules["P3.9"]["holds"] is True
    assert RULES.holds("P3.9", {**BASE, "amt_vs_usual": float("nan")}) is True
    assert RULES.holds("P3.1", {**BASE, "amt_vs_usual": None}) is False
    assert RULES.holds("P7.3", BASE) is True
    with pytest.raises(KeyError):
        RULES.holds("P0.0", BASE)


def test_rul_unreadable_decisions_are_checked_as_allows():
    kw: dict[str, Any] = {"cited_key": "policy_clauses", "allow": 'action == "clear"'}
    no_action = rule_compliance({"policy_clauses": ["P3.1"]}, BASE, RULES, **kw)
    assert no_action.details["allow"] is True and no_action.value == 0.0
    wrong_type = rule_compliance({"action": 3, "policy_clauses": ["P7.1"]}, BASE, RULES, **kw)
    assert wrong_type.details["allow"] is True and wrong_type.value == 1.0
    bad = rule_compliance({"action": "clear", "policy_clauses": "P7.1"}, BASE, RULES, **kw)
    assert "bad_citations" in bad.details["reasons"] and bad.value == 0.0


@pytest.mark.safety
def test_rul_policy_v1_never_clears_extreme_spend_high_risk_or_thin_history():
    allow_ids = [r.id for r in RULES.rules if r.kind == "allow"]
    for bad in ({"amt_vs_usual": 12.0}, {"risk_score": 0.95}, {"card_history_n": 2}):
        res = rule_compliance({"rule_ids": allow_ids}, {**BASE, **bad}, RULES)
        assert res.value == 0.0 and any(r.startswith("never:") for r in res.details["reasons"])


def test_rul_task_scorer_reads_cited_key_and_allow_predicate(registry):
    s = rule_compliance_scorer(
        RULES, name="fraud_policy", cited_key="policy_clauses", allow='label == "clear"'
    )
    assert get_scorer("fraud_policy") is s and s.version == "1"
    assert "fraud_policy" not in scorer_base._BUILTIN
    decision = json.dumps({"policy_clauses": ["P7.1"], "reasons": ["Routine purchase"]})
    ok = s(case({"record": BASE}), completion(label="clear", text=decision), model="m")
    assert ok.value == 1.0 and ok.details["cited"] == ("P7.1",)
    planted = s(
        case({"record": {**BASE, "amount": 500.0}}),
        completion("clear", decision.replace("P7.1", "P7.2")),
    )
    assert planted.value == 0.0 and "predicate_false:P7.2" in planted.details["reasons"]
    passed = s(case({"record": {**BASE, "risk_score": 0.95}}), completion("pass", decision))
    assert passed.value == 1.0  # not a clear: only ids are checked
    failed = s(case({"record": BASE}), completion(error="schema"))
    assert failed.value == 0.0 and failed.details["reason"] == "schema"
    with pytest.raises(ValueError):
        rule_compliance_scorer(RULES, name="fraud_policy")  # names are unique
    with pytest.raises(RuleError):
        rule_compliance_scorer(RULES, name="other", allow="__import__('os')")


def test_rul_rule_set_round_trips_json_with_identical_verdicts():
    again = RuleSet.model_validate_json(RULES.model_dump_json())
    assert again == RULES
    for row in POL["clearing_cases"]:
        record = {**BASE, **row["changes"]}
        d = {"rule_ids": row["cited"]}
        assert rule_compliance(d, record, again) == rule_compliance(d, record, RULES)


@pytest.mark.parametrize(
    "when",
    [
        "amount < 50",
        "0 < amount <= 50",
        "amount < 50 and not merchant_first_seen",
        "amount >= 500 or merchant_first_seen",
        "category == 'grocery_pos'",
        'category != "grocery_pos"',
        "merchant_first_seen == True",
        "amount < risk_score",
        "amount > -5",
    ],
)
def test_rul_parser_accepts_the_grammar(when):
    RuleSet(rules=(Rule(id="R1", kind="allow", when=when),), fields=POL["rule_set"]["fields"])


MALICIOUS = [
    "__import__('os').system('touch /tmp/pwned')",
    "amount.real > 1",
    "amount.__class__ == 1",
    "amount[0] > 1",
    "amount * 2 > 1",
    "(lambda: True)()",
    "[x for x in amount]",
    "{amount: 1} == 1",
    "amount > None",
    "amount > b'1'",
    "-amount < 0",
    "amount > 1e999",
    "True",
    "amount < 50 and True",
    "1 < 2 and amount < 3",
    "amount < 'grocery_pos'",
    "amount < 50 if amount else amount < 60",
    "category > 'grocery_pos'",
    "merchant_first_seen < True",
    "amount < 50 # and card_history_n >= 10",
    "category == 'grocery_' 'pos'",
    "ａmount < 50",
    "amount <= 0x10",
    "amount < 1_0",
    "amount < 1e2",
    "amount  < 50",
    "amount < 50\nand True",
    "f'{amount}' == 'x'",
    "eval('1') == 1",
    "(" * 200 + "amount < 1" + ")" * 200,
    "amount < 1 and " * 40 + "amount < 1",
    "",
]


@pytest.mark.safety
@pytest.mark.parametrize("when", MALICIOUS, ids=[f"m{i}" for i in range(len(MALICIOUS))])
def test_rul_malicious_predicates_are_refused(when):
    with pytest.raises((RuleError, ValueError)):
        RuleSet(rules=(Rule(id="R1", kind="allow", when=when),), fields=POL["rule_set"]["fields"])
    with pytest.raises(RuleError):
        rule_compliance({"rule_ids": ["P7.1"]}, BASE, RULES, allow=when or " ")


MISTYPED = ["amount", "amount == True", "category == 'kirlin_and_sons'", "unknown_field > 1"]


@pytest.mark.safety
@pytest.mark.parametrize("when", MISTYPED)
def test_rul_mistyped_predicates_are_refused_against_the_fields(when):
    with pytest.raises(ValueError):
        RuleSet(rules=(Rule(id="R1", kind="allow", when=when),), fields=POL["rule_set"]["fields"])


@pytest.mark.safety
def test_rul_and_sev_modules_never_call_eval_or_exec():
    for name in ("rules.py", "evidence.py", "rows.py", "clarify.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
        names = {f.id for f in calls if isinstance(f, ast.Name)}
        attrs = {f.attr for f in calls if isinstance(f, ast.Attribute)}
        assert not names & {"eval", "exec", "compile", "__import__", "getattr"}, name
        assert not attrs & {"eval", "exec", "literal_eval", "import_module", "format_map"}, name


@pytest.mark.safety
def test_rul_rule_sets_refuse_bad_structure():
    fields = POL["rule_set"]["fields"]
    allow = Rule(id="A", kind="allow", when="amount < 5")
    with pytest.raises(ValueError, match="duplicate"):
        RuleSet(rules=(allow, allow), fields=fields)
    with pytest.raises(ValueError, match="allow"):
        RuleSet(rules=(Rule(id="N", kind="never", when="amount > 5"),), fields=fields)
    with pytest.raises(RuleError, match="number"):
        rule_compliance({"rule_ids": ["P7.1"]}, {**BASE, "amount": "30"}, RULES)
    with pytest.raises(RuleError, match="number"):
        rule_compliance({"rule_ids": ["P7.1"]}, {**BASE, "hour": True}, RULES)


# ================================ SEV-01 structured evidence =============================


def test_sev_render_fraud_alert_golden():
    assert render_evidence(SEV["record"], SEV["template"]) == SEV["evidence"]
    known = render_evidence({**SEV["record"], "merchant_first_seen": False}, SEV["template"])
    assert known[3] == "The card has bought at this merchant before."
    missing = render_evidence({**SEV["record"], "amt_vs_usual": None}, SEV["template"])
    assert missing[1] == "amt_vs_usual is not known."
    assert render_evidence({"hour": 3, "first": True, "x": 0.5}) == [
        "hour is 3.",
        "first is true.",
        "x is 0.5.",
    ]
    assert render_evidence({"a": 1}, ["Braces {{a}} stay; a is {a}."]) == [
        "Braces {a} stay; a is 1."
    ]


PHRASES = [ValuePhrase(**p) for p in SEV["phrases"]]
# B5.16 (D-74): the vendored "ok" meant "nothing failed". Under the strict check a reason with
# a number its text does not bind to a field ("5% of the card's purchases", "High risk score of
# 0.83", "3 a.m." with no hour field), a correct but coarse rounding ("9x" on 9.1), two fields
# in one clause ("$812.40 purchase in a rarely used category"), a phrase's number with another
# field named in its clause ("Amount is 15x ... usual", "5 times the usual amount": verify516),
# nothing checkable or a missing field is unverifiable; the other vendored false reasons are
# still contradicted, and none is ever verified.
_SEV_UNVERIFIABLE = {
    ("Spend is more than 5 times the usual amount", ()),
    ("Amount is 15x this card's usual spend", ()),
    ("Spend is under 2 times the usual amount", ()),
    ("Amount is 9x this card's usual spend", ()),
    ("A $812.40 purchase in a rarely used category", ()),
    ("Only 5% of the card's purchases are in this category", ()),
    ("High risk score of 0.83", ()),
    ("Bought at 3 a.m. local time", ()),
    ("Purchase made in the afternoon", ()),
    ("Amount is 9x this card's usual spend", (("amt_vs_usual", None),)),
}


@pytest.mark.parametrize(
    "row",
    SEV["reason_cases"],
    ids=[f"{r['reason'][:30]}-{r['changes']}" for r in SEV["reason_cases"]],
)
def test_sev_value_check_fraud_reasons(row):
    """fraud-review-bedrock's value checks: "9x usual spend", "at night" (logical)."""
    res = value_check(row["reason"], {**SEV["record"], **row["changes"]}, phrases=PHRASES)
    if (row["reason"], tuple(row["changes"].items())) in _SEV_UNVERIFIABLE:
        assert res.value == 0.0 and res.details["outcome"] == "unverifiable", res.details
        assert res.details["failed"] == ()
        return
    assert (res.value == 1.0) is row["ok"], res.details
    assert res.details["outcome"] == ("verified" if row["ok"] else "contradicted"), res.details


def _scripted(req):
    """Extractor: one claim per sentence. Judge: supported when every content word of the
    claim appears in the shown evidence sentences."""

    def words(s: str) -> list[str]:
        return re.sub(r"[^a-z0-9. ]", "", s.casefold()).replace(". ", " ").split()

    if req.model == "extractor":
        text = re.search(r"\nAnswer: (.*)\n", req.prompt).group(1)  # type: ignore[union-attr]
        claims = [
            {"text": s.strip() + ".", "addresses_question": True}
            for s in text.split(". ")
            if s.strip()
        ]
        return json.dumps({"claims": claims})
    claim = req.prompt.split("Claim: ", 1)[1].split("\n", 1)[0]
    shown = req.prompt.split("Cited source sentences:", 1)[1]
    sentences = re.findall(r"^\[(\d+)\] (.+)$", shown, re.M)
    have = {w.rstrip(".") for _, s in sentences for w in words(s)}
    ok = all(w.rstrip(".") in have for w in words(claim) if len(w.rstrip(".")) > 3)
    return json.dumps(
        {
            "verdict": "supported" if ok else "unsupported",
            "evidence": [1] if ok else [],
            "quote": None,
            "rationale": "scripted",
        }
    )


def test_sev_rendered_evidence_feeds_faithfulness(tmp_path):
    """The rendered record is the evidence `faithfulness()` judges a decision against."""
    from evalkit.cost import load_prices
    from evalkit.runtime import Executor, Store
    from evalkit.schemas import RunConfig
    from evalkit.scorers.faithfulness import FaithfulnessItem, faithfulness
    from tests.pipeline.fakes import ScriptedBatchAdapter

    evidence = tuple(render_evidence(SEV["record"], SEV["template"]))
    item = FaithfulnessItem(
        id="a1",
        question="Why was this alert held?",
        explanation=(
            "The amount is 9.1 times the card's usual spend. "
            "The cardholder is travelling abroad this week"
        ),
        evidence=evidence,
    )
    cfg = RunConfig(
        models=("claude-sonnet-5-5",),
        judges=("claude-opus-5-5", "gpt-6.1-sol"),
        extractor="claude-haiku-5-5",
    )
    fake = ScriptedBatchAdapter(_scripted)
    (res,) = asyncio.run(
        faithfulness(
            [item],
            executor=Executor(Store(tmp_path)),
            config=cfg,
            adapters={"anthropic": fake, "openai": fake},
            prices=load_prices(),
        )
    )
    assert res.n_claims == 2
    verdicts = [c.verdicts["claude-opus-5-5"] for c in res.claims]
    assert verdicts == ["supported", "unsupported"]
    assert res.claims[0].cited == tuple(range(1, len(evidence) + 1))


def test_sev_render_and_check_are_deterministic():
    rec = SEV["record"]
    shuffled = dict(reversed(list(rec.items())))
    assert render_evidence(rec, SEV["template"]) == render_evidence(shuffled, SEV["template"])
    reason = "A $812.40 purchase, 9x usual spend, at night, risk 0.83"
    assert value_check(reason, rec, phrases=PHRASES) == value_check(
        reason, shuffled, phrases=PHRASES
    )
    again = [ValuePhrase.model_validate_json(p.model_dump_json()) for p in PHRASES]
    assert value_check(reason, rec, phrases=again) == value_check(reason, rec, phrases=PHRASES)


@pytest.mark.safety
@pytest.mark.parametrize(
    "template",
    [
        ["{amount.__class__}"],
        ["{amount[0]}"],
        ["{amount!r}"],
        ["{__import__('os')}"],
        ["{amount:{hour}}"],
        ["{missing_field} here"],
        ["{"],
        ["}"],
        ["   "],
    ],
)
def test_sev_templates_cannot_reach_beyond_plain_fields(template):
    with pytest.raises(ValueError):
        render_evidence(SEV["record"], template)


@pytest.mark.safety
def test_sev_condition_phrases_use_the_safe_predicate_grammar():
    for holds in ("__import__('os').system('x')", "hour.real < 5", "(lambda: 1)()"):
        with pytest.raises(ValueError):  # refused when the ValuePhrase is built
            value_check(
                "at night", SEV["record"], phrases=[ValuePhrase(pattern="night", holds=holds)]
            )
    # a pathological reason stays fast
    t0 = time.perf_counter()
    value_check("9" * 20_000 + "x usual " * 2_000, SEV["record"], phrases=PHRASES)
    assert time.perf_counter() - t0 < 10


# ================================ inside `evalkit run` ===================================


def test_agent_scorers_run_as_task_scorers_in_the_pipeline(registry):
    """The v0.2 task-scorer path: looked up at plan time, called with the run's context."""
    from evalkit.pipeline.task_scorers import resolve_task_scorers, score_task
    from evalkit.schemas import TaskConfig
    from evalkit.schemas.benchmark import Scores

    rule_compliance_scorer(
        RULES, name="policy_check", cited_key="policy_clauses", allow='label == "clear"'
    )
    value_check_scorer(phrases=PHRASES, name="reason_values")
    task = TaskConfig(
        name="t",
        dataset="x.jsonl",
        split="all",
        labels=("clear", "hold", "clarify", "answer"),
        prompt_template="{question}",
        output_schema="answer_v1",
        scorers=(
            "label_correct",
            "answer_from_rows",
            "asked_back",
            "policy_check",
            "reason_values",
        ),
        slices=("expected",),
    )
    scorers = resolve_task_scorers(task)
    names = [s.name for s in scorers]
    assert names == ["answer_from_rows", "asked_back", "policy_check", "reason_values"]
    meta = {"result": AFR["rows"], "expected": "answer", "record": {**BASE, **SEV["record"]}}
    text = json.dumps({"policy_clauses": ["P3.1"], "reasons": ["Purchase at night"]})
    scores = Scores(
        label_correct=True,
        citation_valid=1.0,
        groundedness={},
        fully_grounded={},
        coverage=1.0,
        correct_and_grounded={},
    )
    out = score_task(
        scorers,
        case(meta),
        completion("hold", text),
        model="m",
        claims=(),
        verdicts=(),
        scores=scores,
    )
    assert {k: v.value for k, v in out.items()} == {
        "answer_from_rows": 1.0,  # "P3.1" is a code, not a number claim
        "asked_back": 0.0,
        "policy_check": 1.0,  # a hold citing a real signal
        "reason_values": 1.0,  # hour 3 is at night
    }


# ================================ verifier findings (B5.13 fixes) ========================


def test_rul_structured_fields_win_over_answer_json(registry):
    """A clear completion whose answer JSON says hold is still checked as a clear."""
    s = rule_compliance_scorer(
        RULES, name="fp_struct", cited_key="policy_clauses", allow='label == "clear"'
    )
    risky = case({"record": {**BASE, "risk_score": 0.95}})
    lying = json.dumps({"label": "hold", "policy_clauses": ["P7.1"]})
    res = s(risky, completion("clear", lying))
    assert res.details["allow"] is True and res.value == 0.0
    assert "never:P8.9" in res.details["reasons"]
    # the answer text cannot replace the structured label, answer or citations
    fields = decision_fields(completion("clear", lying))
    assert fields["label"] == "clear" and fields["answer"] == lying
    assert fields["policy_clauses"] == ["P7.1"]


def test_rul_either_source_saying_allow_is_checked(registry):
    """Fail closed: a hold whose answer JSON says clear is checked against the never rules."""
    s = rule_compliance_scorer(
        RULES, name="fp_either", cited_key="policy_clauses", allow='label == "clear"'
    )
    risky = case({"record": {**BASE, "risk_score": 0.95}})
    res = s(risky, completion("hold", json.dumps({"label": "clear", "policy_clauses": ["P7.1"]})))
    assert res.details["allow"] is True and res.value == 0.0
    assert "never:P8.9" in res.details["reasons"]
    honest = s(risky, completion("hold", json.dumps({"policy_clauses": ["P3.1"]})))
    assert honest.details["allow"] is False and honest.value == 1.0


@pytest.mark.safety
@pytest.mark.parametrize(
    "text",
    ["a " * 2_000, "one " * 2_000, "one hundred and " * 1_000, "half " * 2_000, "and " * 5_000],
    ids=["a", "one", "hundred-and", "half", "and"],
)
def test_afr_number_word_runs_are_linear(text):
    t0 = time.perf_counter()
    unsupported_numbers(text, None, "")
    assert time.perf_counter() - t0 < 3.0  # linear-time guard; slow CI runners take ~0.8 s


@pytest.mark.safety
def test_afr_long_hundred_chains_do_not_crash():
    for k in (50, 160, 400):
        unsupported_numbers("one " + "hundred " * k + "million", None, "")


def test_afr_multiple_needs_the_named_rows_ratio():
    """The reference flags "2x" (true ratio 6.56); no unrelated cell pair may back it."""
    assert unsupported_numbers("Kensington is 2x Manchester", ROWS, Q) == ("2x",)
    assert unsupported_numbers("Kensington is 6.6x Manchester", ROWS, Q) == ()
    assert unsupported_numbers("Kensington is 7x Manchester", ROWS, Q) == ()  # 6.56 rounds to 7
    assert unsupported_numbers("Leeds is 1.3x Manchester", ROWS, Q) == ()  # 254,312 / 190,312
    # direction and a named column count
    assert unsupported_numbers("Manchester's median price is 6.6x Kensington's", ROWS, Q) == (
        "6.6x",
    )
    # without two named rows nothing is derived (1.6 is a share ratio of unnamed rows)
    assert unsupported_numbers("Prices rose 1.6x", ROWS, Q) == ("1.6x",)
    for k in (2, 5, 8, 9, 10, 11):  # 3x and 4x match the cells 3.2 and 4.0 directly
        assert unsupported_numbers(f"Kensington is {k}x Manchester", ROWS, Q) == (f"{k}x",)


@pytest.mark.safety
@pytest.mark.parametrize(
    "decision",
    [
        {"a": {}, "b": {}},
        {"a": [1], "b": [2]},
        {"a": "x", "b": "y"},
        {"a": True, "b": False},
        {"a": {"k": 1}, "b": {"k": 2}},
    ],
    ids=["dicts", "lists", "strings", "bools", "nested"],
)
def test_rul_untyped_allow_type_errors_fail_the_decision(decision, registry):
    res = rule_compliance({**decision, "rule_ids": ["P7.1"]}, BASE, RULES, allow="a < b")
    assert res.value == 0.0 and "type_error:allow" in res.details["reasons"]
    assert res.details["allow"] is True  # still checked, fail safe
    s = rule_compliance_scorer(RULES, name="typed_allow", allow="label < answer")
    out = s(case({"record": BASE}), completion("clear", "zzz"))
    assert out.value == 0.0 and "type_error:allow" in out.details["reasons"]


def test_afr_written_signs_are_honoured():
    assert unsupported_numbers("Leeds -4%", ROWS, Q) == ("-4%",)  # the cell is +4.0
    assert unsupported_numbers("Manchester -3.2%", ROWS, Q) == ()
    assert unsupported_numbers("Manchester fell 3.2%", ROWS, Q) == ()  # unsigned: either sign
    assert unsupported_numbers("Kensington -12.5%", ROWS, Q) == ("-12.5%",)


def test_rul_allow_string_match_ignores_case():
    kw: dict[str, Any] = {"cited_key": "policy_clauses", "allow": 'action == "clear"'}
    risky = {**BASE, "risk_score": 0.95}
    res = rule_compliance({"action": "Clear", "policy_clauses": ["P7.1"]}, risky, RULES, **kw)
    assert res.details["allow"] is True and "never:P8.9" in res.details["reasons"]
    hold = rule_compliance({"action": " HOLD ", "policy_clauses": ["P3.1"]}, risky, RULES, **kw)
    assert hold.details["allow"] is False and hold.value == 1.0


@pytest.mark.safety
def test_rul_category_values_outside_the_set_are_refused():
    with pytest.raises(RuleError, match="category"):
        rule_compliance({"rule_ids": ["P7.1"]}, {**BASE, "category": "bogus"}, RULES)
    grocery = RuleSet(
        rules=(Rule(id="G", kind="allow", when="category == 'grocery_pos'"),),
        fields=POL["rule_set"]["fields"],
    )
    with pytest.raises(RuleError, match="category"):
        grocery.holds("G", {**BASE, "category": "Grocery_POS"})


def test_sev_conditional_text_may_hold_colons():
    t = ["{flag?yes: really|no: not}"]
    assert render_evidence({"flag": True}, t) == ["yes: really"]
    assert render_evidence({"flag": False}, t) == ["no: not"]
    assert render_evidence({"x": 1.5}, ["{x:.2f}"]) == ["1.50"]
    with pytest.raises(ValueError):
        render_evidence({"x": 1.5}, ["{x:.2f?a|b}"])


# ---- B5.13 verifier pass 2 ---------------------------------------------------------------------
