"""Claim extraction (EXT-01..04): one pinned extractor splits each answer into atomic claims,
so both judges score identical claim sets (D-3) and coverage can be measured (D-31)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence, Set
from typing import Any, TypeVar

from pydantic import BaseModel, field_validator, model_validator

from evalkit.adapters import CompletionRequest, ModelAdapter
from evalkit.cost import Price, call_cost, estimate_tokens, price_for
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


def parse_claims(
    raw_text: str,
    inp: ExtractInput,
    *,
    with_citations: bool = True,
    extra_words: Set[str] = frozenset(),
    extractor_version: str = EXTRACTOR_VERSION,
) -> tuple[tuple[Claim, ...], tuple[bool, ...]]:
    """Validate extractor output against B11. Any violation is a SchemaError.

    The defaults are the benchmark extractor's contract and must not change behaviour. The
    faithfulness extractor (D-60) passes `with_citations=False` (claims are
    `{text, addresses_question}` and carry no citations) and, when it reads the answer as a
    reply to its question, the question's content words as `extra_words`: those words may
    appear in a claim without counting as added content, while coverage (dropping) is still
    measured against the answer's own words only."""
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
    fields = (
        ("text", "cited", "addresses_question")
        if with_citations
        else ("text", "addresses_question")
    )
    keys = set(fields)
    answer_words = content_words(inp.answer_text)
    support_words = answer_words | extra_words
    covered: set[str] = set()
    claims: list[Claim] = []
    flags: list[bool] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != keys:
            raise SchemaError(f"claim {i} must have exactly {', '.join(fields)}")
        text, cited, flag = item["text"], item.get("cited", []), item["addresses_question"]
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
        if words and answer_words and len(words & support_words) / len(words) < MIN_CLAIM_SUPPORT:
            raise SchemaError(f"claim {i} adds content not in the answer (B11)")
        covered |= words & answer_words
        claims.append(
            Claim(
                id=f"{inp.case_id}:{inp.model}:{i}",
                text=text.strip(),
                cited=tuple(sorted(set(cited))),
                extractor_version=extractor_version,
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
        tokens = estimate_tokens(self.prompt, inp.question, inp.answer_text)
        return call_cost(price, tokens_in=tokens, tokens_out=inp.max_output_tokens)

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


# ---- Faithfulness extraction (D-60) ---------------------------------------------------------
# A separate node so the benchmark extractor (`extract_claims`), its prompt, its cache keys and
# its B11 guard stay exactly as they were. Two differences, both for faithfulness only:
#   1. No citation list: an explanation is checked against ALL of its evidence (FTH-02), so the
#      extractor is not asked to map claims to sentence numbers. Echoing a 1..N list per claim
#      on a long page overran the output limit and every such item failed as "schema".
#   2. The explanation is read as an answer to its question (`question_words`, default on): a
#      fragment such as "Income Support, Pension Credit..." may borrow its subject from the
#      question, and those words do not count as added content under B11. Coverage is still
#      measured against the explanation's own words.

FAITHFULNESS_QUESTION_RULE = """
- The answer may be a fragment (a list or a phrase) that only makes sense as a reply to the
  question. To make such a claim stand alone you may use words from the question, such as
  its subject. Add nothing that is in neither the answer nor the question."""

FAITHFULNESS_EXTRACTOR_PROMPT = """You split an answer into atomic claims for fact-checking.

Question: {question}

Answer: {answer}

Rules:
- Each claim is one checkable statement taken from the answer text. Do not add, merge,
  drop, or reword beyond what is needed to make a claim stand alone.{question_rule}
- Mark "addresses_question" true when the claim bears on answering the question.
- Return every claim in the order it appears.

Reply with JSON only, in this shape:
{{"claims": [{{"text": "...", "addresses_question": true}}]}}"""

FAITHFULNESS_EXTRACTOR_CODE_VERSION = "1"
FAITHFULNESS_EXTRACTOR_VERSION = version_for(
    FAITHFULNESS_EXTRACTOR_PROMPT + FAITHFULNESS_QUESTION_RULE,
    f"f{FAITHFULNESS_EXTRACTOR_CODE_VERSION}",
)  # extf1-<hash>: recorded on every faithfulness claim


def faithfulness_extraction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "addresses_question": {"type": "boolean"},
                    },
                    "required": ["text", "addresses_question"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["claims"],
        "additionalProperties": False,
    }


class FaithfulnessExtractInput(ExtractInput):
    """Input of `extract_faithfulness_claims`: no citations; `question_words` reads the answer
    as a reply to `question` (question words allowed in claims under B11)."""

    citations: tuple[SentenceNo, ...] = ()
    question_words: bool = True
    extractor_version: Name = FAITHFULNESS_EXTRACTOR_VERSION

    @field_validator("citations")
    @classmethod
    def _uncited(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if v:
            raise ValueError("faithfulness extraction carries no citations (D-60)")
        return v

    @field_validator("extractor_version")
    @classmethod
    def _current(cls, v: str) -> str:
        if v != FAITHFULNESS_EXTRACTOR_VERSION:
            raise ValueError(
                f"extractor_version {v!r} is not the current {FAITHFULNESS_EXTRACTOR_VERSION!r}"
            )
        return v


class ExtractFaithfulnessClaims(Node[FaithfulnessExtractInput, ClaimSet]):
    """Claim extraction for explanation faithfulness (S2.4, D-60). If the first reply stopped
    at the output limit and does not parse, it retries once with `retry_factor` times the
    limit; both calls are counted and both are inside the cost estimate."""

    name = "extract_faithfulness_claims"
    code_version = FAITHFULNESS_EXTRACTOR_CODE_VERSION
    prompt = FAITHFULNESS_EXTRACTOR_PROMPT
    question_rule = FAITHFULNESS_QUESTION_RULE
    retry_factor = 4
    truncated = ("max_tokens", "length")  # Anthropic / OpenAI stop reasons at the limit
    In = FaithfulnessExtractInput
    Out = ClaimSet

    def __init__(self, adapters: Mapping[str, ModelAdapter], prices: dict[str, Price]) -> None:
        self.adapters = adapters
        self.prices = prices

    def pool(self, inp: FaithfulnessExtractInput) -> str | None:
        return inp.provider

    def estimate_cost(self, inp: FaithfulnessExtractInput, ctx: RunContext) -> float:
        price = price_for(
            self.prices, inp.extractor_id, free_if_unknown=inp.provider == "openai_compatible"
        )
        tokens = estimate_tokens(self.prompt, self.question_rule, inp.question, inp.answer_text)
        # Worst case: the first call and the one retry, each at its own output limit.
        return call_cost(
            price,
            tokens_in=2 * tokens,
            tokens_out=inp.max_output_tokens * (1 + self.retry_factor),
        )

    async def run(self, inp: FaithfulnessExtractInput, ctx: RunContext) -> ClaimSet:
        prompt = self.prompt.format(
            question=inp.question,
            answer=inp.answer_text,
            question_rule=self.question_rule if inp.question_words else "",
        )
        extra = content_words(inp.question) if inp.question_words else frozenset[str]()
        adapter = self.adapters[adapter_key(inp.provider, inp.extractor_id)]
        base: dict[str, Any] = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
        limit = inp.max_output_tokens
        for attempt in (1, 2):
            req = CompletionRequest(
                model="extractor",
                model_id=inp.extractor_id,
                prompt=prompt,
                max_output_tokens=limit,
                params={"effort": inp.effort} if inp.effort else {},
                json_schema=faithfulness_extraction_schema(),
            )
            raw = await adapter.complete(req)
            base["cost_usd"] += raw.cost_usd
            base["tokens_in"] += raw.tokens_in
            base["tokens_out"] += raw.tokens_out + raw.tokens_reasoning
            if raw.refusal:
                return ClaimSet(error="refusal", **base)
            try:
                claims, flags = parse_claims(
                    raw.raw_text,
                    inp,
                    with_citations=False,
                    extra_words=extra,
                    extractor_version=inp.extractor_version,
                )
            except SchemaError:
                if attempt == 1 and raw.stop_reason in self.truncated:
                    limit *= self.retry_factor
                    continue
                return ClaimSet(error="schema", **base)
            return ClaimSet(claims=claims, addresses_question=flags, **base)
        raise AssertionError("unreachable: the second attempt always returns")


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
    return await _settle(executor, node, pairs, len(generated), config, run_id)


async def extract_faithfulness_all(
    executor: Executor,
    generated: Sequence[Generated],
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    *,
    run_id: str,
    question_words: bool = True,
) -> list[ClaimSet]:
    """`extract_all` for explanation faithfulness (D-60): the same usable-answer filter and
    error taxonomy, but through `extract_faithfulness_claims` (no citation list; with
    `question_words`, the answer is read as a reply to its question under B11)."""
    node = ExtractFaithfulnessClaims(adapters, prices)
    pairs = [
        (
            i,
            FaithfulnessExtractInput(
                **inp.model_dump(exclude={"citations", "extractor_version"}),
                question_words=question_words,
            ),
        )
        for i, inp in extract_inputs(generated, config)
    ]
    return await _settle(executor, node, pairs, len(generated), config, run_id)


_In = TypeVar("_In", bound=BaseModel)


async def _settle(
    executor: Executor,
    node: Node[_In, ClaimSet],
    pairs: Sequence[tuple[int, _In]],
    n: int,
    config: RunConfig,
    run_id: str,
) -> list[ClaimSet]:
    ctx = RunContext(run_id=run_id, seed=config.seed)
    results = await executor.map_settled(node, [inp for _, inp in pairs], ctx)
    budget = [r for r in results if isinstance(r, BudgetExceeded)]
    if budget:
        raise BudgetExceeded(executor.spent_usd, budget[0].cap, remaining=len(budget))
    local = [r for r in results if isinstance(r, Exception) and not is_retryable(r)]
    if local:
        raise local[0]
    out = [ClaimSet() for _ in range(n)]
    for (i, _), cs in zip(pairs, results, strict=True):
        out[i] = ClaimSet(error="transport") if isinstance(cs, Exception) else cs
    return out


def ground_on_answer_citations(
    generated: Sequence[Generated], claim_sets: Sequence[ClaimSet]
) -> list[ClaimSet]:
    """Judge every claim against all the sentences its answer cited (D-52).

    The extractor's per-claim mapping is made without the sentence texts, so it guesses by
    position; judging a claim only against that guess marked true, cited claims unsupported
    (88% of a sampled set). Groundedness means "supported by the answer's citations", so each
    claim gets the answer's full (valid) citation set. Out-of-range citations are dropped here
    and penalised by the citation-validity scorer instead of sinking every claim."""
    out = []
    for g, cs in zip(generated, claim_sets, strict=True):
        answer = g.completion.answer
        n = len(g.case.context)
        cites = tuple(sorted({c for c in (answer.citations if answer else ()) if 1 <= c <= n}))
        out.append(
            cs.model_copy(
                update={"claims": tuple(c.model_copy(update={"cited": cites}) for c in cs.claims)}
            )
        )
    return out
