"""SEV-01 value check, strict (B5.16, D-74): `verified` only when every number in a reason is
bound to a field by positive evidence and holds; never by closeness of value."""

from __future__ import annotations

import json
import random
import time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from evalkit.schemas import Answer, Case, Completion
from evalkit.scorers import base as scorer_base
from evalkit.scorers.base import ScoreResult
from evalkit.scorers.evidence import (
    ValuePhrase,
    value_check,
    value_check_scorer,
    value_check_shares,
)
from tests.scorers.value_check_generators import gen1, gen2

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "agent_scorers"
SEV = json.loads((FIX / "fraud_evidence.json").read_text(encoding="utf-8"))
NEVER = json.loads((FIX / "value_check_never_verified.json").read_text(encoding="utf-8"))
PHRASES = [ValuePhrase(**p) for p in SEV["phrases"]]

REC: dict[str, Any] = {
    "amount": 812.4,
    "amt_vs_usual": 9.1,
    "hour": 3,
    "card_history_n": 40,
    "risk_score": 0.83,
    "category_freq": 0.05,
    "merchant_first_seen": True,
}
ALIASES = {"card_history_n": ["card history", "earlier purchases"]}
UNITS: dict[str, Any] = {
    "risk_score": "fraction",
    "category_freq": "fraction",
    "amt_vs_usual": "multiple",
    "amount": "currency",
    "hour": "hour",
}


@pytest.fixture
def registry(monkeypatch):
    """Factory scorers register names; keep each test's registrations to itself."""
    monkeypatch.setattr(scorer_base, "_REGISTRY", dict(scorer_base._REGISTRY))
    monkeypatch.setattr(scorer_base, "_BUILTIN", dict(scorer_base._BUILTIN))


def outcome(reason: str, record: dict[str, Any] | None = None, **kw: Any) -> str:
    return value_check(reason, REC if record is None else record, **kw).details["outcome"]


def case(meta: dict[str, Any]) -> Case:
    return Case(id="c1", question="q", context=("x",), reference_label=None, source="t", meta=meta)


def completion(text: str = "", *, error: str | None = None) -> Completion:
    return Completion(
        model="m",
        model_snapshot="",
        params={},
        raw_text=text,
        answer=None if error else Answer(label="hold", answer=text, citations=()),
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        cost_usd=0.0,
        error=error,  # type: ignore[arg-type]
    )


# ================================ never verified: the verifier passes' false claims =======


@pytest.mark.safety
def test_false_claim_reproductions_are_never_verified():
    """Every reason from B5.13 verifier passes 1-7 (verify513..verify513g probes) and every
    B5.13 test reason that was not verified: none is verified (logical, D-74)."""
    verified = []
    for reason, r, p, o, origin in NEVER["cases"]:
        phrases = [ValuePhrase(**x) for x in NEVER["phrases"][p]]
        res = value_check(reason, NEVER["records"][r], phrases=phrases, **NEVER["options"][o])
        if res.details["outcome"] == "verified":
            verified.append((origin, reason))
    assert len(NEVER["cases"]) >= 590
    assert verified == []


@pytest.mark.safety
def test_gen1_verifies_no_false_claim():
    """verify513f/gen.py at full size: 10,080 false comparisons, 0 verified."""
    n, verified = 0, []
    for _, text, rec in gen1():
        n += 1
        if value_check(text, rec).value == 1.0:
            verified.append(text)
    assert n == 10_080 and verified == [], verified[:5]


@pytest.mark.safety
@pytest.mark.parametrize("seed", [7, 11])
def test_gen2_verifies_no_false_claim(seed):
    """verify513g/gen2.py at full size (400 records, about 56,000 false claims in 14
    families): 0 verified."""
    n, verified = 0, []
    for family, text, rec in gen2(seed):
        n += 1
        if value_check(text, rec).value == 1.0:
            verified.append((family, text))
    assert n > 55_000 and verified == [], verified[:5]


@pytest.mark.safety
def test_gen2_families_with_declared_units_and_aliases_verify_nothing():
    """Declaring every unit and the partial names as aliases binds more claims; still every
    false claim is contradicted or unverifiable."""
    aliases = {"card_history_n": ["card history"], "category_freq": ["category freq"]}
    units: dict[str, Any] = {**UNITS, "category_freq": "fraction"}
    verified = [
        (f, t)
        for f, t, rec in gen2(3, 120)
        if value_check(t, rec, aliases=aliases, units=units).value == 1.0
    ]
    assert verified == []


# ================================ bound true claims are verified ==========================


@pytest.mark.parametrize(
    "reason",
    [
        "Amount is 812.40",
        "The amount is 812.40.",
        "amount of 812.4",
        "Amount: 812.40",
        "amount = 812.4",
        "amount is 812",  # displayed precision
        "risk score 0.83",
        "risk_score is 0.83",
        "RISK SCORE is 0.83",
        "riskscore is 0.83",  # spaces and underscores are interchangeable
        "The risk score (0.83) is high",
        "risk score above 0.8",
        "risk score is below 0.9",
        "risk score > 0.5",
        "risk score is at least 0.83",
        "risk score ≤ 0.83",
        "amount over 800",
        "amount exceeds 800",
        "card history is 40",
        "card_history_n is 40",
        "The card has 40 earlier purchases",
        "more than 30 earlier purchases",
        "risk score is 83%",
        "category freq is 5%",
        "amt vs usual is 9.1x",
        "amt_vs_usual is 9.1 times",
        "amount is $812.40",
        "amount is 812.40 USD",
        "hour was 3 a.m.",
        "hour is 03:00",
        "merchant_first_seen is true",
        "Amount is 812.40 and risk score is 0.83; card history is 40.",
        "Amount 812.40, risk score 0.83, card history 40.",
        "- amount: 812.40\n- risk score: 0.83",
        "risk score of 0.83, so this is a hold",
    ],
)
def test_bound_true_claims_are_verified(reason):
    res = value_check(reason, REC, aliases=ALIASES, units=UNITS)
    assert res.details["outcome"] == "verified" and res.value == 1.0, res.details


def test_fixture_phrases_bind_their_one_number():
    assert outcome("Amount is 9.1x this card's usual spend", phrases=PHRASES) == "verified"
    assert outcome("Spend is more than 5 times the usual amount", phrases=PHRASES) == "verified"
    assert outcome("A $812.40 purchase", phrases=PHRASES) == "verified"
    # "9x" on 9.1 is a correct rounding but 1.1% off: too coarse to verify (D-42 tolerance)
    assert outcome("Amount is 9x this card's usual spend", phrases=PHRASES) == "unverifiable"
    assert outcome("Amount is 15x this card's usual spend", phrases=PHRASES) == "contradicted"
    assert outcome("Spend is under 2 times the usual amount", phrases=PHRASES) == "contradicted"
    # two numbers in one phrase match, a hedge, a range, a stray comparison: unbound
    assert outcome("9x usual, 10x usual spend", phrases=PHRASES) == "unverifiable"
    for reason in (
        "about 9x usual spend",
        "$812 or so",
        "$812, or less than that",
        "$812 or higher-value items",
        "9-10x usual spend",
    ):
        assert outcome(reason, phrases=PHRASES) != "verified", reason


# ================================ D-74: everything else is unverifiable ====================


@pytest.mark.parametrize(
    ("reason", "why"),
    [
        ("The price was 812.40", "unknown subject"),
        ("Total is 812.40", "unknown subject"),
        ("It was 40", "no subject"),
        ("The total was 812.4", "equals a field's value, names no field"),
        ("history is 40", "partial name"),
        ("risk is 0.83", "partial name"),
        ("risk scores are 0.83", "plural"),
        ("amounts of 812.40", "plural"),
        ("amout is 812.40", "typo"),
        ("risk scroe is 0.83", "typo"),
        ("risk-score is 0.83", "hyphenated"),
        ("Risk score and amount are 0.83 and 812.40 respectively", "respectively"),
        ("Risk score and amount: 0.83", "coordinated fields"),
        ("Amount, risk score: 0.83", "coordinated fields"),
        ("Risk score / hour: 3 / 0.83", "coordinated fields"),
        ("risk score 0.83 amount", "a field on both sides"),
        ("the risk score of the amount is 0.83", "several fields"),
        ("the average amount is 812.40", "another subject"),
        ("We note amount of 812.40", "another subject"),
        ("Bought at 3 a.m.", "time, no hour field"),
        ("Bought at 22:00", "time, no hour field"),
        ("hour was 3:30 a.m.", "minutes an hour field cannot settle"),
        ("risk score is below the 0.9 threshold", "constant"),
        ("amount is 0.8124 thousand", "scale"),
        ("amount is 81240 cents", "scale"),
        ("amount is 1k", "scale"),
        ("card history is 4e1", "exponent"),
        ("amount is about 812", "approximate"),
        ("risk score is roughly 0.83", "approximate"),
        ("amount is ~812", "approximate"),
        ("card history is forty", "number word"),
        ("amount between 800 and 900", "range"),
        ("amount of 800-900", "range"),
        ("Amount is 812.40 overall", "trailing word"),
        ("card history of 40 or more", "postfix comparison"),
        ("Amount is 812.40 or more", "postfix comparison"),
        ("risk score of 0.83 or less", "postfix comparison"),
        ("risk score is 0.8", "a correct rounding, too coarse"),
        ("amount 812.40 is 9.1 times the usual", "two fields in one clause"),
        ("Amount is 812.40 under P3.1", "a rule id beside the claim"),
        ("risk score is 0.83 against the threshold", "a threshold word in the clause"),
        ("amount is 812.40 on average", "a baseline word"),
        ("card history is ٤٠", "non-ASCII digits"),
        ("card history is 1.2.3", "malformed"),
    ],
)
def test_unbound_numbers_are_unverifiable(reason, why):
    res = value_check(reason, REC, aliases=ALIASES, units=UNITS)
    assert res.details["outcome"] == "unverifiable", (why, res.details)
    assert res.value == 0.0 and res.details["failed"] == ()


@pytest.mark.parametrize(
    "reason",
    [
        "risk score is 83%",
        "category freq is 5 percent",
        "amt vs usual is 9.1x",
        "amount is $812.40",
        "amount is 812.40 USD",
        "hour was 3 a.m.",
    ],
)
def test_undeclared_units_are_unverifiable(reason):
    """A percent, multiple, currency or time binds only to a field that declares its unit."""
    assert outcome(reason, aliases=ALIASES) == "unverifiable"
    assert outcome(reason, aliases=ALIASES, units=UNITS) == "verified"


def test_percent_reads_the_declared_unit():
    fraction = {"risk_score": "fraction"}
    percent = {"risk_score": "percent"}
    assert outcome("risk score is 83%", units=fraction) == "verified"
    assert outcome("risk score is 30%", units=fraction) == "contradicted"
    assert outcome("risk score is 0.83%", units=fraction) == "contradicted"
    assert outcome("risk score is 83%", {"risk_score": 83.0}, units=percent) == "verified"
    assert outcome("risk score above 80%", units=fraction) == "verified"
    assert outcome("risk score above 90 percent", units=fraction) == "contradicted"


def test_integer_fields_are_exact():
    """D-74: "card history is 44" on 40 is never verified, whatever the tolerance."""
    for tol in (0.0, 0.1, 0.5):
        assert outcome("card history is 44", aliases=ALIASES, tolerance=tol) == "contradicted"
        assert outcome("card history is 41", aliases=ALIASES, tolerance=tol) == "contradicted"
    assert outcome("card_history_n is 40.5") == "contradicted"
    assert outcome("card_history_n is 40.0") == "verified"
    assert outcome("hour is 4") == "contradicted"


def test_tolerance_applies_to_non_integer_fields_only():
    assert outcome("amount is 850") == "contradicted"
    assert outcome("amount is 850", tolerance=0.05) == "verified"
    assert outcome("risk score is 0.9", tolerance=0.1) == "verified"
    with pytest.raises(ValueError):
        value_check("x", REC, tolerance=1.0)


def test_contradicted_only_when_a_bound_statement_clearly_fails():
    # one unit off in the last digit shown (a truncation) and a coarse rounding: unsure
    assert outcome("risk score is 0.84") == "unverifiable"
    assert outcome("amt vs usual is 9.0") == "unverifiable"
    assert outcome("risk score is 1") == "unverifiable"
    assert outcome("risk score is 0") == "contradicted"
    # clearly false
    assert outcome("risk score is 0.85") == "contradicted"
    assert outcome("risk score above 0.9") == "contradicted"
    assert outcome("amount is 900") == "contradicted"
    assert outcome("merchant_first_seen is false") == "contradicted"
    # "over 517 earlier purchases" may mean across them: unsure, not false
    assert outcome("over 40 earlier purchases", aliases=ALIASES) == "unverifiable"


def test_never_by_closeness_of_value():
    """A number equal to some field's value is still unbound when the text binds it to none,
    and a bound number is compared with its own field only."""
    assert outcome("The total was 812.4") == "unverifiable"
    assert outcome("It is 0.83") == "unverifiable"
    assert outcome("risk score is 812.40") == "contradicted"  # amount's value, risk's field
    assert outcome("card history n is 3") == "contradicted"  # hour's value


@pytest.mark.parametrize(
    "reason",
    [
        "Amount is not 900",
        "Amount is 812.40, not 900",
        "Amount is 812.40, never 900",
        "Amount above 800 is not true",
        "Amount above 900. Not really.",
        "Amount is 900. That is false.",
        "risk score of 0.9 is wrong",
        "Amount is 900, which is incorrect",
        "If the amount were 900, hold",
        "The amount would be 900",
        "The previous amount was 900",
        "Is the amount 900?",
        "Risk score is 0.9 and hour is 3, respectively",
        "amount doesn't exceed 800",
    ],
)
def test_negations_hypotheticals_and_denials_never_verify_or_contradict(reason):
    res = value_check(reason, REC)
    assert res.details["outcome"] == "unverifiable", res.details
    assert res.details["failed"] == ()


def test_condition_phrases_are_checked_and_a_negation_voids_them():
    night = ValuePhrase(pattern=r"at night", holds="hour >= 22 or hour < 6")
    assert outcome("Purchase at night", phrases=[night]) == "verified"
    assert outcome("Purchase at night", {**REC, "hour": 14}, phrases=[night]) == "contradicted"
    assert outcome("Purchase not at night", phrases=[night]) == "unverifiable"
    missing = value_check("Purchase at night", {"amount": 1.0}, phrases=[night])
    assert missing.details["checks"][0]["reason"] == "missing_value:hour"


def test_booleans_bind_like_numbers():
    assert outcome("merchant_first_seen is true") == "verified"
    assert outcome("(merchant_first_seen true)") == "verified"
    assert outcome("merchant_first_seen: false") == "contradicted"
    assert outcome("amount is true") == "unverifiable"  # wrong type: unchecked
    assert outcome("merchant first seen is true", {"amount": 1.0}) == "unverifiable"


def test_ignore_spans_are_ids_and_constants_only_label():
    got = value_check("Amount is 812.40 (ref 950)", REC, ignore=[r"ref \d+"])
    assert got.details["outcome"] == "verified"
    assert got.details["unchecked"] == ({"text": "950", "kind": "ignored"},)
    # an ignored span never hides a negation
    assert outcome("Amount is 812.40 (ref not 950)", ignore=[r"ref not \d+"]) == "unverifiable"
    got = value_check("Amount is 812.40, the 200 limit applies", REC, constants=[200])
    assert got.details["outcome"] == "unverifiable"
    assert [c["reason"] for c in got.details["checks"]] == [None, "constant"]


# ================================ fraud-review-bedrock reasons ===========================

# Its bridge's phrases and the aliases its measurement used (fraud-review-bedrock, simulated
# card data, MIT, same author).
FR_PHRASES = [
    ValuePhrase(
        pattern=r"\d[\d.,]*\s*(?:x|×|times)\s+(?:\S+\s+){0,3}?(?:usual|normal|typical|median)",
        field="amt_vs_usual",
    ),
    ValuePhrase(
        pattern=r"(?:rare|rarely used|uncommon|unusual) (?:merchant )?category",
        holds="category_freq < 0.05",
    ),
]
FR_ALIASES = {
    "amt_vs_usual": ["usual", "usual amount", "card's usual"],
    "card_history_n": ["card history", "earlier purchases", "history"],
}
_FR = [
    (
        "Amount is 11.49 times the card's usual, which is extreme spend.",
        {"amount": 863.23, "amt_vs_usual": 11.491347177848775, "card_history_n": 387},
        "verified",
    ),
    (
        "Amount is 11.49 times the card's usual, which is extreme spend under P3.9.",
        {"amount": 863.23, "amt_vs_usual": 11.491347177848775, "card_history_n": 387},
        "unverifiable",  # "under" no binding used
    ),
    (
        "merchant_first_seen is false, so the merchant is known to the card, and card_history_n "
        "is 846, a settled history.",
        {"amount": 9.25, "card_history_n": 846, "merchant_first_seen": False},
        "verified",
    ),
    (
        "Evidence points the other way too: risk_score is 0.008176721945643811, "
        "merchant_first_seen is false and card_history_n is 2261.",
        {"risk_score": 0.008176721945643811, "card_history_n": 2261, "merchant_first_seen": False},
        "verified",
    ),
    (
        "Card has 1309 earlier purchases, so its usual pattern is well established",
        {"amt_vs_usual": 12.438720064399277, "card_history_n": 1309},
        "verified",
    ),
    (
        "Screening risk score is 0.005, well below the 0.9 no-clear threshold",
        {"risk_score": 0.005249293236036321},
        "unverifiable",
    ),
    (
        "Merchant is not a first-seen merchant and the card has 2366 earlier purchases",
        {"card_history_n": 2366, "merchant_first_seen": False},
        "unverifiable",
    ),
    (
        "Amount 18.19 is under 50 and the merchant is known to the card",
        {"amount": 18.19},
        "unverifiable",
    ),
    (
        "Amount is 18.21 times the card's usual, which is extreme spend",
        {"amt_vs_usual": 18.21505569529285},
        "unverifiable",
    ),
    (
        "Category entertainment is rare for this card (category_freq 0.08) over 517 earlier "
        "purchases",
        {"category_freq": 0.08, "card_history_n": 517},
        "unverifiable",
    ),
    (
        "category_freq is 0.073, so food_dining is a rare category for this card",
        {"category_freq": 0.07332900713822194},
        "contradicted",
    ),
]


@pytest.mark.parametrize(("reason", "record", "expected"), _FR, ids=[r[0][:40] for r in _FR])
def test_fraud_review_reasons_golden(reason, record, expected):
    res = value_check(reason, record, phrases=FR_PHRASES, aliases=FR_ALIASES)
    assert res.details["outcome"] == expected, res.details


# ================================ D-72 output shape =======================================


def test_d72_output_shape_and_shares(registry):
    rec = {"amount": 812.4, "risk_score": 0.83}
    both = value_check("Amount above 900; the risk score is about 0.5", rec)
    assert both.details["outcome"] == "contradicted" and both.value == 0.0
    assert [c["status"] for c in both.details["checks"]] == ["failed", "unbound"]
    assert both.details["failed"] == ("900",) and both.details["unverified"] == ("0.5",)
    check = both.details["checks"][0]
    assert (check["field"], check["op"], check["claimed"], check["found"]) == (
        "amount",
        ">",
        900.0,
        812.4,
    )
    s = value_check_scorer(name="three_outcomes", record_key="alert")
    texts = ["Amount above 800", "Amount is roughly 800", "Amount above 900", "Routine"]
    res = s(case({"alert": rec}), completion(json.dumps({"reasons": texts})))
    assert res.value == 0.0 and res.details["outcome"] == "contradicted"
    assert res.details["counts"] == {"verified": 1, "contradicted": 1, "unverifiable": 2}
    assert res.details["shares"] == {"verified": 0.25, "contradicted": 0.25, "unverifiable": 0.5}
    assert res.details["n_reasons"] == 4
    assert [r["n_checked"] for r in res.details["reasons"]] == [1, 1, 1, 0]
    assert {c["reason_index"] for c in res.details["checks"]} == {0, 1, 2}
    unsure = s(case({"alert": rec}), completion(json.dumps({"reasons": texts[:2]})))
    assert unsure.value == 0.0 and unsure.details["outcome"] == "unverifiable"
    sure = s(case({"alert": rec}), completion(json.dumps({"reasons": texts[:1]})))
    assert sure.value == 1.0 and sure.details["outcome"] == "verified"
    plain = s(case({"alert": rec}), completion("Amount above 800."))
    assert plain.value == 1.0
    empty = s(case({"alert": rec}), completion(json.dumps({"reasons": []})))
    assert empty.details["outcome"] == "unverifiable" and empty.details["shares"] is None
    refused = s(case({"alert": rec}), completion(error="refusal"))
    assert refused.value == 0.0 and "outcome" not in refused.details
    one = value_check("Amount above 900", rec)
    shares = value_check_shares([res, unsure, sure, plain, empty, refused, one])
    assert (shares.n_reasons, shares.verified, shares.contradicted, shares.unverifiable) == (
        9,
        4,
        2,
        3,
    )
    assert shares.nothing_checked == 1 and shares.n_skipped == 1
    assert shares.contradicted_share is not None
    assert shares.contradicted_share.lo < 2 / 9 < shares.contradicted_share.hi
    with pytest.raises(ValueError):
        value_check_shares([], alpha=1.0)
    with pytest.raises(ValueError):
        value_check_shares([ScoreResult(value=0.0, details={"outcome": "maybe", "n_checked": 1})])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"units": {"risk_score": "ratio"}},
        {"units": {"risk_score": None}},
        {"aliases": {"amount": "spend"}},
        {"aliases": {"amount": [" "]}},
        {"constants": "5"},
        {"constants": [float("nan")]},
        {"ignore": r"\d+"},
        {"ignore": ["("]},
        {"tolerance": -0.1},
    ],
)
def test_options_are_validated(kwargs, registry):
    with pytest.raises(ValueError):
        value_check("amount is 812.40", REC, **kwargs)
    with pytest.raises(ValueError):
        value_check_scorer(name="bad_options", **kwargs)


# ================================ robustness =============================================


@pytest.mark.safety
def test_never_raises_on_record_contents():
    """verify513c/d fuzzers: odd record values never raise and never verify their claim."""
    odd: list[Any] = ["812", "", "yes", None, float("nan"), float("inf"), -float("inf")]
    odd += [10**400, -(10**400), 10**20, [1], {"a": 1}, [], {}, True, False, (1,), b"3"]
    odd += [Decimal("3"), Decimal("NaN"), Fraction(1, 3), complex(1, 2), object(), set(), 0, -0.0]
    reason = (
        "$812 purchase, 9x usual spend, at night, new merchant, known merchant, rare category, "
        "thin history, high risk score, over 80% risk, 40 transactions, not above 0.5, 3 or "
        "more, 1e308, amount is 812.40, card_history_n is 40, merchant_first_seen is true"
    )
    extra = [
        ValuePhrase(pattern="combo", holds="hour < 5 and risk_score >= 0.7"),
        ValuePhrase(pattern="eq", holds="category == 'shopping_net'"),
        ValuePhrase(pattern="flag", holds="merchant_first_seen and hour < 5"),
    ]
    rnd = random.Random(2)
    base = SEV["record"]
    for k in base:
        for v in odd:
            value_check(reason, {**base, k: v}, phrases=PHRASES, units=UNITS)
    for _ in range(1500):
        rec = {k: rnd.choice([*odd, base[k]]) for k in base}
        value_check(reason + " combo eq flag", rec, phrases=PHRASES + extra, units=UNITS)


@pytest.mark.safety
def test_random_token_soup_never_raises():
    """verify513g/fz.py: random reasons from tricky tokens over odd records."""
    rnd = random.Random(3)
    tokens = [
        "amount",
        "risk score",
        "card history",
        "hour",
        "amt vs usual",
        "usual",
        "card",
        "risk",
        "history",
        "above",
        "below",
        "not",
        "no",
        "never",
        "n't",
        "at least",
        "more than",
        "or less",
        "between",
        "and",
        "to",
        "x",
        "times",
        "fold",
        "%",
        "percent",
        "$",
        "USD",
        "10 p.m.",
        "6am",
        "22:00",
        "midnight",
        "noon",
        "threshold",
        "limit",
        "of",
        "the",
        "rule 5.9",
        "P7.1",
        ",",
        ";",
        ".",
        "!",
        "?",
        "(",
        ")",
        "—",
        "-",
        "which",
        "is",
        "was",
        "not true",
        "untrue",
        "0",
        "-3",
        "1e309",
        "812.40",
        "0.83",
        "9.1x",
        "3",
        "40",
        "1,234.5",
        "∞",
        "NaN",
        "½",
        "٣",
        "3:61",
        "25:00",
        "13 p.m.",
        "0 a.m.",
        "..",
        "\n",
        "   ",
        "é",
        "ß",
        "İ",
        "true",
        "false",
        "respectively",
    ]
    values: list[Any] = [None, 0, 1, -1, 3, 0.83, 812.4, float("nan"), float("inf"), 10**400]
    values += ["x", True, False, [1], {"a": 1}, "", "3", 1e-300, -0.0]
    phrases = [
        ValuePhrase(pattern=r"at night", holds="hour >= 22 or hour < 6"),
        ValuePhrase(pattern=r"\d+(?:\.\d+)?\s*x\s+usual", field="amt_vs_usual"),
        ValuePhrase(pattern=r"new merchant", holds="merchant_first_seen"),
    ]
    keys = ["amount", "risk_score", "card_history_n", "hour", "amt_vs_usual"]
    for _ in range(3000):
        text = " ".join(rnd.choice(tokens) for _ in range(rnd.randrange(1, 40)))
        rec = {k: rnd.choice(values) for k in [*keys, rnd.choice(["ts_hour", "x", "vs", "a_b"])]}
        value_check(
            text,
            rec,
            phrases=phrases,
            aliases={"amt_vs_usual": ["usual"]},
            units=UNITS,
            constants=[5, 10, 0.9],
            ignore=[r"\bref\s+\d+"],
        )


@pytest.mark.parametrize(
    "unit",
    [
        "Amount above $800 is not true, risk score 0.83 (not 0.9); between 10 p.m. and 6 a.m. "
        "card history 40 or more times usual 9.1x. ",
        "1 2 3 4 5 6 7 8 9 10 ",
        "amount risk score card history hour usual 812.40 0.83 ",
        "amount is 812.40 and risk score is 0.83 and hour is 3 and ",
        "- amount: 812.40\n",
        "9" * 150 + " ",
    ],
)
def test_reading_is_linear(unit):
    def run(reps: int) -> float:
        t0 = time.perf_counter()
        value_check(unit * reps, REC, phrases=PHRASES, aliases=ALIASES, units=UNITS)
        return time.perf_counter() - t0

    run(10)
    small, big = min(run(50) for _ in range(3)), min(run(400) for _ in range(3))
    assert big < 8 * small * 3 + 0.05, (small, big)  # 8x the text: well under quadratic


def test_value_check_is_deterministic():
    """Same reason and record, same result: key order and phrase round trips change nothing
    (repro)."""
    shuffled = dict(reversed(list(REC.items())))
    reason = "A $812.40 purchase, 9x usual spend, at night, risk score is 0.83, about 40"
    first = value_check(reason, REC, phrases=PHRASES, aliases=ALIASES)
    assert first == value_check(reason, shuffled, phrases=PHRASES, aliases=ALIASES)
    again = [ValuePhrase.model_validate_json(p.model_dump_json()) for p in PHRASES]
    assert first == value_check(reason, REC, phrases=again, aliases=ALIASES)
    assert first.details["outcome"] == "unverifiable"  # "about 40" binds nothing


# ================================ fraud-review-bedrock D-42 false claims =================

# fraud-review-bedrock's strict checker (its D-42, tests/test_valuecheck.py, MIT, same author):
# its record, its hand-written false and tricky claims, and a config with its vocabulary.
D42_REC: dict[str, Any] = {
    "ts_hour": 22,
    "category": "travel",
    "amount": 11.01,
    "risk_score": 0.0386,
    "amt_vs_usual": 0.2134,
    "hour": 22,
    "category_freq": 0.038,
    "merchant_first_seen": False,
    "card_history_n": 1545,
}
D42 = {
    "phrases": [
        ValuePhrase(
            pattern=r"\d[\d.,]*\s*(?:x|×|times)\s+(?:\S+\s+){0,3}?(?:usual|normal|typical|median)",
            field="amt_vs_usual",
        ),
        ValuePhrase(
            pattern=r"\bfirst (?:purchase|time|use|transaction|visit) at (?:this|the) merchant\b"
            r"|\bnew merchant\b|\bmerchant is new\b",
            holds="merchant_first_seen",
        ),
        ValuePhrase(
            pattern=r"\bknown merchant\b|\bmerchant is known\b|\bknown to the card\b",
            holds="not merchant_first_seen",
        ),
    ],
    "aliases": {
        "amount": ["purchase amount"],
        "risk_score": ["risk score", "fraud score", "screening score"],
        "card_history_n": ["card history", "earlier purchases", "prior transactions"],
    },
    "units": {"risk_score": "fraction", "category_freq": "fraction", "amount": "currency"},
    "unreadable": [
        r"\b(?:rare\w*|uncommon|unusual\w*|infrequent\w*|seldom|occasional\w*)\b",
        r"\bmerchant\b|\bbrand[- ]new\b|\bfirst[- ]time\b|\bunfamiliar\b|\bnew\b",
    ],
}


@pytest.mark.safety
@pytest.mark.parametrize(
    "reason",
    [
        "Amount is 12.01",
        "Amount is not 11.01",
        "Amount is 11.01 times the card's usual",
        "Amount is 0.2134 times the card's usual, not 11.01",
        "Hour is 21",
        "Purchase hour is 9 p.m.",
        "Hour is 22 (10 a.m.)",
        "category_freq is 0.38",
        "category_freq is 38%",
        "category_freq is 0.38%",
        "category_freq is 3.8",
        "category_freq is not below 0.05 so it is not rare",
        "The card has 1546 earlier purchases",
        "The card has no earlier purchases",
        "First purchase at this merchant",
        "The merchant is new",
        "The merchant is known to the card and it is the first purchase at this merchant",
        "Amount is 11.01 and the amount is 12.01",
        "Amount is 11.01 respectively 12.01",
        "amount is above 11.01",
        "risk score is above 0.0386",
        "Risk score is 3.86",
        "amount 11.01 is 0.21 times the card's usual",
        "Amount is 0.21 times the card's usual, above the 5x fraud signal level",
        "Amount is 11.01 under P3.1",
        "amt_vs_usual is 0.21 P3.1",
        "Amount is 11.01 or more",
        "category_freq of 0.038 or less",
        "the card's usual amount of 11.01",
        "average amount is 11.01",
        "Amount is 11.01 at hour 22",
        "Amount is 0.2 times the card's usual",
        "merchant is brand new, amount 11.01",
        "first-time merchant, amount 11.01",
        "merchant: new, amount 11.01",
        "amount is 11.01 at an unfamiliar merchant",
        "category_freq is 0.038, so travel is a rare category for this card",
        "Amount is 11.01, which is well above the card's norm",
        "Amount is 11.01, twice the limit",
        "P7.1 needs a category the card uses often and a merchant that is not new",
    ],
)
def test_fraud_review_d42_false_and_tricky_claims_are_never_verified(reason):
    assert value_check(reason, D42_REC, **D42).details["outcome"] != "verified"


def test_fraud_review_d42_true_claims_still_verify():
    for reason in (
        "Amount is 11.01",
        "The purchase amount of $11.01",
        "amount is under 50",
        "Amount is 0.21 times the card's usual",
        "amt_vs_usual is 0.213",
        "risk score is 3.9%",
        "risk score is below 0.9",
        "category_freq 0.038",
        "Card has 1545 earlier purchases",
        "card_history_n is 1545",
        "hour 22",
        "merchant_first_seen is false",
    ):
        assert value_check(reason, D42_REC, **D42).details["outcome"] == "verified", reason


def test_unreadable_claims_void_the_reason_unless_a_phrase_reads_them():
    rare = [r"\brare\w*\b"]
    phrase = ValuePhrase(pattern=r"rare category", holds="category_freq < 0.05")
    assert outcome("category freq is 0.05, a rare pick") == "verified"
    assert outcome("category freq is 0.05, a rare pick", unreadable=rare) == "unverifiable"
    got = outcome("category freq is 0.05. A rare category", unreadable=rare, phrases=[phrase])
    assert got == "contradicted"  # read by the phrase: 0.05 is not below 0.05
