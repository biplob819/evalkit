"""Claim extraction (EXT-01..04): one pinned extractor splits each answer into atomic claims,
so both judges score identical claim sets (D-3) and coverage can be measured (D-31)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import field_validator, model_validator

from evalkit.adapters import CompletionRequest, ModelAdapter
from evalkit.cost import Price, call_cost, price_for
from evalkit.pipeline.answer import SchemaError, _extract_json
from evalkit.pipeline.generate import Generated, adapter_key, provider_for
from evalkit.retry import is_retryable
from evalkit.runtime import BudgetExceeded, Executor, Node, RunContext
from evalkit.schemas import Claim, RunConfig
from evalkit.schemas._base import Model, Name, NonBlank, NonNegFloat, NonNegInt, PosInt, SentenceNo
from evalkit.schemas.benchmark import ErrorType
from evalkit.scorers.base import ScoreResult

EXTRACTOR_PROMPT = """You split an answer into atomic claims for fact-checking.

Question: {question}

Answer (citations refer to numbered source sentences): {answer}
Cited sentence numbers: {citations}

Rules:
- Each claim is one checkable statement taken from the answer text. Do not add, merge,
  drop, or reword beyond what is needed to make a claim stand alone.
- For each claim, list which of the cited sentence numbers the answer uses to support it.
  Use only numbers from the cited list. A claim may have an empty list.
- Mark "addresses_question" true when the claim bears on answering the question.
- Return every claim in the order it appears.

Reply with JSON only, in this shape:
{{"claims": [{{"text": "...", "cited": [n, ...], "addresses_question": true}}]}}"""

EXTRACTOR_CODE_VERSION = "2"


def version_for(prompt: str, code_version: str = EXTRACTOR_CODE_VERSION) -> str:
    """`ext<code_version>-<first 8 hex of sha256(prompt)>` (EXT-01)."""
    return f"ext{code_version}-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]


EXTRACTOR_VERSION = version_for(EXTRACTOR_PROMPT)

# B11 guard (D-32): share of a claim's content words found in the answer (adding), and share
# of the answer's content words covered by all claims together (dropping).
MIN_CLAIM_SUPPORT = 0.6
MIN_ANSWER_COVERAGE = 0.5
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "shall",
        "she",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "under",
        "up",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


def extraction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "cited": {"type": "array", "items": {"type": "integer"}},
                        "addresses_question": {"type": "boolean"},
                    },
                    "required": ["text", "cited", "addresses_question"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["claims"],
        "additionalProperties": False,
    }


class ExtractInput(Model):
    case_id: NonBlank
    model: Name  # the model whose answer is being split
    question: str
    answer_text: str  # the label is not needed and stays out of the cache key
    citations: tuple[SentenceNo, ...]
    extractor_id: Name
    provider: Name
    effort: Name | None
    max_output_tokens: PosInt = 1024
    extractor_version: Name = EXTRACTOR_VERSION

    @field_validator("case_id")
    @classmethod
    def _no_colon(cls, v: str) -> str:
        if ":" in v:
            raise ValueError("case ids may not contain ':' (claim ids are case:model:index)")
        return v

    @field_validator("extractor_version")
    @classmethod
    def _current(cls, v: str) -> str:
        if v != EXTRACTOR_VERSION:
            raise ValueError(f"extractor_version {v!r} is not the current {EXTRACTOR_VERSION!r}")
        return v


class ClaimSet(Model):
    claims: tuple[Claim, ...] = ()
    addresses_question: tuple[bool, ...] = ()
    error: ErrorType | None = None
    cost_usd: NonNegFloat = 0.0
    tokens_in: NonNegInt = 0
    tokens_out: NonNegInt = 0

    @model_validator(mode="after")
    def _aligned(self) -> ClaimSet:
        if len(self.claims) != len(self.addresses_question):
            raise ValueError("claims and addresses_question must have the same length")
        if self.error is not None and self.claims:
            raise ValueError("a failed extraction carries no claims")
        return self


def content_words(text: str) -> set[str]:
    """Lower-cased content words (stop words dropped, a plural 's' trimmed)."""
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.casefold())
    return {
        w[:-1] if len(w) > 4 and w.endswith("s") else w
        for w in words
        if w not in _STOP and (len(w) > 2 or w.isdigit())
    }


def parse_claims(raw_text: str, inp: ExtractInput) -> tuple[tuple[Claim, ...], tuple[bool, ...]]:
    """Validate extractor output against B11. Any violation is a SchemaError."""
    try:
        data = json.loads(_extract_json(raw_text))
    except ValueError as exc:
        raise SchemaError(f"extraction is not valid JSON: {exc}") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"claims"}
        or not isinstance(data["claims"], list)
    ):
        raise SchemaError("extraction must be an object with exactly a 'claims' list")
    items = data["claims"]
    if not items and inp.answer_text.strip():
        raise SchemaError("a non-empty answer produced no claims")
    allowed = set(inp.citations)
    answer_words = content_words(inp.answer_text)
    covered: set[str] = set()
    claims: list[Claim] = []
    flags: list[bool] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"text", "cited", "addresses_question"}:
            raise SchemaError(f"claim {i} must have exactly text, cited, addresses_question")
        text, cited, flag = item["text"], item["cited"], item["addresses_question"]
        if not isinstance(text, str) or not text.strip():
            raise SchemaError(f"claim {i} has no text")
        if not isinstance(flag, bool):
            raise SchemaError(f"claim {i}: addresses_question must be true or false")
        if not isinstance(cited, list) or any(type(c) is not int for c in cited):
            raise SchemaError(f"claim {i}: cited must be a list of integers")
        invented = sorted(set(cited) - allowed)
        if invented:
            raise SchemaError(f"claim {i} cites {invented}, which the answer did not cite")
        norm = " ".join(text.split()).casefold()
        if norm in seen:
            raise SchemaError(f"claim {i} duplicates an earlier claim")
        seen.add(norm)
        words = content_words(text)
        if words and answer_words and len(words & answer_words) / len(words) < MIN_CLAIM_SUPPORT:
            raise SchemaError(f"claim {i} adds content not in the answer (B11)")
        covered |= words & answer_words
        claims.append(
            Claim(
                id=f"{inp.case_id}:{inp.model}:{i}",
                text=text.strip(),
                cited=tuple(sorted(set(cited))),
                extractor_version=EXTRACTOR_VERSION,
            )
        )
        flags.append(flag)
    if answer_words and len(covered) / len(answer_words) < MIN_ANSWER_COVERAGE:
        raise SchemaError("claims drop most of the answer's content (B11)")
    return tuple(claims), tuple(flags)


class ExtractClaims(Node[ExtractInput, ClaimSet]):
    name = "extract_claims"
    code_version = EXTRACTOR_CODE_VERSION
    prompt = EXTRACTOR_PROMPT  # a class string constant: hashed by the version check (RUN-07)
    In = ExtractInput
    Out = ClaimSet

    def __init__(self, adapters: Mapping[str, ModelAdapter], prices: dict[str, Price]) -> None:
        self.adapters = adapters
        self.prices = prices

    def pool(self, inp: ExtractInput) -> str | None:
        return inp.provider

    def estimate_cost(self, inp: ExtractInput, ctx: RunContext) -> float:
        price = price_for(
            self.prices, inp.extractor_id, free_if_unknown=inp.provider == "openai_compatible"
        )
        prompt_chars = len(self.prompt) + len(inp.question) + len(inp.answer_text)
        return call_cost(price, tokens_in=prompt_chars // 3 + 1, tokens_out=inp.max_output_tokens)

    async def run(self, inp: ExtractInput, ctx: RunContext) -> ClaimSet:
        prompt = self.prompt.format(
            question=inp.question, answer=inp.answer_text, citations=list(inp.citations)
        )
        req = CompletionRequest(
            model="extractor",
            model_id=inp.extractor_id,
            prompt=prompt,
            max_output_tokens=inp.max_output_tokens,
            params={"effort": inp.effort} if inp.effort else {},
            json_schema=extraction_schema(),
        )
        raw = await self.adapters[adapter_key(inp.provider, inp.extractor_id)].complete(req)
        base: dict[str, Any] = {
            "cost_usd": raw.cost_usd,
            "tokens_in": raw.tokens_in,
            "tokens_out": raw.tokens_out + raw.tokens_reasoning,
        }
        if raw.refusal:
            return ClaimSet(error="refusal", **base)
        try:
            claims, flags = parse_claims(raw.raw_text, inp)
        except SchemaError:
            return ClaimSet(error="schema", **base)
        return ClaimSet(claims=claims, addresses_question=flags, **base)


def coverage(claim_set: ClaimSet) -> ScoreResult:
    """Share of claims that bear on the question (D-31); 0.0 with no claims."""
    n = len(claim_set.claims)
    addressing = sum(claim_set.addresses_question)
    version = claim_set.claims[0].extractor_version if n else None
    return ScoreResult(
        value=addressing / n if n else 0.0,
        details={"n_claims": n, "n_addressing": addressing, "extractor_version": version},
    )


def extract_inputs(
    generated: Sequence[Generated], config: RunConfig
) -> list[tuple[int, ExtractInput]]:
    """(index into `generated`, input) for every completion with a usable answer."""
    provider = provider_for(config.extractor, config.endpoints)
    out = []
    for i, g in enumerate(generated):
        c = g.completion
        if c.error is None and c.answer is not None and c.answer.answer.strip():
            out.append(
                (
                    i,
                    ExtractInput(
                        case_id=g.case.id,
                        model=g.model,
                        question=g.case.question,
                        answer_text=c.answer.answer,
                        citations=c.answer.citations,
                        extractor_id=config.extractor,
                        provider=provider,
                        effort=config.extractor_effort,
                    ),
                )
            )
    return out


async def extract_all(
    executor: Executor,
    generated: Sequence[Generated],
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    *,
    run_id: str,
) -> list[ClaimSet]:
    """One ClaimSet per Generated record, in order. Answers that cannot be split get an empty
    set. Same taxonomy as generation: transient provider failures are recorded per item as
    error="transport" (not cached); budget and configuration errors stop the stage."""
    node = ExtractClaims(adapters, prices)
    pairs = extract_inputs(generated, config)
    ctx = RunContext(run_id=run_id, seed=config.seed)
    results = await executor.map_settled(node, [inp for _, inp in pairs], ctx)
    budget = [r for r in results if isinstance(r, BudgetExceeded)]
    if budget:
        raise BudgetExceeded(executor.spent_usd, budget[0].cap, remaining=len(budget))
    local = [r for r in results if isinstance(r, Exception) and not is_retryable(r)]
    if local:
        raise local[0]
    out = [ClaimSet() for _ in generated]
    for (i, _), cs in zip(pairs, results, strict=True):
        out[i] = ClaimSet(error="transport") if isinstance(cs, Exception) else cs
    return out
