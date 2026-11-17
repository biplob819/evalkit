"""Explanation faithfulness (S2.4, FR14): which statements in an explanation does the given
evidence support? Reuses the pinned claim extractor (A2.3) and the blind judges (A2.4), so it
is measured exactly like groundedness and cached the same way.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Literal

from evalkit.adapters import ModelAdapter
from evalkit.cost import (
    ClaimsAssumption,
    CostEstimate,
    CostLine,
    Price,
    TokenAssumptions,
    call_cost,
    claims_from_texts,
    claims_in_text,
    estimate_tokens,
    measured_claims,
    price_for,
)
from evalkit.pipeline.extract import (
    FAITHFULNESS_EXTRACTOR_PROMPT,
    FAITHFULNESS_QUESTION_RULE,
    ClaimSet,
    extract_faithfulness_all,
)
from evalkit.pipeline.generate import Generated
from evalkit.pipeline.judge import (
    JudgeInput,
    groundedness,
    judge_claims,
    judge_claims_live,
    redact,
    render_judge_prompt,
)
from evalkit.runtime import Executor
from evalkit.schemas import Answer, Case, Completion, RunConfig
from evalkit.schemas._base import Fraction, Model, NonBlank, NonNegInt

# Public since 0.3.0, plus estimate_faithfulness_cost (fraud-review-bedrock, D-61 rule 3).
__all__ = [
    "FaithfulnessItem",
    "FaithfulnessResult",
    "JudgedClaim",
    "estimate_faithfulness_cost",
    "faithfulness",
]

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
        # Explanations rarely cite: every evidence sentence counts as cited (FTH-01). The
        # extractor is not shown this list (D-60); claims are mapped to all evidence below.
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
    question_words: bool = True,
) -> list[FaithfulnessResult]:
    """Score each explanation against its evidence, per judge. Live judging by default (one
    call per claim, immediate); `live=False` uses the Batch APIs and may leave claims unjudged
    until a later call collects them (those count against `fully_faithful`).

    Claims come from the faithfulness extractor (`extract_faithfulness_claims`, D-60), which is
    not asked to cite: every claim is judged against all of the item's evidence. With
    `question_words` (the default) the explanation is read as an answer to its question, so a
    claim may use the question's words (e.g. the subject of a fragment like "Income Support,
    Pension Credit...") without failing the B11 "adds content" check; coverage is still
    measured against the explanation alone. `question_words=False` applies B11 against the
    explanation only, as the benchmark extractor does."""
    if not items:
        return []
    for item in items:
        if not item.evidence:
            raise ValueError(f"item {item.id!r} has no evidence to check against")
    generated = [_generated(i) for i in items]
    claim_sets: list[ClaimSet] = await extract_faithfulness_all(
        executor,
        generated,
        config,
        adapters,
        prices,
        run_id=run_id,
        question_words=question_words,
    )
    # Judge every claim against ALL of the item's evidence: faithfulness is to the evidence
    # given (FTH-02). The faithfulness extractor returns no citations, so this sets them.
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


def estimate_faithfulness_cost(
    items: Sequence[FaithfulnessItem],
    *,
    config: RunConfig,
    prices: dict[str, Price],
    judges: Sequence[str] | None = None,
    live: bool = True,
    question_words: bool = True,
    claims_per_item: float | None = None,
    claims_source: str = "a measured sample",
    assumptions: TokenAssumptions | None = None,
) -> CostEstimate:
    """The expected cost of `faithfulness(items, ...)` with the same arguments, before any
    call: one extraction per item and one judge call per claim per judge.

    The explanations already exist, so claims are counted from them with no model call
    (`claims_from_texts`: words at `WORDS_PER_CLAIM`, each item at least the planning default)
    and each judge prompt is sized from the item's own evidence, which every claim is judged
    against (FTH-02). `claims_per_item` replaces the count with a figure measured on a small
    sample (`claims_source` says where it came from). `.claims` on the result states the
    assumption; it is an expected cost, not an upper bound (the executor's cap is the bound)."""
    if not items:
        raise ValueError("no items to estimate")
    a = assumptions or TokenAssumptions()
    texts = [i.explanation for i in items]
    claims: ClaimsAssumption
    if claims_per_item is not None:
        claims = measured_claims(claims_per_item, claims_source)
        per_item = [claims_per_item] * len(items)
    else:
        claims = claims_from_texts(texts, default=a.claims_per_answer)
        per_item = [claims_in_text(t, default=a.claims_per_answer) for t in texts]
    free = frozenset(config.endpoints) | frozenset(config.systems)

    def pf(model: str) -> Price:
        return price_for(prices, model, free_if_unknown=model in free)

    # Extraction output grows with the claims written out (the planning output covers 4.5).
    out_per_claim = a.t_ext_out / a.claims_per_answer
    rule = FAITHFULNESS_QUESTION_RULE if question_words else ""
    ext_price = pf(config.extractor)
    ext_usd = math.fsum(
        call_cost(
            ext_price,
            tokens_in=estimate_tokens(FAITHFULNESS_EXTRACTOR_PROMPT, rule, i.question, t),
            tokens_out=max(a.t_ext_out, math.ceil(n * out_per_claim)),
            tokens_reasoning=a.t_ext_reason,
        )
        for i, t, n in zip(items, texts, per_item, strict=True)
    )
    lines = [
        CostLine(
            node="extract_faithfulness_claims",
            model=config.extractor,
            calls=len(items),
            usd=ext_usd,
        )
    ]
    n_claims = claims.calls(len(items))
    for judge in tuple(judges or config.judges):
        price = pf(judge)
        usd = 0.0
        for item, text, n in zip(items, texts, per_item, strict=True):
            # A claim of this item's mean length, judged against all of its evidence.
            claim = text[: math.ceil(len(text) / n)] or "claim"
            prompt = render_judge_prompt(
                JudgeInput(
                    claim_id="estimate",
                    claim_text=redact(claim),
                    cited=tuple(range(1, len(item.evidence) + 1)),
                    sentences=tuple(redact(s) for s in item.evidence),
                    judge_id=judge,
                    effort=config.judge_effort,
                )
            )
            usd += n * call_cost(
                price,
                tokens_in=estimate_tokens(prompt),
                tokens_out=a.t_j_out,
                tokens_reasoning=a.t_j_reason,
                batch=not live,
            )
        lines.append(CostLine(node="judge", model=judge, calls=n_claims, usd=usd))
    return CostEstimate(lines=tuple(lines), total_usd=sum(ln.usd for ln in lines), claims=claims)
