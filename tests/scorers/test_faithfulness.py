import asyncio
import json
import re
from pathlib import Path

import pytest

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
EXTRACT_RE = re.compile(r"Answer \(citations[^:]*: (.*)\nCited sentence numbers: \[(.*)\]")


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.casefold()).split()


def reply(req):
    """Extractor: one claim per sentence. Judge: supported only if every content word of the
    claim appears in the cited sentences (no outside knowledge)."""
    if req.model == "extractor":
        m = EXTRACT_RE.search(req.prompt)
        assert m is not None
        text = m.group(1)
        cited = [int(x) for x in m.group(2).split(",") if x.strip()]
        claims = [
            {"text": s.strip() + ".", "cited": cited, "addresses_question": True}
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
    raw = json.loads(FIX.read_text())
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
    _, fake = score(tmp_path, its[:1])
    ext = next(r for r in fake.requests if r.model == "extractor")
    assert "Cited sentence numbers: [1, 2]" in ext.prompt


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


def _bad_citations(cited):
    def r(req):
        if req.model == "extractor":
            m = EXTRACT_RE.search(req.prompt)
            assert m is not None
            claim = {"text": m.group(1).strip(), "cited": cited, "addresses_question": True}
            return json.dumps({"claims": [claim]})
        return reply(req)

    return r


@pytest.mark.parametrize("cited", [[], [2]])
def test_claims_are_judged_against_all_evidence_whatever_the_extractor_cites(tmp_path, cited):
    item = FaithfulnessItem(
        id="x",
        question="Did waiting times fall?",
        explanation="Median waiting time fell from 52 to 31 minutes.",
        evidence=(
            "Median waiting time fell from 52 to 31 minutes.",
            "Staff numbers were unchanged.",
        ),
    )
    results, fake = score(tmp_path, [item], fake=ScriptedBatchAdapter(_bad_citations(cited)))
    res = results[0]
    assert res.claims[0].cited == (1, 2)
    assert res.score == {"claude-opus-5-5": 1.0, "gpt-6.1-sol": 1.0}
    judge_prompts = [r.prompt for r in fake.requests if r.model == "judge"]
    assert judge_prompts and all("[1] Median waiting time" in p for p in judge_prompts)


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
