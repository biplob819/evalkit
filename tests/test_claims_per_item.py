"""Claims per item behind every judge estimate (fraud-review-bedrock, D-61 rule 3).

The failing case: fraud-review-bedrock planned faithfulness on its explanations with the
planning default of 4.5 claims per item (measured on PubMedQA benchmark answers), but its
explanations hold about 12 claims each, so the judge estimate was well under half the real
cost and the run passed its cap. The fixture is synthetic, in fraud-review's shape: eight
reviewer explanations, each written from 12 atomic claims listed beside it.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import pytest

from evalkit import cli
from evalkit import run as R
from evalkit.cost import (
    CHARS_PER_CLAIM,
    MAX_CLAIMS_PER_ITEM,
    PLANNING_CLAIMS,
    WORDS_PER_CLAIM,
    ClaimsAssumption,
    TokenAssumptions,
    claim_bounds,
    claims_from_texts,
    claims_in_text,
    count_chars,
    count_words,
    estimate_for_config,
    estimate_run_cost,
    load_prices,
    measured_claims,
    planning_claims,
)
from evalkit.datasets.pubmedqa import parse_pqa_l
from evalkit.schemas import RunConfig
from evalkit.scorers.faithfulness import FaithfulnessItem, estimate_faithfulness_cost
from tests.golden.test_golden_run import MINI

ROOT = Path(__file__).resolve().parent.parent
FRAUD = ROOT / "tests" / "fixtures" / "faithfulness" / "fraud_review_synthetic.json"
PRICES = load_prices()
CONFIG = RunConfig(
    models=("claude-sonnet-5-5",),
    judges=("claude-opus-5-5", "gpt-6.1-sol"),
    extractor="claude-haiku-5-5",
)


def fraud_items() -> tuple[list[FaithfulnessItem], int]:
    raw = json.loads(FRAUD.read_text(encoding="utf-8"))["items"]
    items = [FaithfulnessItem(**{k: v for k, v in r.items() if k != "claims"}) for r in raw]
    return items, sum(len(r["claims"]) for r in raw)


# ---- the failing case ------------------------------------------------------------------------


def test_fraud_review_fixture_is_twelve_claims_per_explanation():
    raw = json.loads(FRAUD.read_text(encoding="utf-8"))
    assert raw["_source"].startswith("SYNTHETIC")
    assert [len(r["claims"]) for r in raw["items"]] == [12] * 8


def test_fraud_review_claims_are_planned_at_or_above_the_true_count():
    items, true = fraud_items()
    old = PLANNING_CLAIMS.calls(len(items))  # what fraud-review planned: 8 x 4.5
    new = claims_from_texts([i.explanation for i in items])
    assert (old, true) == (36, 96) and true / old >= 2  # the default under-planned >2x
    # The first D-76 rule (words / 10) planned 85, 11% low; at 8 words per claim it is 106.
    first = math.ceil(sum(max(count_words(i.explanation) / 10, 4.5) for i in items))
    assert first == 85 < true
    assert new.basis == "texts" and new.calls(len(items)) == 106
    assert true <= new.calls(len(items)) <= 1.25 * true
    assert new.source.startswith(
        "from the texts: 105.8 claims in 8 items (843 words), each item at the largest of "
        "words ÷ 8, sentence-like units, key=value fields, distinct numbers, characters ÷ 50 "
        "and 4.5; 1 raised above words ÷ 8 (1 by sentence-like units)"
    )


@pytest.mark.parametrize("live", [True, False])
def test_faithfulness_estimate_plans_judging_from_the_explanations(live):
    items, true = fraud_items()
    est = estimate_faithfulness_cost(items, config=CONFIG, prices=PRICES, live=live)
    assert est.claims.basis == "texts" and "not an upper bound" in est.claims.describe()
    judge_lines = [ln for ln in est.lines if ln.node == "judge"]
    assert [ln.model for ln in judge_lines] == list(CONFIG.judges)
    for ln in judge_lines:
        assert true <= ln.calls <= 1.25 * true
    extract = [ln for ln in est.lines if ln.node == "extract_faithfulness_claims"]
    assert len(extract) == 1 and extract[0].calls == len(items)
    assert est.total_usd == pytest.approx(sum(ln.usd for ln in est.lines))
    # The old plan (4.5 claims per item) put judging at well under half of this one's.
    old = estimate_faithfulness_cost(
        items,
        config=CONFIG,
        prices=PRICES,
        live=live,
        claims_per_item=PLANNING_CLAIMS.per_item,
    )
    judged = sum(ln.usd for ln in judge_lines)
    assert sum(ln.usd for ln in old.lines if ln.node == "judge") < 0.5 * judged


def test_faithfulness_judge_prompts_are_sized_from_each_items_evidence():
    small = FaithfulnessItem(id="a", question="q?", explanation="One claim.", evidence=("e.",))
    big = small.model_copy(update={"evidence": tuple(f"Sentence {i} " * 20 for i in range(30))})
    usd = [
        estimate_faithfulness_cost([it], config=CONFIG, prices=PRICES).lines[1].usd
        for it in (small, big)
    ]
    assert usd[1] > 3 * usd[0]  # every claim is judged against all of the evidence (FTH-02)


def test_a_measured_override_is_used_as_given_and_says_where_it_came_from():
    items, _ = fraud_items()
    est = estimate_faithfulness_cost(
        items,
        config=CONFIG,
        prices=PRICES,
        claims_per_item=12.25,
        claims_source="20-item paid sample, 2026-11-17",
    )
    assert est.claims.basis == "measured" and est.claims.per_item == 12.25
    assert all(ln.calls == 98 for ln in est.lines if ln.node == "judge")
    assert est.claims.describe() == (
        "claims per item: measured override 12.25 claims per item (20-item paid sample, "
        "2026-11-17); an expected count, not an upper bound"
    )
    assert '"basis":"measured"' in est.model_dump_json()


def test_faithfulness_estimate_needs_items():
    with pytest.raises(ValueError, match="no items"):
        estimate_faithfulness_cost([], config=CONFIG, prices=PRICES)


# ---- counting claims from texts --------------------------------------------------------------


def test_each_text_is_at_least_the_planning_default_and_the_sum_is_kept():
    c = claims_from_texts(["word " * 120, "Short.", ""])
    assert c.per_item == pytest.approx((15 + 4.5 + 4.5) / 3)
    assert c.total == pytest.approx(24.0) and c.calls(3) == 24 and c.raised == 2
    assert c.words == 121 and c.words_per_claim == WORDS_PER_CLAIM == 8.0
    assert c.chars_per_claim == CHARS_PER_CLAIM == 50.0
    assert "2 raised above words ÷ 8 (2 by the default)" in c.source
    with pytest.raises(ValueError, match="3 texts, not 4"):
        c.calls(4)
    with pytest.raises(ValueError):
        claims_from_texts([])
    assert claims_from_texts(["a " * 30], default=1.0, words_per_claim=5).calls(1) == 6


def test_words_are_counted_in_scripts_without_spaces():
    assert count_words("The amount is $1,240.00, 9.1x usual.") == 6
    assert count_words("日本語のテキスト abc") == 9  # each CJK character, then "abc"
    assert count_words("") == 0


def test_saved_answers_can_be_rejudged_at_their_counted_claims():
    texts = ["word " * 90] * 4  # 4 saved answers (2 models x 2 cases), 90 / 8 claims each
    est = estimate_run_cost(
        PRICES,
        models=["claude-sonnet-5-5", "gpt-6.1-sol"],
        extractor="claude-haiku-5-5",
        judges=["claude-opus-5-5"],
        n_cases=2,
        claims=claims_from_texts(texts),
    )
    assert [ln.calls for ln in est.lines if ln.node == "judge"] == [45]
    assert est.claims.basis == "texts"
    with pytest.raises(ValueError, match="4 texts, not 6"):
        estimate_run_cost(
            PRICES,
            models=["claude-sonnet-5-5", "gpt-6.1-sol", "gpt-6-luna"],
            extractor="claude-haiku-5-5",
            judges=["claude-opus-5-5"],
            n_cases=2,
            claims=claims_from_texts(texts),
        )


# ---- the ratio and its source ----------------------------------------------------------------


def test_the_ratio_record_matches_evalkits_committed_extraction_data():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import measure_words_per_claim as m
    finally:
        sys.path.pop(0)
    record = json.loads(m.OUT.read_text(encoding="utf-8"))
    assert m.measure() == record
    report = record["report"]
    assert (report["answers_with_claims"], report["words"], report["claims"]) == (553, 31744, 2663)
    assert report["words_per_claim"] == pytest.approx(11.92, abs=0.01)
    assert report["chars_per_claim"] == pytest.approx(69.55, abs=0.01)
    assert record["replay"]["words_per_claim"] == pytest.approx(12.2)
    # Both ratios are the report's 10th percentile of per-answer ratios, rounded down.
    assert WORDS_PER_CLAIM == math.floor(report["per_answer_words_per_claim_p10"]) == 8
    assert CHARS_PER_CLAIM == math.floor(report["per_answer_chars_per_claim_p10"]) == 50
    assert record["planning_words_per_claim"] == WORDS_PER_CLAIM
    assert record["planning_chars_per_claim"] == CHARS_PER_CLAIM
    # The fraud-review-shaped fixture is denser than the report, and is still planned above.
    assert record["fraud_fixture"]["words_per_claim"] == pytest.approx(8.78, abs=0.01)
    assert record["fraud_fixture"]["words_per_claim"] > WORDS_PER_CLAIM


def test_the_whole_rule_under_plans_far_fewer_report_answers_than_the_first():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import measure_words_per_claim as m
    finally:
        sys.path.pop(0)
    record = json.loads(m.OUT.read_text(encoding="utf-8"))
    now, first = record["plan_check"], record["plan_check_first_rule"]
    # Report: answers planned below their true count, 18.6% -> 4.2%, at 1.53x the true total.
    assert first["report"]["under_planned_share"] == pytest.approx(0.186)
    assert now["report"]["under_planned_share"] == pytest.approx(0.042)
    assert now["report"]["planned_over_true"] == pytest.approx(1.527)
    # No source is under-quoted in total any more; the fixture was 12% low.
    assert first["fraud_fixture"]["planned_over_true"] < 1
    for name in ("report", "replay", "fraud_fixture"):
        assert 1 <= now[name]["planned_over_true"] <= 1.6


def test_on_real_pubmedqa_answers_a_text_count_errs_high_not_low():
    data = json.loads((ROOT / "demo" / "replay.json").read_text(encoding="utf-8"))
    answers = [(e["answer"], e["n_claims"]) for v in data.values() for e in v]
    actual = sum(n for _, n in answers)
    planned = claims_from_texts([a for a, _ in answers]).calls(len(answers))
    assert (actual, planned) == (105, 162)
    assert actual <= planned <= 1.6 * actual


# ---- the PubMedQA benchmark estimate is unchanged --------------------------------------------


def test_benchmark_estimate_keeps_the_planning_default_and_its_bytes():
    est = estimate_for_config(
        PRICES,
        CONFIG.model_copy(update={"models": ("claude-sonnet-5-5", "gpt-6.1-sol")}),
        n_cases=200,
    )
    assert est.claims == PLANNING_CLAIMS and est.claims.basis == "default"
    assert est.claims.describe() == (
        "claims per item: planning default 4.5, PubMedQA answers (D-46, measured 4.46); "
        "an expected count, not an upper bound"
    )
    assert "claims" not in est.model_dump() and '"claims"' not in est.model_dump_json()
    assert [ln.calls for ln in est.lines if ln.node == "judge"] == [1800, 1800]
    assert planning_claims(TokenAssumptions()) is PLANNING_CLAIMS
    override = planning_claims(TokenAssumptions(claims_per_answer=6))
    assert override.basis == "override" and override.calls(10) == 60


def test_claims_assumption_round_trips():
    for c in (PLANNING_CLAIMS, measured_claims(7.5, "x"), claims_from_texts(["a b c"])):
        assert ClaimsAssumption.model_validate_json(c.model_dump_json()) == c


# ---- the dry run states it -------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # never read the repo's real .env
    rows = json.loads(MINI.read_text(encoding="utf-8"))
    monkeypatch.setattr(R, "load_pqa_l", lambda: parse_pqa_l(rows))
    (tmp_path / "evalkit.yaml").write_text(
        "models: [claude-sonnet-5-5, gpt-6.1-sol]\njudges: [claude-opus-5-5, gpt-6.1-sol]\n"
        "extractor: claude-haiku-5-5\nseed: 7\n",
        encoding="utf-8",
    )
    return tmp_path


def dry(*argv: str) -> int:
    return cli.main(["run", "evalkit.yaml", "--n", "12", "--dry-run", *argv])


def test_dry_run_prints_the_claims_assumption(project, capsys):
    assert dry() == 0
    out = capsys.readouterr().out
    assert "claims per item: planning default 4.5, PubMedQA answers" in out
    assert "not an upper bound" in out and "no API calls" in out
    assert dry("--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"]["basis"] == "default" and "claims" not in payload["estimate"]
    est = R.estimate(
        R.plan_run(Path("evalkit.yaml"), task="pubmedqa", n=12, out=Path("runs")),
        PRICES,
        live_judges=False,
    )
    assert payload["estimate"] == json.loads(est.model_dump_json())


def test_dry_run_takes_a_measured_claims_per_answer(project, capsys):
    assert dry("--claims-per-answer", "12", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"]["basis"] == "measured"
    assert payload["estimate"]["claims"]["source"].startswith("measured override 12 claims")
    calls = [ln["calls"] for ln in payload["estimate"]["lines"] if ln["node"] == "judge"]
    assert calls == [2 * 12 * 12] * 2  # 2 models x 12 cases x 12 claims, per judge
    assert dry("--claims-per-answer", "12") == 0
    assert "measured override 12 claims per item (--claims-per-answer)" in (capsys.readouterr().out)
    for bad in ("0", "-1", "nan", "inf", "x", "1e308", "10001"):
        with pytest.raises(SystemExit) as exc:
            dry("--claims-per-answer", bad)
        assert exc.value.code == 2
    assert "more than 10,000 claims per answer" in capsys.readouterr().err
    assert dry("--claims-per-answer", "10000") == 0  # the maximum itself is accepted


def test_default_judge_calls_are_still_the_ceiling_of_answers_times_4_5():
    for n in (1, 3, 7, 200):
        est = estimate_for_config(PRICES, CONFIG, n_cases=n)
        assert [ln.calls for ln in est.lines if ln.node == "judge"] == [math.ceil(n * 4.5)] * 2
        full = est.model_dump_json().encode()
        without = est.model_dump_json(exclude={"claims"}).encode()
        assert hashlib.sha256(full).digest() == hashlib.sha256(without).digest()


# ---- dense texts the length rule under-planned (D-76 verifier cases) -------------------------

# Each text with its true claim count (by construction). Under the first rule (words / 10, at
# least 4.5) every one of them was planned below its true count. Now each is planned at or
# above it, and at most OVER_PLAN times it.
OVER_PLAN = 1.5
DENSE = {
    "one-word sentences": (
        "Fraud. Confirmed. Card. Stolen. Refund. Denied. Merchant. Flagged. Account. Frozen. "
        "Police. Notified.",
        12,
    ),
    "ten numeric facts": (
        "Amount 420. Velocity 12. MCC 5999. BIN 411111. Score 0.91. Age 3 days. Attempts 4. "
        "Distance 8800 km. Refunds 2. Chargebacks 1.",
        10,
    ),
    "numeric facts in one sentence": (
        "Amount 420, velocity 12, MCC 5999, BIN 411111, score 0.91, age 3 days, attempts 4, "
        "distance 8800 km, refunds 2 and chargebacks 1",
        10,
    ),
    "semicolon-separated figures": (
        "Q3 rev $4.2M; Q2 $3.9M; margin 41%; churn 2.1%; NPS 62; ARR $17M; 340 staff; "
        "12 offices; 8 regions; 3 products.",
        10,
    ),
    "csv without spaces": (
        "amount=420,country=NG,ip=1.2.3.4,device=new,velocity=12,mcc=5999,cvv=fail,avs=fail,"
        "3ds=none,bin=411111",
        10,
    ),
    "json fields": (
        '{"amount": 420, "country": "NG", "device": "new", "cvv": "fail", "avs": "fail", '
        '"ds3": "none"}',
        6,
    ),
    "thai, spaces between phrases only": (
        "ธุรกรรมนี้เป็นการฉ้อโกง บัตรถูกขโมย ร้านค้าถูกตั้งค่าสถานะ บัญชีถูกระงับ "
        "ตำรวจได้รับแจ้งแล้ว ลูกค้าปฏิเสธการทำธุรกรรมทั้งหมดสามรายการ",
        6,
    ),
    "30 bullets": ("\n".join(f"- flag {i}: yes" for i in range(30)), 30),
    "30 bullets on one line": (" • ".join(f"flag {i} set" for i in range(30)), 30),
}


@pytest.mark.parametrize("name", list(DENSE))
def test_dense_texts_are_planned_at_least_at_their_true_count(name):
    text, true = DENSE[name]
    assert max(count_words(text) / 10, 4.5) < true  # the first rule under-planned it
    planned = claims_in_text(text)
    assert true <= planned <= OVER_PLAN * true, claim_bounds(text)
    c = claims_from_texts([text])
    assert c.total == planned and c.calls(1) >= true and c.raised == 1


def test_all_dense_texts_together_are_not_under_planned():
    texts = [t for t, _ in DENSE.values()]
    true = sum(n for _, n in DENSE.values())
    c = claims_from_texts(texts)
    assert true <= c.calls(len(texts)) <= OVER_PLAN * true
    assert c.raised == len(texts)


def test_each_bound_counts_what_it_says():
    b = claim_bounds("Card stolen; merchant flagged.\nAccount frozen at 03:12, risk 0.91.")
    assert b["units"] == 3 and b["numbers"] == 2 and b["fields"] == 0
    assert claim_bounds("a=1, b=2, c: 3")["fields"] == 3
    assert claim_bounds("Time 03:12 and https://example.com/x")["fields"] == 0
    assert claim_bounds("1. one\n2. two\n3. three")["units"] == 3
    # NFKC: full-width digits are the same number as ASCII ones
    assert claim_bounds("１２３ and 123")["numbers"] == 1
    assert count_chars("a b\tc\n") == 3 and count_chars("ｆｕｌｌ") == 4
    # a script without spaces is counted by characters as well
    assert claim_bounds("ก" * 500)["chars"] == 500 / CHARS_PER_CLAIM == 10
    assert claims_in_text("ก" * 500) == 10
    # long words (compounds, identifiers) count by characters too
    assert claims_in_text("Kreditkartenbetrugsverdachtsmeldungsbearbeitung" * 10) >= 9


def test_ordinary_prose_is_still_planned_by_its_words():
    text = (
        "The trial randomized adults with poorly controlled diabetes to the drug or to placebo, "
        "and the drug lowered HbA1c more than placebo did, with no increase in serious adverse "
        "events. Weight fell in both groups, so the authors conclude that the drug is a safe "
        "addition to metformin for patients who are not at target."
    )
    b = claim_bounds(text)
    assert max(b.values()) == b["words"] == count_words(text) / WORDS_PER_CLAIM
    assert claims_in_text(text) == b["words"] and b["numbers"] == 0  # HbA1c is a name
    # numbers count when there are more distinct figures than words / 8 suggests
    figures = claim_bounds("HbA1c fell 0.8 points; 240 adults, 24 weeks, weight -1.9 kg.")
    assert figures["numbers"] == 4


# ---- invalid figures are errors, never a silent plan -----------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -5.0, math.nan, math.inf, 1e308, MAX_CLAIMS_PER_ITEM + 1])
def test_bad_defaults_and_overrides_are_rejected(bad):
    with pytest.raises(ValueError, match="finite number above 0 and at most 10,000"):
        claims_from_texts(["a"], default=bad)
    with pytest.raises(ValueError, match="finite number above 0 and at most 10,000"):
        claims_in_text("a", default=bad)
    with pytest.raises(ValueError, match="finite number above 0 and at most 10,000"):
        measured_claims(bad)
    items, _ = fraud_items()
    with pytest.raises(ValueError, match="finite number above 0 and at most 10,000"):
        estimate_faithfulness_cost(items, config=CONFIG, prices=PRICES, claims_per_item=bad)
    with pytest.raises(ValueError):  # pydantic's ValidationError is a ValueError
        TokenAssumptions(claims_per_answer=bad)


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf])
def test_bad_ratios_are_rejected(bad):
    with pytest.raises(ValueError, match="words_per_claim must be a finite number above 0"):
        claims_from_texts(["a b"], words_per_claim=bad)
    with pytest.raises(ValueError, match="chars_per_claim must be a finite number above 0"):
        claims_from_texts(["a b"], chars_per_claim=bad)


def test_the_maximum_override_is_accepted_and_stays_finite():
    c = measured_claims(MAX_CLAIMS_PER_ITEM)
    assert c.calls(1_000_000) == 10_000_000_000
    assert claims_from_texts(["a"], default=MAX_CLAIMS_PER_ITEM).total == MAX_CLAIMS_PER_ITEM
    with pytest.raises(ValueError, match="must be a number"):
        measured_claims("12")  # type: ignore[arg-type]
