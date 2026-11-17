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
    PLANNING_CLAIMS,
    WORDS_PER_CLAIM,
    ClaimsAssumption,
    TokenAssumptions,
    claims_from_texts,
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


def test_fraud_review_claims_are_counted_within_25_percent_where_the_default_was_2x_low():
    items, true = fraud_items()
    old = PLANNING_CLAIMS.calls(len(items))  # what fraud-review planned: 8 x 4.5
    new = claims_from_texts([i.explanation for i in items])
    assert (old, true) == (36, 96) and true / old >= 2  # the default under-planned >2x
    assert new.basis == "texts" and new.calls(len(items)) == 85
    assert abs(new.calls(len(items)) - true) <= 0.25 * true
    assert new.source.startswith("from the texts: 843 words → 84.3 claims in 8 items at 10 words")


@pytest.mark.parametrize("live", [True, False])
def test_faithfulness_estimate_plans_judging_from_the_explanations(live):
    items, true = fraud_items()
    est = estimate_faithfulness_cost(items, config=CONFIG, prices=PRICES, live=live)
    assert est.claims.basis == "texts" and "not an upper bound" in est.claims.describe()
    judge_lines = [ln for ln in est.lines if ln.node == "judge"]
    assert [ln.model for ln in judge_lines] == list(CONFIG.judges)
    for ln in judge_lines:
        assert abs(ln.calls - true) <= 0.25 * true
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
    assert c.per_item == pytest.approx((12 + 4.5 + 4.5) / 3)
    assert c.total == pytest.approx(21.0) and c.calls(3) == 21 and c.raised == 2
    assert c.words == 121 and c.words_per_claim == WORDS_PER_CLAIM == 10.0
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
    texts = ["word " * 90] * 4  # 4 saved answers (2 models x 2 cases), ~9 claims each
    est = estimate_run_cost(
        PRICES,
        models=["claude-sonnet-5-5", "gpt-6.1-sol"],
        extractor="claude-haiku-5-5",
        judges=["claude-opus-5-5"],
        n_cases=2,
        claims=claims_from_texts(texts),
    )
    assert [ln.calls for ln in est.lines if ln.node == "judge"] == [36]
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
    assert record["replay"]["words_per_claim"] == pytest.approx(12.2)
    # Conservative: below the lower quartile, so texts plan for more claims, not fewer.
    assert report["per_answer_words_per_claim_quartiles"][0] >= WORDS_PER_CLAIM
    assert record["planning_words_per_claim"] == WORDS_PER_CLAIM


def test_on_real_pubmedqa_answers_a_text_count_errs_high_not_low():
    data = json.loads((ROOT / "demo" / "replay.json").read_text(encoding="utf-8"))
    answers = [(e["answer"], e["n_claims"]) for v in data.values() for e in v]
    actual = sum(n for _, n in answers)
    planned = claims_from_texts([a for a, _ in answers]).calls(len(answers))
    assert (actual, planned) == (105, 132)
    assert actual <= planned <= 1.3 * actual


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
    for bad in ("0", "-1", "nan", "x"):
        with pytest.raises(SystemExit) as exc:
            dry("--claims-per-answer", bad)
        assert exc.value.code == 2


def test_default_judge_calls_are_still_the_ceiling_of_answers_times_4_5():
    for n in (1, 3, 7, 200):
        est = estimate_for_config(PRICES, CONFIG, n_cases=n)
        assert [ln.calls for ln in est.lines if ln.node == "judge"] == [math.ceil(n * 4.5)] * 2
        full = est.model_dump_json().encode()
        without = est.model_dump_json(exclude={"claims"}).encode()
        assert hashlib.sha256(full).digest() == hashlib.sha256(without).digest()
