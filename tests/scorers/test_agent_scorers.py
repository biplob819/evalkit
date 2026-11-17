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
    value_check_shares,
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
# D-72: the vendored "ok" meant "nothing failed". Two of those reasons hold nothing checkable
# (an unnamed time, no number) and one fails on a missing value: all three are unverifiable.
_SEV_UNVERIFIABLE = {
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


def test_sev_value_check_details_and_tolerance():
    rec = SEV["record"]
    res = value_check("Amount is 9x this card's usual spend, at night", rec, phrases=PHRASES)
    checks = res.details["checks"]
    assert res.details["n_checked"] == 2 and res.details["failed"] == ()
    assert checks[0]["field"] == "amt_vs_usual" and checks[0]["found"] == 9.1
    assert checks[1]["holds"] == "hour < 5" and checks[1]["ok"] is True
    # loose numbers: 8 is 12% off 9.1, outside the default 10% tolerance but inside 15%
    assert value_check("about 8 times", rec).value == 0.0
    assert value_check("about 8 times", rec, tolerance=0.15).value == 1.0
    nothing = value_check("Looks routine", rec)
    # D-72: nothing checkable is unverifiable, never verified, and never a contradiction
    assert nothing.value == 0.0 and nothing.details["n_checked"] == 0
    assert nothing.details["outcome"] == "unverifiable" and nothing.details["failed"] == ()
    with pytest.raises(ValueError):
        value_check("x", rec, tolerance=-1)
    with pytest.raises(ValueError):
        ValuePhrase(pattern="x", field="a", holds="a > 1")
    with pytest.raises(ValueError):
        ValuePhrase(pattern="x")


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


def test_sev_three_outcomes_and_their_shares(registry):
    """D-72: verified, contradicted, unverifiable per reason; a contradiction wins over an
    ambiguous check in the same reason, and the case is contradicted when any reason is."""
    rec = {"amount": 812.4, "risk_score": 0.83}
    both = value_check("Amount above $900; no doubt the risk score is above 0.5", rec)
    assert both.details["outcome"] == "contradicted" and both.value == 0.0
    assert [c["status"] for c in both.details["checks"]] == ["failed", "ambiguous"]
    assert both.details["failed"] == ("$900",) and both.details["unverified"] == ("0.5",)
    s = value_check_scorer(name="three_outcomes", record_key="alert")
    texts = ["Amount above $800", "no doubt above $800", "Amount above $900", "Routine"]
    res = s(case({"alert": rec}), completion("hold", json.dumps({"reasons": texts})))
    assert res.value == 0.0 and res.details["outcome"] == "contradicted"
    assert res.details["counts"] == {"verified": 1, "contradicted": 1, "unverifiable": 2}
    assert res.details["shares"] == {"verified": 0.25, "contradicted": 0.25, "unverifiable": 0.5}
    assert res.details["n_reasons"] == 4
    assert [r["n_checked"] for r in res.details["reasons"]] == [1, 1, 1, 0]
    unsure = s(case({"alert": rec}), completion("hold", json.dumps({"reasons": texts[:2]})))
    assert unsure.value == 0.0 and unsure.details["outcome"] == "unverifiable"
    sure = s(case({"alert": rec}), completion("hold", json.dumps({"reasons": texts[:1]})))
    assert sure.value == 1.0 and sure.details["outcome"] == "verified"
    empty = s(case({"alert": rec}), completion("hold", json.dumps({"reasons": []})))
    assert empty.details["outcome"] == "unverifiable" and empty.details["shares"] is None
    refused = s(case({"alert": rec}), completion(error="refusal"))
    one = value_check("Amount above $900", rec)
    shares = value_check_shares([res, unsure, sure, empty, refused, one])
    assert (shares.n_reasons, shares.verified, shares.contradicted, shares.unverifiable) == (
        8,
        3,
        2,
        3,
    )
    assert shares.nothing_checked == 1 and shares.n_skipped == 1
    assert shares.contradicted_share is not None
    assert shares.contradicted_share.point == 0.25
    assert shares.contradicted_share.lo < 0.25 < shares.contradicted_share.hi
    none = value_check_shares([refused])
    assert none.n_reasons == 0 and none.verified_share is None
    with pytest.raises(ValueError):
        value_check_shares([], alpha=1.0)
    with pytest.raises(ValueError):
        value_check_shares([ScoreResult(value=0.0, details={"outcome": "maybe", "n_checked": 1})])


def test_sev_value_check_scorer_checks_each_reason(registry):
    s = value_check_scorer(phrases=PHRASES, name="alert_values", record_key="alert")
    assert get_scorer("alert_values") is s
    good = json.dumps({"reasons": ["Amount is 9x this card's usual spend", "Purchase at night"]})
    res = s(case({"alert": SEV["record"]}), completion("hold", good), model="m")
    assert res.value == 1.0 and res.details["n_checked"] == 2
    bad = json.dumps({"reasons": ["Purchase at night", "Amount is 15x this card's usual spend"]})
    res = s(case({"alert": SEV["record"]}), completion("hold", bad))
    assert res.value == 0.0 and res.details["failed"] == ("15x this card's usual",)
    plain = s(case({"alert": SEV["record"]}), completion("hold", "Purchase at night."))
    assert plain.value == 1.0
    failed = s(case({"alert": SEV["record"]}), completion(error="refusal"))
    assert failed.value == 0.0


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


@pytest.mark.safety
@pytest.mark.parametrize("text", ["$1 " * 5_000, "a " * 2_000], ids=["dollars", "words"])
def test_sev_value_check_long_reasons_are_linear(text):
    t0 = time.perf_counter()
    value_check(text, SEV["record"], phrases=PHRASES)
    assert time.perf_counter() - t0 < 3.0  # linear-time guard; slow CI runners take ~0.8 s


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


def test_sev_phrase_over_absent_field_fails_the_claim():
    rec = {k: v for k, v in SEV["record"].items() if k not in ("hour", "amount")}
    night = value_check("Purchase at night", rec, phrases=PHRASES)
    assert night.value == 0.0
    assert night.details["checks"][0]["reason"] == "missing_value:hour"
    # D-72: the record cannot settle it: unchecked, so unverifiable, never verified
    assert night.details["checks"][0]["status"] == "unchecked"
    assert night.details["outcome"] == "unverifiable"
    dollars = value_check("A $812.40 purchase", rec, phrases=PHRASES)
    assert dollars.value == 0.0
    assert dollars.details["checks"][0]["reason"] == "missing_value:amount"


def test_sev_loose_comparator_numbers_are_comparisons():
    rec = SEV["record"]  # amount 812.4
    assert value_check("amount above 900", rec).value == 0.0
    assert value_check("amount over 1,000", rec).value == 0.0
    assert value_check("amount under 800", rec).value == 0.0
    assert value_check("amount over 800", rec).value == 1.0
    # hour 3, but no hour field named: an unchecked time, unverifiable (D-72), not a failure
    at_least = value_check("at least 3 a.m.", rec)
    assert at_least.value == 0.0 and at_least.details["outcome"] == "unverifiable"
    assert value_check("hour at least 3 a.m.", rec).value == 1.0
    check = value_check("amount above 900", rec).details["checks"][0]
    assert (check["field"], check["op"], check["ok"]) == ("amount", ">", False)


def test_afr_written_signs_are_honoured():
    assert unsupported_numbers("Leeds -4%", ROWS, Q) == ("-4%",)  # the cell is +4.0
    assert unsupported_numbers("Manchester -3.2%", ROWS, Q) == ()
    assert unsupported_numbers("Manchester fell 3.2%", ROWS, Q) == ()  # unsigned: either sign
    assert unsupported_numbers("Kensington -12.5%", ROWS, Q) == ("-12.5%",)


def test_sev_written_signs_are_honoured():
    rec = SEV["record"]
    assert value_check("risk score of -0.83", rec).value == 0.0
    assert value_check("risk fell 0.83", {**rec, "risk_score": -0.83}).value == 1.0
    assert value_check("risk moved -0.83", {**rec, "risk_score": -0.83}).value == 1.0
    assert value_check("risk score above -0.9", {**rec, "risk_score": -0.83}).value == 1.0
    assert value_check("risk score below -0.9", {**rec, "risk_score": -0.83}).value == 0.0
    # verifier pass 6: "risk" alone only partly names risk_score, so a comparison is unclear
    partly = value_check("risk above -0.9", {**rec, "risk_score": -0.83})
    assert partly.details["outcome"] == "unverifiable", partly.details


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


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        ("risk score over 80%", True),
        ("risk score at most 1%", False),
        ("risk score below 85%", True),
        ("risk score above 90%", False),
        ("risk score not above 90%", True),
        ("risk score up to 85%", True),
        ("risk score higher than 80%", True),
        ("risk score lower than 80%", False),
        # pass 6: "risk" after the number is one word of risk_score, not a mention
        ("over 80% risk", False),
    ],
)
def test_sev_percent_comparisons_against_a_fraction_field(reason, ok):
    """A percent compared with a fraction-valued field reads as a fraction (risk 0.83)."""
    from evalkit.scorers.evidence import value_check

    got = value_check(reason, {"risk_score": 0.83})
    assert (got.value == 1.0) is ok, got


def test_sev_wrong_typed_field_fails_the_claim_and_never_raises():
    from evalkit.scorers.evidence import ValuePhrase, value_check

    phrase = ValuePhrase(pattern=r"\$\s*[\d,.]+", field="amount")
    got = value_check("charged $812", {"amount": "812"}, phrases=[phrase])
    assert got.value == 0.0
    assert any(c.get("reason") == "type_error:amount" for c in got.details["checks"])


# ---- B5.13 verifier pass 3 ---------------------------------------------------------------------

_REC = {"amount": 812.4, "card_history_n": 40}


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        ("amount not higher than $800", False),
        ("no greater than $800", False),
        ("never above $800", False),
        ("not lower than $900", False),
        ("no lower than $900", False),
        ("not fewer than 41 transactions", False),
        ("does not exceed $800", False),
        ("doesn't exceed $800", False),
        ("less than or equal to $800", False),
        ("$800 or less", False),
        ("amount $800 or more", True),
        ("amount not above $900", True),
        ("amount does not exceed $900", True),
        ("card history of at least 39", True),
        ("39 transactions or fewer", False),
        # pass 6: no field named (or "transactions" names no field): never verified
        ("$800 or more", False),
        ("not above $900", False),
        ("at least 39 transactions", False),
    ],
)
def test_sev_negated_and_postfix_comparisons(reason, ok):
    from evalkit.scorers.evidence import value_check

    got = value_check(reason, _REC)
    assert (got.value == 1.0) is ok, (reason, got.details)


@pytest.mark.parametrize(
    "record", [{"hour": "3"}, {"hour": True}, {"hour": [3]}, {"hour": {"h": 3}}, {"hour": 10**400}]
)
def test_sev_value_check_never_raises_on_record_contents(record):
    from evalkit.scorers.evidence import ValuePhrase, value_check

    phrase = ValuePhrase(pattern="at night", holds="hour < 5")
    got = value_check("at night", record, phrases=[phrase])
    assert got.value == 0.0
    assert value_check("40 purchases", {"n": 10**400, "k": 40}).value == 1.0


# ---- B5.13 verifier pass 4 ---------------------------------------------------------------------


def test_sev_a_bare_name_over_a_number_fails_the_claim_instead_of_raising():
    """A bare name is valid grammar (a yes/no field); over a number it is a data mismatch."""
    got = value_check(
        "at night", SEV["record"], phrases=[ValuePhrase(pattern="night", holds="hour")]
    )
    assert got.value == 0.0


@pytest.mark.parametrize("value", ["yes", 0, 10**20, [1], {}, None])
def test_sev_valid_predicates_over_odd_values_never_raise(value):
    from decimal import Decimal

    for holds, field in (
        ("merchant_first_seen", "merchant_first_seen"),
        ("category == 'x'", "category"),
    ):
        for v in (value, Decimal("3"), True):
            got = value_check(
                "new merchant", {field: v}, phrases=[ValuePhrase(pattern="new", holds=holds)]
            )
            assert got.value in (0.0, 1.0)


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [
        ("not, in fact, above $800", "unverifiable"),
        ("is not in fact above $800", "unverifiable"),
        ("no purchase above $800", "unverifiable"),
        ("amount cannot exceed $800", "contradicted"),  # directly before: inverted, false
        ("nothing above $800", "unverifiable"),
        ("without exceeding $800", "unverifiable"),
        ("amount isn\u2018t above $800", "contradicted"),
    ],
)
def test_sev_negations_with_words_between_still_invert(reason, outcome):
    """Read as ambiguous (pass 5): never verified, which was the point; D-72 reports an
    ambiguous check as unverifiable, not as a contradiction."""
    got = value_check(reason, {"amount": 812.4})
    assert got.value == 0.0 and got.details["outcome"] == outcome, got.details


# ---- B5.13 verifier pass 5: negation is fail-safe -----------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "no doubt above $900",
        "without doubt above $900",
        "without question above $900",
        "not only above $900",
        "no wonder it exceeds $900",
        "no, it is above $900",
        "nothing unusual, above $900",
        "not much, but above $900",
        "never seen, over $900",
        "no prior history above 41 transactions",
        "not no more than $900",
        "not-above $800",
        "No-one above $800",
        "never, ever, at all above $800",
        "not even close to above $800",
        "non above $800",
        "not; above $800",
        "not. above $800",
    ],
)
def test_sev_no_negation_lets_a_false_claim_pass(reason):
    got = value_check(reason, {"amount": 812.4, "card_history_n": 40})
    assert got.value == 0.0, (reason, got.details)
    # D-72: ambiguous, so unverifiable (never verified), not counted as a contradiction
    assert got.details["outcome"] == "unverifiable", (reason, got.details)
    assert [c["status"] for c in got.details["checks"]] == ["ambiguous"]


def test_sev_ambiguous_negation_is_named_and_a_clean_sentence_still_reads():
    got = value_check("no doubt above $800", {"amount": 812.4})
    assert got.value == 0.0  # documented limit: true, but ambiguous
    assert any(c.get("reason") == "ambiguous_negation" for c in got.details["checks"])
    assert got.details["outcome"] == "unverifiable" and got.details["failed"] == ()
    assert got.details["unverified"] == ("$800",)
    # a negation in an earlier sentence does not touch this one
    assert value_check("No fraud flags. Amount above $800.", {"amount": 812.4}).value == 1.0
    assert value_check("amount not above $900", {"amount": 812.4}).value == 1.0
    exceed = value_check("amount doesn't exceed $800", {"amount": 812.4})
    assert exceed.value == 0.0 and exceed.details["outcome"] == "contradicted"


# ---- B5.13 fraud-review integration: false negatives on real model reasons ----------------------
# fraud-review-bedrock ran value_check (add5af9) on its eval2 reasons: 2,303 of 3,536 passed, most
# failures true claims. The reasons below are theirs (simulated card data); the records are cut
# down to the fields the reasons talk about.

_FR = {
    "amount": 7.07,
    "risk_score": 0.39,
    "amt_vs_usual": 4.43,
    "hour": 23,
    "ts_hour": 23,
    "ts_dow": 5,
    "category_freq": 0.06,
    "card_history_n": 2165,
    "merchant_first_seen": False,
    "category": "misc_pos",
}
_FR_ALIASES = {"amt_vs_usual": ["usual", "usual amount", "card's usual"]}
_FR_CONSTANTS = [5, 10, 200, 50, 0.9, 0.05]


@pytest.mark.parametrize(
    ("reason", "changes", "ok"),
    [
        ("risk score is below 0.9", {}, True),
        ("risk score is above 0.9", {}, False),
        ("risk is below 0.3", {}, False),  # "risk" only partly names risk_score: unclear
        ("The screening risk_score is 0.39, far below 0.9.", {}, True),
        ("Risk score is 0.39, well below the 0.9 never-clear threshold", {}, True),
        ("risk score is not above 0.9", {}, True),
        ("risk score is not below 0.9", {}, False),
        # pass 6: the words after ", and" are another clause's subject, not candidates for 50
        (
            "Amount is 7.07 dollars, under 50, and 0.14 times the card's usual amount",
            {"amt_vs_usual": 0.14},
            True,
        ),
        ("Amount is 7.07 dollars, over 200", {}, False),
        ("amount over 200", {"amount": 336.46}, True),
        ("amount over 200", {"amount": 150.0}, False),
        ("amount under 50", {}, True),
        ("amt_vs_usual of 13.26 is at or above 10, so P3.9 bars it", {"amt_vs_usual": 13.26}, True),
        ("amt_vs_usual of 4.43 is at or above 10", {}, False),
        # pass 6: "Card" is one word of card_history_n, not a mention: unclear
        ("Card has only 3 earlier purchases, below the 5 needed", {"card_history_n": 3}, False),
    ],
)
def test_sev_threshold_claims_compare_with_the_named_field(reason, changes, ok):
    got = value_check(reason, {**_FR, **changes})
    assert (got.value == 1.0) is ok, (reason, got.details)
    history = {"card_history_n": ["earlier purchases"]}
    named = "Card has only 3 earlier purchases, below the 5 needed"
    assert value_check(named, {**_FR, "card_history_n": 3}, aliases=history).value == 1.0
    assert value_check(named, {**_FR, "card_history_n": 7}, aliases=history).value == 0.0


def test_sev_a_named_field_beats_the_closest_value_and_aliases_name_fields():
    # the v0.3 reading compared with the first field within tolerance: amount 0.85 is near 0.9,
    # so "risk score below 0.9" passed although risk_score is 0.95 (a false pass, now caught)
    rec = {"amount": 0.85, "risk_score": 0.95}
    got = value_check("risk score is below 0.9", rec)
    assert got.value == 0.0
    assert got.details["checks"][0]["field"] == "risk_score"
    assert got.details["checks"][0]["via"] == "name"
    spend = {"amount": 7.07, "card_history_n": 3}
    # names nothing and no field is near 50: never verified; the record cannot settle a
    # comparison without a subject, so it is unverifiable (`no_field`), not a contradiction
    nameless = value_check("spend is under 50", spend)
    assert nameless.value == 0.0 and nameless.details["outcome"] == "unverifiable"
    assert nameless.details["checks"][0]["reason"] == "no_field"
    assert nameless.details["checks"][0]["status"] == "unchecked"
    assert value_check("spend is under 50", spend, aliases={"amount": ["spend"]}).value == 1.0
    assert value_check("spend is over 50", spend, aliases={"amount": ["spend"]}).value == 0.0
    # a named missing value, or an alias for a field the record lacks, fails the claim
    missing = value_check("risk score below 0.9", {"risk_score": None, "amount": 0.85})
    assert missing.value == 0.0 and missing.details["outcome"] == "unverifiable"
    assert missing.details["checks"][0]["reason"] == "missing_value:risk_score"
    absent = value_check("the score is below 0.9", {"amount": 0.85}, aliases={"risk": ["score"]})
    assert absent.details["checks"][0]["reason"] == "missing_value:risk"
    # documented: a comparison naming no field, with no field within tolerance, fails
    assert value_check("well below 0.9", {"risk_score": 0.39}).value == 0.0
    # a plain number no field backs is a mismatch: contradicted
    stray = value_check("Risk score of 0.3", {"risk_score": 0.39})
    assert stray.details["outcome"] == "contradicted"
    bare = value_check("It was 950", {"amount": 812.4})
    assert bare.details["outcome"] == "contradicted" and bare.details["failed"] == ("950",)
    # a field named in another clause is not this number's field
    two = value_check("Risk score is 0.39; well below 0.9", {"risk_score": 0.39})
    assert two.value == 0.0
    same = value_check("Risk score is 0.39, and well below 0.9", {"risk_score": 0.39})
    assert same.value == 1.0


def test_sev_a_multiple_names_only_a_ratio_field():
    """ "5x" is a ratio: "Amount is 4.1 times usual, above the 5x threshold" is false even
    though the amount (318) is above 5."""
    rec = {"amount": 318.04, "amt_vs_usual": 4.14}
    false = "Amount is 4.1 times the card's usual, a spend signal above the 5x threshold."
    assert value_check(false, rec, aliases=_FR_ALIASES).value == 0.0
    true = "Amount is 4.1 times the card's usual, a spend signal below the 5x threshold."
    assert value_check(true, rec, aliases=_FR_ALIASES).value == 1.0
    assert value_check("Spend at 10 or more times usual", rec, aliases=_FR_ALIASES).value == 0.0
    big = {**rec, "amt_vs_usual": 30.9}
    assert value_check("Spend at 10 or more times usual", big, aliases=_FR_ALIASES).value == 1.0


@pytest.mark.parametrize(
    ("reason", "hour", "ok", "n_checked"),
    [
        # pass 6: a window is checked against the hour field its clause names
        ("Purchase at hour 22 falls in the 10 p.m. to 6 a.m. window", 22, True, 2),
        ("Purchase hour is 23 (11 p.m.), inside the 10 p.m.-6 a.m. window", 23, True, 3),
        ("Purchase hour is 23 (10 p.m. local)", 23, False, 2),  # the model's slip, caught
        ("The purchase hour was 6am", 6, True, 1),
        ("The purchase hour was 6am", 18, False, 1),
        ("The hour was 10:30 p.m.", 22, None, 1),  # pass 6: hour 22 does not give minutes
        ("The hour was 22:00", 22, True, 1),
        ("The hour is after 10 p.m.", 23, True, 1),
        ("The hour is after 10 p.m.", 21, False, 1),
        ("The hour is before 6 a.m.", 3, True, 1),
        ("The hour is not 11 p.m.", 23, None, 1),  # a negation before a time: ambiguous
        ("Bought at 22:00", 3, None, 0),  # no hour field named: unchecked, not a failure
        ("Bought at 6am", 3, None, 0),
        ("Hour 6 is after the midnight-to-5 a.m. signal window", 6, True, 2),
        ("Hour is 7, outside the 10 to 6 a.m. window", 7, True, 2),
    ],
)
def test_sev_times_of_day_are_not_free_numbers(reason, hour, ok, n_checked):
    """ok None: unverifiable (D-72), so not verified and not contradicted either."""
    got = value_check(reason, {"hour": hour, "amount": 10.0, "card_history_n": 6})
    outcome = {True: "verified", False: "contradicted", None: "unverifiable"}[ok]
    assert got.value == (1.0 if ok else 0.0), (reason, got.details)
    assert got.details["outcome"] == outcome, (reason, got.details)
    assert got.details["n_checked"] == n_checked, got.details


def test_sev_times_are_recorded_and_a_phrase_may_claim_one():
    got = value_check("Bought at 22:00, in the 10 p.m. to 6 a.m. window", {"n": 22, "hour": 3})
    kinds = sorted(u["kind"] for u in got.details["unchecked"])
    assert kinds == ["time", "time_window"] and got.details["n_checked"] == 0
    phrase = ValuePhrase(pattern=r"at \d{1,2} ?[ap]\.?m\.?", field="hour")
    assert value_check("Bought at 3 a.m.", {"hour": 3}, phrases=[phrase]).value == 1.0
    assert value_check("Bought at 3 a.m.", {"hour": 15}, phrases=[phrase]).value == 0.0
    assert value_check("Bought at 3 p.m.", {"hour": 15}, phrases=[phrase]).value == 1.0
    # "ts_hour" is hour-like too; a 24-hour clock past 23 is not a time
    assert value_check("ts_hour is 23 (11 p.m.)", {"ts_hour": 23}).value == 1.0
    assert value_check("ts_hour is 23 (11 p.m.)", {"ts_hour": 13}).value == 0.0
    late = value_check("hour 25:00", {"hour": 25})  # read as no time and no check: unverifiable
    assert late.details["outcome"] == "unverifiable" and late.details["failed"] == ()


def test_sev_policy_constants_rule_ids_and_ignored_spans():
    rec = {"amount": 212.01, "amt_vs_usual": 4.43, "risk_score": 0.1}
    signal = "This trips the 7x fraud signal"
    assert value_check(signal, rec).value == 0.0
    got = value_check(signal, rec, constants=[7])
    # a declared constant is recorded, never a contradiction; nothing else: unverifiable (D-72)
    assert got.details["outcome"] == "unverifiable" and got.details["n_checked"] == 0
    assert got.details["unchecked"] == ({"text": "7x", "kind": "constant", "value": 7.0},)
    threshold = value_check("the threshold of 0.9 applies", rec, constants=[0.9])
    assert threshold.details["outcome"] == "unverifiable"
    # a constant needs a threshold word next to it, and a clause that names no field for it
    it_is = value_check("it is 7", rec, constants=[7])
    assert it_is.value == 0.0 and it_is.details["outcome"] == "contradicted"
    false = "The risk score is above the 0.9 threshold"
    assert value_check(false, rec, constants=_FR_CONSTANTS).value == 0.0
    assert value_check("risk score is at the 0.9 threshold", rec, constants=[0.9]).value == 0.0
    assert value_check("The risk score is below the 0.9 threshold", rec).value == 1.0
    # fraud-review: the multiples compare with amt_vs_usual, named by its alias "usual"
    reason = (
        "Amount is 4.43 times the card's usual, which is elevated but below the 5x fraud "
        "signal threshold, and well below the 10x no-clear threshold."
    )
    phrased = value_check(reason, rec, aliases=_FR_ALIASES, constants=_FR_CONSTANTS)
    assert phrased.value == 1.0, phrased.details
    # clause and rule ids are never numbers
    for text in ("rule 7.1 applies", "P7.1 applies", "clause P5.9 bars it", "see §4.2", "(P5.9)."):
        got = value_check(text, {"x": 1.0})
        assert got.details["n_checked"] == 0 and got.details["outcome"] == "unverifiable", text
    # ignore: numbers in the matched spans are not checked
    got = value_check("version 3 of the policy says so", rec, ignore=[r"version \d+"])
    assert got.details["outcome"] == "unverifiable" and got.details["failed"] == ()
    assert got.details["unchecked"][0]["kind"] == "ignored"
    with pytest.raises(ValueError):
        value_check("x", rec, ignore=["("])
    with pytest.raises(ValueError):
        value_check("x", rec, ignore="policy")
    with pytest.raises(ValueError):
        value_check("x", rec, constants=[math.inf])
    with pytest.raises(ValueError):
        value_check("x", rec, aliases={"amount": "spend"})


@pytest.mark.safety
def test_sev_ignored_spans_and_constants_never_hide_a_negation():
    rec = {"amount": 812.4}
    reason = "the never-auto-clear rule: amount is above $800"
    for got in (
        value_check(reason, rec),
        value_check(reason, rec, ignore=[r"never-auto-clear"]),
        value_check(reason, rec, constants=[800]),
    ):
        assert got.value == 0.0 and got.details["checks"][0]["reason"] == "ambiguous_negation"
        assert got.details["outcome"] == "unverifiable"


_FIRST = ValuePhrase(pattern=r"first purchase at this merchant", holds="merchant_first_seen")
_NIGHT = ValuePhrase(pattern=r"at night", holds="hour >= 22 or hour < 6")
_NEVER = ValuePhrase(pattern=r"never (?:been )?used at this merchant", holds="merchant_first_seen")


@pytest.mark.parametrize(
    ("reason", "record", "ok"),
    [
        ("A first purchase at this merchant", {"merchant_first_seen": True}, True),
        ("A first purchase at this merchant", {"merchant_first_seen": False}, False),
        ("This is not a first purchase at this merchant", {"merchant_first_seen": False}, True),
        ("This is not a first purchase at this merchant", {"merchant_first_seen": True}, False),
        ("It isn't at night", {"hour": 14}, True),
        ("never at night", {"hour": 3}, False),
        # any other negation earlier in the sentence: ambiguous, fails either way
        # (None: unverifiable, never verified; D-72)
        ("It is not true that the purchase was at night", {"hour": 3}, None),
        ("It is not true that the purchase was at night", {"hour": 14}, None),
        ("No flags, but a first purchase at this merchant", {"merchant_first_seen": True}, None),
        (
            "Not, as the analyst wrote in this morning's long note, made at night",
            {"hour": 3},
            None,
        ),
        # a negation in an earlier sentence does not count; a phrase's own words do not either
        ("No fraud flags. Purchase at night.", {"hour": 3}, True),
        ("The card has never been used at this merchant", {"merchant_first_seen": True}, True),
        ("The card has never been used at this merchant", {"merchant_first_seen": False}, False),
    ],
)
def test_sev_negated_condition_phrases(reason, record, ok):
    got = value_check(reason, record, phrases=[_FIRST, _NIGHT, _NEVER])
    assert got.value == (1.0 if ok else 0.0), (reason, got.details)
    outcome = {True: "verified", False: "contradicted", None: "unverifiable"}[ok]
    assert got.details["outcome"] == outcome, (reason, got.details)


@pytest.mark.safety
@pytest.mark.parametrize(
    "reason",
    [
        "Not, as the analyst wrote in this morning's long note, above $800",
        "It is not the case that, as claimed in the earlier review, the amount is 812.40",
        "Nothing suggests that the purchase, which came late in the evening, was over $800",
    ],
)
def test_sev_a_negation_far_back_in_the_sentence_is_still_ambiguous(reason):
    """v0.3 looked 40 characters back for a negation; a longer gap let a false claim pass."""
    got = value_check(reason, {"amount": 812.4})
    assert got.value == 0.0, got.details
    assert got.details["checks"][0]["reason"] == "ambiguous_negation"
    assert got.details["outcome"] == "unverifiable"


def test_sev_value_check_scorer_keeps_each_checks_reason(registry):
    s = value_check_scorer(phrases=PHRASES, name="alert_values_idx", record_key="alert")
    texts = ["Purchase at night", "Amount is 9x this card's usual spend", "no doubt above $800"]
    rec = {**SEV["record"], "amt_vs_usual": None}
    res = s(case({"alert": rec}), completion("hold", json.dumps({"reasons": texts})))
    checks = res.details["checks"]
    assert [c["reason_index"] for c in checks] == [0, 1, 2]
    assert checks[1]["reason"] == "missing_value:amt_vs_usual"
    assert checks[2]["reason"] == "ambiguous_negation"
    assert "reason" not in checks[0]
    assert [c["status"] for c in checks] == ["ok", "unchecked", "ambiguous"]
    assert [r["outcome"] for r in res.details["reasons"]] == [
        "verified",
        "unverifiable",
        "unverifiable",
    ]
    assert res.value == 0.0 and res.details["outcome"] == "unverifiable"
    timed = value_check_scorer(name="alert_values_time", record_key="alert", constants=[5])
    answer = json.dumps({"reasons": ["x", "at 22:00"]})
    res = timed(case({"alert": {"hour": 3}}), completion("hold", answer))
    assert res.details["unchecked"] == ({"text": "22:00", "kind": "time", "reason_index": 1},)
    with pytest.raises(ValueError):
        value_check_scorer(name="bad_ignore", ignore=["("])


# fraud-review's own reasons and records (eval2, simulated), with the options it would pass
_FR_CASES = [
    (
        "Purchase hour is 23 (11 p.m.), which falls in the 10 p.m. to 6 a.m. window where P5.9 "
        "requires a person to review, so this alert cannot be cleared.",
        {"hour": 23, "amount": 15.37, "risk_score": 0.005},
        True,
    ),
    (
        "Amount is 4.431647157190635 times the card's usual, which is elevated but below the 5x "
        "fraud signal threshold, and well below the 10x no-clear threshold.",
        {"amount": 212.01, "amt_vs_usual": 4.431647157190635, "ts_dow": 5},
        True,
    ),
    (
        "Purchase of 336.46 is a first purchase at this merchant (merchant_first_seen is true) "
        "and the amount is 200 or more, so P4.9 forbids clearing.",
        {"amount": 336.46, "merchant_first_seen": True},
        True,
    ),
    (
        "Hour is 7, outside the 10 p.m. to 6 a.m. no-clear window. The screening risk_score is "
        "0.0037, far below 0.9.",
        {"hour": 7, "risk_score": 0.003689352418426931, "amount": 11.76},
        True,
    ),
    (
        "Amount is 7.07 dollars, under 50, and 0.14 times the card's usual amount",
        {"amount": 7.07, "amt_vs_usual": 0.13706863125242344},
        True,
    ),
    (
        "amt_vs_usual of 13.26 is at or above 10, so P3.9 bars auto-clearing and the alert "
        "needs a person",
        {"amt_vs_usual": 13.261725216111824, "amount": 721.04},
        True,
    ),
    (
        "Amount is 11.58 times the card's usual, which is at or above the 10x never-auto-clear "
        "threshold",
        {"amount": 558.99, "amt_vs_usual": 11.58288437629507},
        True,
    ),
    # false variants of the same reasons stay caught
    (
        "Purchase hour is 23 (11 p.m.), which falls in the 10 p.m. to 6 a.m. window",
        {"hour": 14},
        False,
    ),
    (
        "Amount is 4.43 times the card's usual, which is above the 5x fraud signal threshold.",
        {"amount": 212.01, "amt_vs_usual": 4.431647157190635, "ts_dow": 5},
        False,
    ),
    ("Amount is 7.07 dollars, over 50", {"amount": 7.07}, False),
    ("The screening risk_score is 0.0037, far above 0.9.", {"risk_score": 0.0037}, False),
]


@pytest.mark.parametrize(
    ("reason", "record", "ok"), _FR_CASES, ids=[r[:40] for r, _, _ in _FR_CASES]
)
def test_sev_fraud_review_reasons(reason, record, ok):
    phrase = ValuePhrase(
        pattern=r"\d[\d.,]*\s*(?:x|×|times)\s+(?:\S+\s+){0,3}?(?:usual|normal|typical|median)",
        field="amt_vs_usual",
    )
    got = value_check(
        reason, record, phrases=[phrase], aliases=_FR_ALIASES, constants=_FR_CONSTANTS
    )
    assert (got.value == 1.0) is ok, (reason, got.details)


@pytest.mark.safety
@pytest.mark.parametrize(
    "text",
    [
        "risk score below 0.9 " * 2_000,
        "10 p.m. to 6 a.m. " * 2_000,
        "rule 7.1 " * 3_000,
        "22:00 " * 4_000,
        "the 5x signal threshold " * 2_000,
        "not " * 5_000 + "above 5",
    ],
    ids=["named", "windows", "ids", "clock", "constants", "negations"],
)
def test_sev_value_check_new_readings_are_linear(text):
    t0 = time.perf_counter()
    value_check(text, _FR, aliases=_FR_ALIASES, constants=_FR_CONSTANTS, ignore=[r"v\d"])
    assert time.perf_counter() - t0 < 3.0  # linear-time guard; slow CI runners take ~0.8 s


@pytest.mark.parametrize(
    ("reason", "record", "ok"),
    [
        # "and the 10x" shares the comparison of "above the 5x"
        (
            "Amount is 26.49 times the card's usual, well above the 5x fraud signal (P3.1) and "
            "the 10x never-auto-clear limit (P3.9).",
            {"amount": 1026.66, "amt_vs_usual": 26.49},
            True,
        ),
        (
            "Amount is 7.43 times the card's usual, above both the 5x signal and the 10x limit.",
            {"amount": 389.32, "amt_vs_usual": 7.43},
            False,
        ),
        (
            "Amount is 2.05 times the card's usual, well below the 5x signal level and the 10x "
            "never-clear level.",
            {"amount": 98.35, "amt_vs_usual": 2.05},
            True,
        ),
        (
            "Amount is 27.6 times the card's usual, which meets the 5x fraud signal (P3.1) and "
            "the 10x never-auto-clear limit (P3.9), so this cannot be cleared.",
            {"amount": 1401.35, "amt_vs_usual": 27.6074, "ts_dow": 5},
            True,
        ),
        (
            "Amount is 7.4 times the card's usual, which meets the 5x fraud signal (P3.1) and "
            "the 10x never-auto-clear limit (P3.9).",
            {"amount": 389.32, "amt_vs_usual": 7.43, "ts_dow": 5},
            False,
        ),
        (
            "Amount is 7.4 times usual, which does not meet the 10x limit",
            {"amt_vs_usual": 7.4},
            True,
        ),
        # a different kind of number is not joined: "under 50, and the 0.14 times usual"
        (
            "Amount is 7.07, under 50, and the 0.14 times usual",
            {"amount": 7.07, "amt_vs_usual": 0.14},
            True,
        ),
        # ", which is at least 200" refers back to the number before it
        ("First purchase here for 801.52, which is at least 200", {"amount": 801.52}, True),
        ("First purchase here for 150.00, which is at least 200", {"amount": 150.0}, False),
        ("for 986.73, which is 200 or more", {"amount": 986.73}, True),
        ("for 186.73, which is 200 or more", {"amount": 186.73}, False),
        # not after a negation, and not across a named field
        ("It is not 801.52, which is at least 200", {"amount": 801.52}, False),
        (
            "Paid 801.52 with risk 0.01, which is at least 200",
            {"amount": 801.52, "risk_score": 0.01},
            False,
        ),
    ],
)
def test_sev_coordinated_and_back_referring_comparisons(reason, record, ok):
    got = value_check(reason, record, aliases=_FR_ALIASES)
    assert (got.value == 1.0) is ok, (reason, got.details)


# ---- B5.13 verifier pass 6: a false claim is never verified ------------------------------------
# The verifier's generator (named fields, joins, comparison words; record below) had 2,616 of
# 10,080 false claims verified. Governing rule (D-72): verified only when every statement the
# checker reads was checked on a clear reading and held; anything unsure is unverifiable.

_V6 = {
    "amount": 812.4,
    "amt_vs_usual": 9.1,
    "hour": 3,
    "card_history_n": 40,
    "risk_score": 0.83,
    "category_freq": 0.05,
    "merchant_first_seen": True,
}
_OUTCOME = {True: "verified", False: "contradicted", None: "unverifiable"}


def _v6(reason: str, ok: bool | None, record: dict[str, Any] | None = None, **kw: Any) -> None:
    got = value_check(reason, _V6 if record is None else record, **kw)
    assert got.details["outcome"] == _OUTCOME[ok], (reason, got.details)
    assert got.value == (1.0 if ok else 0.0)


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        # a time no hour field backs: unverifiable, never verified with the amount beside it
        ("Purchase at 7 p.m., amount $812.40", None),
        ("Purchase at 9 p.m., amount $812.40", None),
        ("Purchase at 11:15, amount $812.40", None),
        # checked against the hour its clause names: false is contradicted
        ("Purchase hour 7 p.m., amount $812.40", False),
        ("Purchase hour 3 a.m., amount $812.40", True),
        # a window no hour field backs, or one whose place the reason does not state
        ("Purchase between 9 a.m. and 5 p.m., amount $812.40", None),
        ("Purchase from 1 p.m. to 2 p.m., amount $812.40", None),
        ("Hour 3 sits at the edge of the midnight to 5 a.m. window", None),
        # a window the clause places an hour field in or out of is checked
        ("Purchase during business hours, 9 a.m. to 5 p.m., amount $812.40", False),
        ("Hour 3 falls in the 10 p.m. to 6 a.m. window", True),
        ("Hour 3 is outside the 10 p.m. to 6 a.m. window", False),
        ("Hour 3 is outside the 9 a.m. to 5 p.m. window", True),
        ("Hour 3 falls in the midnight-to-5 a.m. window", True),
        # a whole hour does not give the minutes
        ("Purchase hour before 3:30 a.m.", None),
        ("Purchase hour after 3:30 a.m.", None),
        ("Purchase hour 3:45 a.m.", None),
        ("Purchase hour before 4 a.m.", True),
        ("Purchase hour after 4 a.m.", False),
    ],
)
def test_sev_v6_unchecked_times_and_windows_are_never_verified(reason, ok):
    _v6(reason, ok, phrases=PHRASES)


def test_sev_v6_unchecked_times_are_listed_as_unverified():
    got = value_check("Purchase at 7 p.m., amount $812.40", _V6, phrases=PHRASES)
    assert got.details["n_checked"] == 1 and got.details["checks"][0]["status"] == "ok"
    assert got.details["unchecked"] == ({"text": "7 p.m.", "kind": "time"},)
    assert got.details["unverified"] == ("7 p.m.",)
    # a window across midnight with a bare first hour ("10 to 6 a.m.") is read both ways
    assert value_check("Hour 7 is outside the 10 to 6 a.m. window", {"hour": 7}).value == 1.0
    two = value_check("Hour 11 is outside the 10 to 6 a.m. window", {"hour": 11})
    assert two.details["outcome"] == "unverifiable"  # 10 a.m. or 10 p.m.: they disagree


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        # the clause names the amount and the claim is about "earlier transactions"
        ("Amount of $812.40 with more than 50 earlier transactions on the card", None),
        ("Amount $812.40 with over 50 transactions", None),
        ("Risk score 0.83 with over 50 transactions", None),
        ("The amount is high, with more than 50 card transactions", None),
        # two fields named: the comparison must hold for both (and fail for both to fail)
        ("The risk score, unlike the amount, is above 0.9", None),
        ("The amount of the hour is below 270.8", None),
        ("Card history with below 270.8 for the amount", None),
        ("The risk score, unlike the amount, is below 900", True),
        ("The risk score, unlike the amount, is above 1000", False),
        # a field after a clause boundary is another clause's subject
        ("Amount is 9.16, under 50, and the merchant is known to the card", True),
    ],
)
def test_sev_v6_a_comparison_must_hold_for_every_field_its_clause_names(reason, ok):
    _v6(reason, ok, record={**_V6, "amount": 9.16} if "9.16" in reason else None)
    if "earlier transactions" in reason:
        _v6(reason, ok, phrases=PHRASES)


@pytest.mark.safety
def test_sev_v6_the_verifiers_generator_verifies_no_false_claim():
    """The verifier's generator (gen.py, pass 6): every claim is false on _V6."""
    words = {
        "amount": ["the amount", "amount"],
        "risk_score": ["the risk score", "risk"],
        "card_history_n": ["card history", "the card history"],
        "hour": ["the hour"],
    }
    cmps = {">": ["above", "over", "more than", "exceeds"], "<": ["below", "under", "less than"]}
    joins = [" with ", " and ", " while ", ", and ", ", like ", " compared with ", " next to "]
    joins += [" unlike "]
    verified = []
    n = 0
    for t, d in ((t, d) for t in words for d in words if t != d):
        for op, cws in cmps.items():
            v = _V6[t]
            x = round(v * 2 + 1, 2) if op == ">" else round(v / 3, 3)
            for cw in cws:
                for j in joins:
                    for tw in words[t]:
                        for dw in words[d]:
                            for s in (
                                f"{dw.capitalize()} is high{j}{tw} {cw} {x}",
                                f"{dw.capitalize()}{j}{cw} {x} for {tw}",
                                f"{cw.capitalize()} {x} on {tw}{j}{dw} noted",
                                f"{tw.capitalize()}, unlike {dw}, is {cw} {x}",
                                f"{tw.capitalize()} of the {dw.split()[-1]} is {cw} {x}",
                            ):
                                n += 1
                                if value_check(s, _V6).value == 1.0:
                                    verified.append(s)
    assert n == 10_080 and verified == [], verified[:10]


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        ("The card's usual spend is under 50", None),  # "card", "usual": one word each
        ("card spend above 50", None),
        ("risk is below 0.9", None),
        ("Risk of the amount is above 2.66", None),
        ("card history above 50", False),  # the whole name ("card history" of card_history_n)
        ("card history below 50", True),
        ("risk score is below 0.9", True),
    ],
)
def test_sev_v6_one_shared_word_is_not_a_field_mention(reason, ok):
    _v6(reason, ok)
    _v6(reason, ok, phrases=PHRASES)


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        ("Spend is 3 X usual", False),
        ("Spend is 3 x usual", False),
        ("Spend is 3X usual", False),
        ("Spend is 9 X usual", True),
        ("Spend is 9 x the usual", True),
    ],
)
def test_sev_v6_a_spaced_x_is_a_multiple(reason, ok):
    """ "3 X" is a multiple: compared only with a ratio field, never with amount 3."""
    _v6(reason, ok, record={**_V6, "amount": 3.0, "hour": 9})
    check = value_check(reason, {**_V6, "amount": 3.0}).details["checks"][0]
    assert check["field"] != "amount"


@pytest.mark.parametrize(
    ("reason", "constants", "ok"),
    [
        ("Spend crosses the 20x limit; amount $812.40", [20], None),
        ("Spend reached the 20x limit; amount $812.40", [20], None),
        ("Score sits on the 0.95 threshold; amount $812.40", [0.95], None),
        ("Amount $812.40 hits the 10x never-auto-clear threshold", [10], None),
        # a comparison with a named field is still checked
        ("Risk score is below the 0.95 threshold", [0.95], True),
        ("Risk score is above the 0.95 threshold", [0.95], False),
    ],
)
def test_sev_v6_a_constant_never_makes_a_reason_verified(reason, constants, ok):
    _v6(reason, ok, phrases=PHRASES, constants=constants)


def test_sev_v6_a_recorded_constant_is_listed_as_unverified():
    got = value_check("Score sits on the 0.95 threshold; amount $812.40", _V6, constants=[0.95])
    assert got.details["unchecked"] == ({"text": "0.95", "kind": "constant", "value": 0.95},)
    assert got.details["outcome"] == "unverifiable" and got.details["unverified"] == ("0.95",)


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        ("risk score exceeds the threshold of 0.9", False),
        ("risk score is below the threshold of 0.9", True),
        ("risk score is below the extreme-risk limit of 0.9", True),
        ("amt_vs_usual is 10.02, which meets the extreme-spend threshold (10x)", True),
        ("amount above USD 850", False),
        ("amount above USD 800", True),
        ("The amount is above both 800 and 900", False),
        ("The amount is above both 800 and 810", True),
        # a comparison word whose shape is not read: unclear, never an equality
        ("Amount is above what the policy allows, 812.40", None),
        ("Amount is higher than usual at 812.40", None),
        ("Spend crosses 20x the usual", None),
    ],
)
def test_sev_v6_a_comparison_word_away_from_its_number(reason, ok):
    _v6(reason, ok, record={**_V6, "amt_vs_usual": 10.02} if "10.02" in reason else None)


@pytest.mark.parametrize(
    ("reason", "ok"),
    [
        ("Amount above $800 is not true", None),
        ("Amount above $800 is not the case", None),
        ("Risk score of 0.83 is not accurate", None),
        ("Amount above $800, which is not true", None),
        ("Amount above $800, which the record does not show", None),
        ("Amount above $800 is untrue", None),
        ("Amount is 812.40, not 900", None),
        ("Purchase at night is not the case", None),
        ("a new merchant? Not at all", None),
        ("A new merchant. No.", None),
        ("Hardly at night? no", None),
        # a negation in a later clause with its own subject denies nothing
        ("Amount above $800, which is not a small purchase", True),
        ("A new merchant, so the routine clear clause does not apply", True),
        ("Amount above $800 and the 10x never-auto-clear rule applies", True),
    ],
)
def test_sev_v6_a_negation_after_the_claim_is_ambiguous(reason, ok):
    _v6(reason, ok, phrases=PHRASES)


@pytest.mark.safety
@pytest.mark.parametrize(
    "text",
    [
        "Hour 3 falls in the 10 p.m. to 6 a.m. window, " * 1_000,
        "card " * 5_000 + "above 5",
        "5 not " * 3_000,
        "risk above 0.5 for the amount " * 1_000,
        "amount 5, which is not true " * 1_000,
        "above both 1 and 2 and 3 " * 1_000,
        "no. " * 5_000 + "a 5",
    ],
    ids=["windows", "partials", "negations-after", "candidates", "denials", "both", "responses"],
)
def test_sev_v6_new_readings_are_linear(text):
    t0 = time.perf_counter()
    value_check(text, {**_V6, "ts_hour": 3}, aliases=_FR_ALIASES, constants=_FR_CONSTANTS)
    assert time.perf_counter() - t0 < 3.0  # linear-time guard; slow CI runners take ~0.8 s
