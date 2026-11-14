"""Benchmark schemas (SCH-01): one case through generation, claims, verdicts and scores."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import model_validator

from evalkit.schemas._base import (
    Fraction,
    FrozenMap,
    Model,
    NonBlank,
    NonNegFloat,
    NonNegInt,
    SentenceNo,
    StrictInt,
    Timestamp,
    Versioned,
)

ErrorType = Literal["transport", "schema", "refusal"]
ERROR_TYPES: tuple[str, ...] = get_args(ErrorType)
Verdict = Literal["supported", "unsupported"]


class Case(Model):
    id: NonBlank
    question: NonBlank
    context: tuple[str, ...]  # source split into sentences; sentence i is cited as i (1-based)
    reference_label: NonBlank | None = None  # one of the task's labels, or None if unlabelled
    source: NonBlank  # e.g. "pubmedqa/pqa_l"


class Answer(Model):
    label: NonBlank  # validated against TaskConfig.labels by the Validate node
    answer: str
    citations: tuple[SentenceNo, ...]


class Completion(Model):
    model: NonBlank  # alias from config
    model_snapshot: str  # exact provider model id from the response ("" if unknown)
    params: FrozenMap[float | int | str | bool]
    raw_text: str
    answer: Answer | None
    tokens_in: NonNegInt
    tokens_out: NonNegInt
    tokens_reasoning: NonNegInt = 0
    tokens_cached: NonNegInt = 0
    latency_ms: NonNegInt
    cost_usd: NonNegFloat
    request_id: str | None = None
    error: ErrorType | None = None

    @model_validator(mode="after")
    def _error_consistency(self) -> Completion:
        if self.error in ("schema", "transport") and self.answer is not None:
            raise ValueError(f"a completion with error={self.error!r} cannot carry an answer")
        if self.error is None and self.answer is None:
            raise ValueError("a completion without an error must carry an answer")
        return self


class Claim(Model):
    id: NonBlank  # f"{case_id}:{model}:{i}"
    text: NonBlank
    cited: tuple[SentenceNo, ...]
    extractor_version: NonBlank


class ClaimVerdict(Model):
    claim_id: NonBlank
    judge: NonBlank
    verdict: Verdict
    evidence: tuple[SentenceNo, ...]
    quote: str | None = None
    rationale: str
    judge_prompt_hash: NonBlank


class Scores(Model):
    label_correct: bool
    citation_valid: Fraction
    groundedness: FrozenMap[Fraction]  # per judge: share of claims supported
    fully_grounded: FrozenMap[bool]  # per judge
    coverage: Fraction
    correct_and_grounded: FrozenMap[bool]  # headline, per judge

    @model_validator(mode="after")
    def _per_judge_consistency(self) -> Scores:
        keys = [set(self.groundedness), set(self.fully_grounded), set(self.correct_and_grounded)]
        if not keys[0] == keys[1] == keys[2]:
            raise ValueError("per-judge score maps must name the same judges")
        return self


class Record(Versioned):
    """One JSONL line: one case answered by one model, with everything needed to rescore it.

    `scores` is None while the record is pending scoring: the pipeline writes the record
    when the completion arrives and fills scores after judging (D-25).
    """

    run_id: NonBlank
    task: NonBlank
    case_id: NonBlank
    timestamp: Timestamp
    completion: Completion
    claims: tuple[Claim, ...]
    verdicts: tuple[ClaimVerdict, ...]
    scores: Scores | None
    seed: StrictInt
    git_sha: str
    prompt_hash: NonBlank

    @model_validator(mode="after")
    def _claims_and_verdicts_link(self) -> Record:
        ids = [c.id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim ids must be unique within a record")
        unknown = {v.claim_id for v in self.verdicts} - set(ids)
        if unknown:
            raise ValueError(f"verdicts name claims not in this record: {sorted(unknown)}")
        pairs = [(v.claim_id, v.judge) for v in self.verdicts]
        if len(pairs) != len(set(pairs)):
            raise ValueError("a judge may give at most one verdict per claim")
        return self
