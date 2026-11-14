"""Explanation faithfulness (S2.4, FR14): which statements in an explanation does the given
evidence support? Reuses the pinned claim extractor (A2.3) and the blind judges (A2.4), so it
is measured exactly like groundedness and cached the same way.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Literal

from evalkit.adapters import ModelAdapter
from evalkit.cost import Price
from evalkit.pipeline.extract import ClaimSet, extract_all
from evalkit.pipeline.generate import Generated
from evalkit.pipeline.judge import groundedness, judge_claims, judge_claims_live
from evalkit.runtime import Executor
from evalkit.schemas import Answer, Case, Completion, RunConfig
from evalkit.schemas._base import Fraction, Model, NonBlank, NonNegInt

EXPLANATION = "explanation"  # the "model" name in claim ids: {item}:explanation:{i}


class FaithfulnessItem(Model):
    id: NonBlank  # no ":" (claim ids use it)
    question: NonBlank
    explanation: str
    evidence: tuple[NonBlank, ...]


class JudgedClaim(Model):
    text: str
    cited: tuple[int, ...]  # the evidence the judges saw: always every sentence (FTH-02)
    verdicts: dict[str, Literal["supported", "unsupported"]]  # judge -> verdict
    evidence: dict[str, tuple[int, ...]]  # judge -> supporting sentence numbers


class FaithfulnessResult(Model):
    id: NonBlank
    score: dict[str, Fraction]  # judge -> share of judged claims supported
    fully_faithful: dict[str, bool]
    n_claims: NonNegInt
    claims: tuple[JudgedClaim, ...]
    extraction_error: str | None = None
    judge_errors: dict[str, NonNegInt]  # claims a judge failed on (excluded after a retry)
    pending: dict[str, NonNegInt]  # claims still in an unfinished batch (live=False)


def _case_id(item: FaithfulnessItem) -> str:
    if ":" in item.id:
        raise ValueError(f"faithfulness item id {item.id!r} cannot contain ':'")
    digest = hashlib.sha256(
        json.dumps([item.question, item.explanation, list(item.evidence)]).encode()
    ).hexdigest()[:12]
    return f"fth-{item.id}-{digest}"  # content-bound, so edited items never reuse old claims


def _generated(item: FaithfulnessItem) -> Generated:
    case = Case(
        id=_case_id(item),
        question=item.question,
        context=item.evidence,
        reference_label=None,
        source="faithfulness",
    )
    completion = Completion(
        model=EXPLANATION,
        model_snapshot="",
        params={},
        raw_text=item.explanation,
        # Explanations rarely cite: every evidence sentence counts as cited (FTH-01).
        answer=Answer(
            label="n/a",
            answer=item.explanation,
            citations=tuple(range(1, len(item.evidence) + 1)),
        ),
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        cost_usd=0.0,
    )
    return Generated(case=case, model=EXPLANATION, completion=completion)


async def faithfulness(
    items: Sequence[FaithfulnessItem],
    *,
    executor: Executor,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    judges: Sequence[str] | None = None,
    live: bool = True,
    max_wait_s: float = 0.0,
    run_id: str = "faithfulness",
) -> list[FaithfulnessResult]:
    """Score each explanation against its evidence, per judge. Live judging by default (one
    call per claim, immediate); `live=False` uses the Batch APIs and may leave claims unjudged
    until a later call collects them (those count against `fully_faithful`)."""
    if not items:
        return []
    for item in items:
        if not item.evidence:
            raise ValueError(f"item {item.id!r} has no evidence to check against")
    generated = [_generated(i) for i in items]
    claim_sets: list[ClaimSet] = await extract_all(
        executor, generated, config, adapters, prices, run_id=run_id
    )
    # Judge every claim against ALL of the item's evidence, whatever subset the extractor
    # mapped it to: faithfulness is to the evidence given, not to the extractor's citations.
    claim_sets = [
        cs.model_copy(
            update={
                "claims": tuple(
                    c.model_copy(update={"cited": tuple(range(1, len(g.case.context) + 1))})
                    for c in cs.claims
                )
            }
        )
        for g, cs in zip(generated, claim_sets, strict=True)
    ]
    pairs = [(g.case, c) for g, cs in zip(generated, claim_sets, strict=True) for c in cs.claims]
    judge_names = tuple(judges or config.judges)
    results = {}
    for judge in judge_names:
        if live:
            results[judge] = await judge_claims_live(
                executor, pairs, judge, config, adapters, prices, run_id=run_id
            )
        else:
            results[judge] = await judge_claims(
                executor,
                pairs,
                judge,
                config,
                adapters,
                prices,
                run_id=run_id,
                max_wait_s=max_wait_s,
            )
    out = []
    for item, cs in zip(items, claim_sets, strict=True):
        score: dict[str, float] = {}
        fully: dict[str, bool] = {}
        errors: dict[str, int] = {}
        pending: dict[str, int] = {}
        for judge, res in results.items():
            mine = {c.id: res.verdicts[c.id] for c in cs.claims if c.id in res.verdicts}
            score[judge], fully[judge] = groundedness(cs.claims, mine)
            missing = len(cs.claims) - len(mine)
            waiting = missing if res.status == "pending" else 0
            pending[judge] = waiting
            errors[judge] = missing - waiting
        claims = tuple(
            JudgedClaim(
                text=c.text,
                cited=c.cited,
                verdicts={
                    j: r.verdicts[c.id].verdict for j, r in results.items() if c.id in r.verdicts
                },
                evidence={
                    j: r.verdicts[c.id].evidence for j, r in results.items() if c.id in r.verdicts
                },
            )
            for c in cs.claims
        )
        out.append(
            FaithfulnessResult(
                id=item.id,
                score=score,
                fully_faithful=fully,
                n_claims=len(cs.claims),
                claims=claims,
                extraction_error=cs.error,
                judge_errors=errors,
                pending=pending,
            )
        )
    return out
