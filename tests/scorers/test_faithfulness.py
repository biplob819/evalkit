import asyncio
import json
import re
from pathlib import Path

import pytest

from evalkit.adapters import RawCompletion
from evalkit.cost import load_prices
from evalkit.runtime import Executor, Store
from evalkit.schemas import RunConfig
from evalkit.scorers.faithfulness import FaithfulnessItem, faithfulness
from tests.pipeline.fakes import ScriptedBatchAdapter

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "faithfulness" / "items.json"
CFG = RunConfig(
    models=("claude-sonnet-5-5",),
    judges=("claude-opus-5-5", "gpt-6.1-sol"),
    extractor="claude-haiku-5-5",
)
EXTRACT_RE = re.compile(r"\nAnswer: (.*)\n")  # the faithfulness extractor prompt (D-60)


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.casefold()).split()


def reply(req):
    """Extractor: one claim per sentence. Judge: supported only if every content word of the
    claim appears in the cited sentences (no outside knowledge)."""
    if req.model == "extractor":
        m = EXTRACT_RE.search(req.prompt)
        assert m is not None
        text = m.group(1)
        claims = [
            {"text": s.strip() + ".", "addresses_question": True}
            for s in text.split(".")
            if s.strip()
        ]
        return json.dumps({"claims": claims})
    claim = req.prompt.split("Claim: ", 1)[1].split("\n", 1)[0]
    shown = req.prompt.split("Cited source sentences:", 1)[1]
    sentences = re.findall(r"^\[(\d+)\] (.+)$", shown, re.M)
    words = {w for _, s in sentences for w in norm(s)}
    ok = all(w in words for w in norm(claim) if len(w) > 3)
    first = [int(n) for n, s in sentences if set(norm(claim)) & set(norm(s))][:1] or [1]
    return json.dumps(
        {
            "verdict": "supported" if ok else "unsupported",
            "evidence": first if ok else [],
            "quote": None,
            "rationale": "scripted",
        }
    )


def items():
    raw = json.loads(FIX.read_text(encoding="utf-8"))
    return [FaithfulnessItem(**{k: v for k, v in r.items() if k != "expected"}) for r in raw], raw


def score(tmp_path, its, *, live=True, fake=None):
    fake = fake or ScriptedBatchAdapter(reply)
    ex = Executor(Store(tmp_path))
    out = asyncio.run(
        faithfulness(
            its,
            executor=ex,
            config=CFG,
            adapters={"anthropic": fake, "openai": fake},
            prices=load_prices(),
            live=live,
            max_wait_s=1,
        )
    )
    return out, fake


# ---- FTH-01 / FTH-03 fixture ------------------------------------------------------------------


def test_fixture_scores_as_expected_and_reruns_free(tmp_path):
    its, raw = items()
    results, fake = score(tmp_path, its)
    for res, r in zip(results, raw, strict=True):
        exp = r["expected"]
        assert res.n_claims == exp["supported"] + exp["unsupported"]
        for judge in CFG.judges:
            supported = sum(c.verdicts[judge] == "supported" for c in res.claims)
            assert supported == exp["supported"], (res.id, judge)
            assert res.fully_faithful[judge] == (exp["unsupported"] == 0)
    assert results[1].score["claude-opus-5-5"] == pytest.approx(0.5)
    calls = len(fake.requests)
    again, _ = score(tmp_path, its, fake=fake)
    assert len(fake.requests) == calls and again == results  # cached


def test_every_evidence_sentence_counts_as_cited(tmp_path):
    its, _ = items()
    results, fake = score(tmp_path, its[:1])
    assert all(c.cited == (1, 2) for c in results[0].claims)  # judged against all evidence
    ext = next(r for r in fake.requests if r.model == "extractor")
    assert "Cited sentence numbers" not in ext.prompt  # but the extractor is not asked (D-60)


# ---- FTH-02 judging semantics -----------------------------------------------------------------


def test_outside_knowledge_is_unfaithful(tmp_path):
    its, _ = items()
    results, _ = score(tmp_path, its[2:])
    assert results[0].score == {"claude-opus-5-5": 0.0, "gpt-6.1-sol": 0.0}
    assert results[0].fully_faithful == {"claude-opus-5-5": False, "gpt-6.1-sol": False}


def test_an_explanation_with_no_claims_is_not_faithful(tmp_path):
    item = FaithfulnessItem(id="empty", question="q?", explanation="", evidence=("Some fact.",))
    results, fake = score(tmp_path, [item])
    assert results[0].n_claims == 0 and results[0].fully_faithful["claude-opus-5-5"] is False
    assert not [r for r in fake.requests if r.model == "judge"]  # nothing to judge


def test_batch_mode_matches_live(tmp_path):
    its, _ = items()
    live, _ = score(tmp_path / "live", its)
    batch, fake = score(tmp_path / "batch", its, live=False)
    assert [r.score for r in batch] == [r.score for r in live] and fake.submitted


def test_edited_items_never_reuse_old_claims_and_bad_ids_are_refused(tmp_path):
    its, _ = items()
    score(tmp_path, its[:1])
    edited = its[0].model_copy(update={"explanation": "Median waiting time fell sharply."})
    results, _ = score(tmp_path, [edited])
    assert [c.text for c in results[0].claims] == ["Median waiting time fell sharply."]
    with pytest.raises(ValueError, match="cannot contain"):
        score(tmp_path, [its[0].model_copy(update={"id": "a:b"})])
    with pytest.raises(ValueError, match="no evidence"):
        score(tmp_path, [its[0].model_copy(update={"evidence": ()})])


# ---- S2.4 verifier regressions (L-31) ---------------------------------------------------------


def _one_claim(req):
    if req.model == "extractor":
        m = EXTRACT_RE.search(req.prompt)
        assert m is not None
        return json.dumps({"claims": [{"text": m.group(1).strip(), "addresses_question": True}]})
    return reply(req)


def test_claims_are_judged_against_all_evidence_though_the_extractor_cites_nothing(tmp_path):
    item = FaithfulnessItem(
        id="x",
        question="Did waiting times fall?",
        explanation="Median waiting time fell from 52 to 31 minutes.",
        evidence=(
            "Staff numbers were unchanged.",
            "Median waiting time fell from 52 to 31 minutes.",
        ),
    )
    results, fake = score(tmp_path, [item], fake=ScriptedBatchAdapter(_one_claim))
    res = results[0]
    assert res.claims[0].cited == (1, 2)
    assert res.score == {"claude-opus-5-5": 1.0, "gpt-6.1-sol": 1.0}
    judge_prompts = [r.prompt for r in fake.requests if r.model == "judge"]
    assert judge_prompts and all("[2] Median waiting time" in p for p in judge_prompts)


def test_same_id_items_in_one_call_stay_separate(tmp_path):
    a = FaithfulnessItem(
        id="t",
        question="q?",
        explanation="Uptake was 41 percent.",
        evidence=("Uptake was 41 percent.",),
    )
    b = FaithfulnessItem(
        id="t",
        question="q?",
        explanation="Uptake doubled overnight.",
        evidence=("Uptake was 41 percent.",),
    )
    results, _ = score(tmp_path, [a, b])
    assert results[0].score["claude-opus-5-5"] == 1.0 and results[1].score["claude-opus-5-5"] == 0.0


def test_pending_claims_are_pending_not_errors_and_not_faithful(tmp_path):
    its, _ = items()
    slow = ScriptedBatchAdapter(reply, pending_polls=10**6)
    results, _ = score(tmp_path, its[:1], live=False, fake=slow)
    res = results[0]
    assert res.pending == {"claude-opus-5-5": 2, "gpt-6.1-sol": 2}
    assert res.judge_errors == {"claude-opus-5-5": 0, "gpt-6.1-sol": 0}
    assert res.fully_faithful == {"claude-opus-5-5": False, "gpt-6.1-sol": False}


def test_judge_errors_are_counted_and_score_covers_judged_claims_only(tmp_path):
    its, _ = items()

    def flaky(req):
        if req.model == "judge" and "Nurse-led" in req.prompt:
            return "not json"  # parse fails twice: excluded and counted
        return reply(req)

    results, _ = score(tmp_path, its[:1], fake=ScriptedBatchAdapter(flaky))
    res = results[0]
    assert res.judge_errors == {"claude-opus-5-5": 1, "gpt-6.1-sol": 1}
    assert res.score["claude-opus-5-5"] == 1.0  # the one judged claim is supported
    assert res.fully_faithful["claude-opus-5-5"] is False  # one claim was never judged


def test_extraction_failure_surfaces(tmp_path):
    its, _ = items()

    def broken(req):
        return "no json here" if req.model == "extractor" else reply(req)

    results, _ = score(tmp_path, its[:1], fake=ScriptedBatchAdapter(broken))
    assert results[0].extraction_error == "schema" and results[0].n_claims == 0


def test_judges_argument_is_respected(tmp_path):
    its, _ = items()
    fake = ScriptedBatchAdapter(reply)
    out = asyncio.run(
        faithfulness(
            its[:1],
            executor=Executor(Store(tmp_path)),
            config=CFG,
            adapters={"anthropic": fake, "openai": fake},
            prices=load_prices(),
            judges=("gpt-6.1-sol",),
        )
    )
    assert set(out[0].score) == {"gpt-6.1-sol"}
    assert {r.model_id for r in fake.requests if r.model == "judge"} == {"gpt-6.1-sol"}


# ---- FTH-04: no citation list for the extractor (D-60) ---------------------------------------


def test_extractor_is_not_asked_to_cite_and_long_pages_do_not_grow_its_output(tmp_path):
    evidence = tuple(f"Filler sentence number {i} about something else." for i in range(1, 750))
    item = FaithfulnessItem(
        id="long",
        question="Did waiting times fall?",
        explanation="Median waiting time fell from 52 to 31 minutes.",
        evidence=(*evidence, "Median waiting time fell from 52 to 31 minutes."),
    )
    results, fake = score(tmp_path, [item], fake=ScriptedBatchAdapter(_one_claim))
    ext = [r for r in fake.requests if r.model == "extractor"]
    assert len(ext) == 1
    assert "Cited sentence numbers" not in ext[0].prompt and "749" not in ext[0].prompt
    schema = json.dumps(dict(ext[0].json_schema or {}))
    assert '"cited"' not in schema and ext[0].max_output_tokens == 1024
    res = results[0]
    assert res.extraction_error is None and res.n_claims == 1
    assert res.claims[0].cited == tuple(range(1, 751))  # still judged against everything
    assert res.score["claude-opus-5-5"] == 1.0


def _truncating(first_stop):
    """Extractor: the first reply is cut off (stop_reason `first_stop`), later ones are whole."""
    calls = {"n": 0}

    def r(req):
        if req.model != "extractor":
            return reply(req)
        calls["n"] += 1
        text = '{"claims": [{"text": "Median waiting' if calls["n"] == 1 else _one_claim(req)
        return RawCompletion(
            model=req.model,
            model_snapshot="snap",
            params=dict(req.params),
            raw_text=text,
            tokens_in=100,
            tokens_out=req.max_output_tokens if calls["n"] == 1 else 20,
            latency_ms=1,
            cost_usd=0.001,
            stop_reason=first_stop if calls["n"] == 1 else "end_turn",
        )

    return r


WAIT = FaithfulnessItem(
    id="wait",
    question="Did waiting times fall?",
    explanation="Median waiting time fell from 52 to 31 minutes.",
    evidence=("Median waiting time fell from 52 to 31 minutes.",),
)


@pytest.mark.parametrize("stop", ["max_tokens", "length"])
def test_an_extraction_cut_off_at_the_limit_is_retried_once_with_a_higher_limit(tmp_path, stop):
    results, fake = score(tmp_path, [WAIT], fake=ScriptedBatchAdapter(_truncating(stop)))
    ext = [r for r in fake.requests if r.model == "extractor"]
    assert [r.max_output_tokens for r in ext] == [1024, 4096]
    assert results[0].extraction_error is None and results[0].n_claims == 1


def test_a_malformed_reply_that_was_not_cut_off_is_not_retried(tmp_path):
    results, fake = score(tmp_path, [WAIT], fake=ScriptedBatchAdapter(_truncating("end_turn")))
    assert len([r for r in fake.requests if r.model == "extractor"]) == 1
    assert results[0].extraction_error == "schema"


def test_the_retry_happens_once_only(tmp_path):
    def always_cut(req):
        if req.model != "extractor":
            return reply(req)
        return RawCompletion(
            model=req.model,
            model_snapshot="snap",
            params=dict(req.params),
            raw_text='{"claims": [',
            tokens_in=100,
            tokens_out=req.max_output_tokens,
            latency_ms=1,
            cost_usd=0.002,
            stop_reason="max_tokens",
        )

    results, fake = score(tmp_path, [WAIT], fake=ScriptedBatchAdapter(always_cut))
    assert len([r for r in fake.requests if r.model == "extractor"]) == 2
    assert results[0].extraction_error == "schema"


# ---- FTH-05: the explanation is read as an answer to its question (D-60) ---------------------

FRAGMENT = FaithfulnessItem(
    id="frag",
    question="Which benefits can pensioners claim for help with heating costs?",
    explanation="Income Support, Pension Credit.",
    evidence=(
        "Pensioners can claim Income Support for help with heating costs.",
        "Pensioners can claim Pension Credit for help with heating costs.",
    ),
)


def _subject_from_question(req):
    """Haiku's behaviour on a fragment: each claim borrows its subject from the question."""
    if req.model == "extractor":
        claims = [
            {
                "text": f"Pensioners can claim {b} for help with heating costs.",
                "addresses_question": True,
            }
            for b in ("Income Support", "Pension Credit")
        ]
        return json.dumps({"claims": claims})
    return reply(req)


def test_a_fragment_may_borrow_words_from_its_question(tmp_path):
    results, fake = score(tmp_path, [FRAGMENT], fake=ScriptedBatchAdapter(_subject_from_question))
    res = results[0]
    assert res.extraction_error is None and res.n_claims == 2
    assert res.fully_faithful == {"claude-opus-5-5": True, "gpt-6.1-sol": True}
    ext = next(r for r in fake.requests if r.model == "extractor")
    assert "you may use words from the question" in ext.prompt


def test_question_words_can_be_switched_off_and_then_b11_refuses_the_fragment(tmp_path):
    fake = ScriptedBatchAdapter(_subject_from_question)
    out = asyncio.run(
        faithfulness(
            [FRAGMENT],
            executor=Executor(Store(tmp_path)),
            config=CFG,
            adapters={"anthropic": fake, "openai": fake},
            prices=load_prices(),
            question_words=False,
        )
    )
    assert out[0].extraction_error == "schema" and out[0].n_claims == 0
    ext = next(r for r in fake.requests if r.model == "extractor")
    assert "words from the question" not in ext.prompt


def test_words_in_neither_answer_nor_question_are_still_refused(tmp_path):
    def invents(req):
        if req.model == "extractor":
            claim = {
                "text": "Income Support and Pension Credit are paid monthly by local councils.",
                "addresses_question": True,
            }
            return json.dumps({"claims": [claim]})
        return reply(req)

    results, _ = score(tmp_path, [FRAGMENT], fake=ScriptedBatchAdapter(invents))
    assert results[0].extraction_error == "schema"


def test_coverage_is_still_measured_against_the_answer_only(tmp_path):
    item = FRAGMENT.model_copy(
        update={"explanation": "Income Support, Pension Credit, Winter Fuel, Cold Weather Payment."}
    )

    def drops(req):  # restates the question and keeps one benefit: drops most of the answer
        if req.model == "extractor":
            claim = {
                "text": "Pensioners can claim Income Support for help with heating costs.",
                "addresses_question": True,
            }
            return json.dumps({"claims": [claim]})
        return reply(req)

    results, _ = score(tmp_path, [item], fake=ScriptedBatchAdapter(drops))
    assert results[0].extraction_error == "schema"
