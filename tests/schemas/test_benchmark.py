import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evalkit.schemas import Answer, Case, Claim, ClaimVerdict, Completion, Record, Scores

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "results_v1"


def completion(**over: Any) -> Completion:
    base: dict[str, Any] = dict(
        model="sonnet",
        model_snapshot="claude-sonnet-5-5",
        params={"temperature": 0},
        raw_text="{}",
        answer=Answer(label="yes", answer="a", citations=(1,)),
        tokens_in=10,
        tokens_out=5,
        latency_ms=100,
        cost_usd=0.001,
    )
    base.update(over)
    return Completion(**base)


def test_record_fixture_parses_with_all_fields():
    rec = Record.model_validate_json((FIX / "record.json").read_text(encoding="utf-8"))
    assert rec.completion.answer is not None and rec.completion.answer.citations == (4,)
    assert {v.judge for v in rec.verdicts} == {"claude-opus-5-5", "gpt-6.1-sol"}
    assert rec.scores is not None and rec.scores.correct_and_grounded["gpt-6.1-sol"]


def test_unknown_fields_are_rejected():
    data = json.loads((FIX / "record.json").read_text(encoding="utf-8"))
    data["surprise"] = 1
    with pytest.raises(ValidationError, match="surprise"):
        Record.model_validate(data)


@pytest.mark.parametrize("field", ["tokens_in", "tokens_out", "latency_ms", "cost_usd"])
def test_negative_usage_is_rejected(field):
    with pytest.raises(ValidationError):
        completion(**{field: -1})


def test_citations_are_one_based():
    with pytest.raises(ValidationError):
        Answer(label="yes", answer="a", citations=(0,))
    with pytest.raises(ValidationError):
        Claim(id="c", text="t", cited=(0,), extractor_version="e")


def test_error_type_is_constrained_and_consistent():
    with pytest.raises(ValidationError):
        completion(error="timeout")
    with pytest.raises(ValidationError, match="cannot carry an answer"):
        completion(error="schema")
    assert completion(error="schema", answer=None).answer is None
    assert completion(error="refusal").error == "refusal"


def test_claim_verdict_shape():
    v = ClaimVerdict(
        claim_id="c",
        judge="j",
        verdict="unsupported",
        evidence=(),
        rationale="r",
        judge_prompt_hash="h",
    )
    assert v.quote is None
    with pytest.raises(ValidationError):
        ClaimVerdict(
            claim_id="c",
            judge="j",
            verdict="maybe",  # type: ignore[arg-type]
            evidence=(),
            rationale="r",
            judge_prompt_hash="h",
        )


def test_scores_per_judge_maps_must_agree():
    ok: dict[str, Any] = dict(
        label_correct=True,
        citation_valid=1.0,
        coverage=1.0,
        groundedness={"a": 1.0},
        fully_grounded={"a": True},
        correct_and_grounded={"a": True},
    )
    Scores(**ok)
    with pytest.raises(ValidationError, match="same judges"):
        Scores(**{**ok, "fully_grounded": {"b": True}})
    with pytest.raises(ValidationError):
        Scores(**{**ok, "groundedness": {"a": 1.5}})


def test_models_are_immutable():
    c = Case(id="x", question="q", context=("s",), reference_label="yes", source="s")
    with pytest.raises(ValidationError):
        c.id = "y"  # type: ignore[misc]
