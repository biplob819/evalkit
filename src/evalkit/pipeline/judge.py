"""Dual judging (JDG-01..08): each judge scores every claim against only its cited sentences,
blind to the model that wrote the answer, through its provider's Batch API.

Batch bookkeeping is per claim: each pending claim remembers the batch it was sent in, so a
crash after partial caching never pays twice and a failed or expired batch is resubmitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import field_validator

from evalkit.adapters import BatchRequest, CompletionRequest, ModelAdapter, RawCompletion
from evalkit.cost import Price, call_cost, price_for
from evalkit.pipeline.answer import SchemaError, _extract_json
from evalkit.pipeline.generate import adapter_key, provider_for
from evalkit.runtime import BudgetExceeded, Executor
from evalkit.runtime.canonical import canonical_json, sha256_hex
from evalkit.runtime.store import Lineage
from evalkit.schemas import Case, Claim, ClaimVerdict, RunConfig
from evalkit.schemas._base import Model, Name, NonNegInt

JUDGE_RUBRIC = """You check one claim against the source sentences it cites.

Claim: {claim}

Cited source sentences:
{sentences}

Rules:
- Use only the sentences above. Do not use outside knowledge, even if the claim is true
  in general. If the sentences do not state or directly imply the claim, it is unsupported.
- "supported" means the cited sentences establish the claim. Partial support is unsupported.
- If supported, list the sentence numbers that support it as evidence; if unsupported, give
  an empty evidence list.
- Optionally quote, word for word, the most relevant part of one cited sentence; otherwise
  null. Give a one-line rationale.

Reply with JSON only, in this shape:
{{"verdict": "supported" or "unsupported", "evidence": [n, ...],
 "quote": "..." or null, "rationale": "..."}}"""

SENTENCE_FORMAT = "[{n}] {text}"
JUDGE_CODE_VERSION = "5"
# Names that must never reach a judge (B4, D-35, D-38). Strong names are redacted wherever they
# stand as a word (plurals too). Ambiguous names are also English, clinical or trial words
# ("saline solution", "serum GPT 120", "the GEMINI 1 trial"), so they are redacted only with a
# second name, a dotted or lettered version ("Sonnet 5.5", "gpt-4o"), a multi-part hyphen
# version ("opus-5-5"), or, for GPT only, a hyphen version ("GPT-4"). A space chains only
# dotted or lettered versions, never a bare integer, so lab values are never swallowed.
STRONG_NAMES = ("claude", "anthropic", "openai", "chatgpt", "deepseek", "qwen")
AMBIGUOUS_NAMES = (
    "gpt",
    "opus",
    "sonnet",
    "haiku",
    "fable",
    "mythos",
    "sol",
    "luna",
    "terra",
    "gemini",
    "llama",
    "mistral",
)
BLIND_DENYLIST = STRONG_NAMES + AMBIGUOUS_NAMES
_NAME = "(?:" + "|".join(sorted(BLIND_DENYLIST, key=len, reverse=True)) + ")"
_VER_ANY = r"[a-z]?\d+(?:\.\d+)*[a-z]*"
_VER_SPACED = r"\d+(?:\.\d+)+[a-z]*|\d+[a-z]+"
_BLIND_RE = re.compile(
    rf"(?<![a-z0-9]){_NAME}s?(?:\d+(?:\.\d+)*[a-z]*)?"
    rf"(?:[-._](?:{_VER_ANY}|{_NAME})|[ ](?:{_VER_SPACED}|{_NAME}))*(?![a-z0-9])",
    re.I,
)
# Spaced or hyphenated spellings of strong names a model may use about itself.
_SPACED_RE = re.compile(r"(?<![a-z0-9])(?:chat[ -]gpt|open[ -]ai)s?(?![a-z0-9])", re.I)
_STRONG = frozenset(STRONG_NAMES)
_ALL = frozenset(BLIND_DENYLIST)
# Typographic dashes, no-break spaces and fullwidth ASCII fold to plain characters before
# matching (GPT models often write U+2011). Nothing else changes: superscripts in "10\u2079/L"
# must reach the judge as written, so this is deliberately not NFKC.
_BLIND_FOLD = str.maketrans(
    {c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
    | {c: " " for c in "\u00a0\u202f\u2007"}
    | {chr(cp): chr(cp - 0xFEE0) for cp in range(0xFF01, 0xFF5F)}
)
# Typographic quotes, dashes and ellipses, folded only for quote comparison (D-38).
_FOLD = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
    }
)

REDACTED = "[model]"
# Room for medium-effort reasoning plus the JSON verdict; truncation would cost a retry.
JUDGE_MAX_OUTPUT_TOKENS = 4096

JudgeMode = Literal["dual", "single"]


def verdict_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["supported", "unsupported"]},
            "evidence": {"type": "array", "items": {"type": "integer"}},
            "quote": {"type": ["string", "null"]},
            "rationale": {"type": "string"},
        },
        "required": ["verdict", "evidence", "quote", "rationale"],
        "additionalProperties": False,
    }


# The hash covers everything that shapes a verdict: rubric, schema, sentence format, code
# version and the blinding rules.
JUDGE_PROMPT_HASH = hashlib.sha256(
    canonical_json(
        [
            JUDGE_RUBRIC,
            verdict_schema(),
            SENTENCE_FORMAT,
            JUDGE_CODE_VERSION,
            _BLIND_RE.pattern,
            _SPACED_RE.pattern,
            sorted(_STRONG),
            JUDGE_MAX_OUTPUT_TOKENS,
            sorted(_BLIND_FOLD.items()),
            sorted(_FOLD.items()),
        ]
    ).encode()
).hexdigest()[:16]


def _redact_match(m: re.Match[str]) -> str:
    s = m.group(0)
    words = [w for w in re.findall(r"[a-z]+", s.lower())]
    head = words[0]
    names = [w for w in words if w in _ALL or (w.endswith("s") and w[:-1] in _ALL)]
    if any(w in _STRONG or w[:-1] in _STRONG for w in names) or len(names) >= 2:
        return REDACTED
    rest = s[len(head) :]
    if re.search(r"\d\.\d|\d[a-z]", rest, re.I) or re.search(r"[-_]\d+[-_.]\d", rest):
        return REDACTED  # dotted, lettered or multi-part version
    if head in ("gpt", "gpts") and re.match(r"[-_]\d", rest):
        return REDACTED  # GPT-4
    return s


def redact(text: str) -> str:
    """Remove model and provider names before a judge sees text (B4, D-35, D-38). Dashes,
    no-break spaces and fullwidth ASCII are folded first; nothing else is changed."""
    folded = _SPACED_RE.sub(REDACTED, text.translate(_BLIND_FOLD))
    return _BLIND_RE.sub(_redact_match, folded)


class JudgeInput(Model):
    """Everything the judge sees, and nothing that names a model or provider (B4)."""

    claim_id: str
    claim_text: str
    cited: tuple[int, ...]
    sentences: tuple[str, ...]  # texts of the cited sentences, in `cited` order, redacted
    judge_id: Name
    effort: Name | None
    rubric_hash: Name = JUDGE_PROMPT_HASH  # in every cache key: a rubric change re-judges

    @field_validator("rubric_hash")
    @classmethod
    def _current(cls, v: str) -> str:
        if v != JUDGE_PROMPT_HASH:
            raise ValueError("rubric_hash must be the current JUDGE_PROMPT_HASH")
        return v


def render_judge_prompt(inp: JudgeInput) -> str:
    lines = "\n".join(
        SENTENCE_FORMAT.format(n=n, text=s) for n, s in zip(inp.cited, inp.sentences, strict=True)
    )
    return JUDGE_RUBRIC.format(claim=inp.claim_text, sentences=lines)


def precheck(claim: Claim, case: Case, judge: str) -> ClaimVerdict | None:
    """B3: no citation, or a citation that does not exist, is unsupported without a call."""
    n = len(case.context)
    bad = [c for c in claim.cited if not 1 <= c <= n]
    if not claim.cited or bad:
        reason = "cites no sentence" if not claim.cited else f"cites missing sentence(s) {bad}"
        return ClaimVerdict(
            claim_id=claim.id,
            judge=judge,
            verdict="unsupported",
            evidence=(),
            rationale=f"precheck: {reason}",
            judge_prompt_hash=JUDGE_PROMPT_HASH,
        )
    return None


def judge_input(claim: Claim, case: Case, judge: str, effort: str | None) -> JudgeInput:
    return JudgeInput(
        claim_id=claim.id,
        claim_text=redact(claim.text),
        cited=claim.cited,
        sentences=tuple(redact(case.context[c - 1]) for c in claim.cited),
        judge_id=judge,
        effort=effort,
    )


def _norm(text: str) -> str:
    """Compare quotes modulo Unicode form, typographic quotes and dashes, case, whitespace,
    and spacing before punctuation or around comparison signs (D-38)."""
    t = unicodedata.normalize("NFKC", text).translate(_FOLD).translate(_BLIND_FOLD).casefold()
    t = " ".join(t.split())
    t = re.sub(r"\s*([=<>\u2264\u2265\u00b1])\s*", r"\1", t)
    return re.sub(r"\s+(?=[%,.;:)])|(?<=[(])\s+", "", t)


def _find_word(hay: str, piece: str, start: int) -> int:
    """First index >= start where `piece` occurs without cutting a word in half."""
    i = hay.find(piece, start)
    while i >= 0:
        before = hay[i - 1] if i > 0 else " "
        after = hay[i + len(piece)] if i + len(piece) < len(hay) else " "
        cut_left = piece[0].isalnum() and before.isalnum()
        cut_right = piece[-1].isalnum() and after.isalnum()
        if not cut_left and not cut_right:
            return i
        i = hay.find(piece, i + 1)
    return -1


def quote_in(quote: str, sentences: Sequence[str]) -> bool:
    """True if the quote appears in one cited sentence on word boundaries. "..." elisions must
    match in order; a quote needs at least three letters or digits, so "." cannot pass."""
    q = _norm(quote)
    q = re.sub(r"^\[\d+\]\s*", "", q)  # the prompt shows "[n] " before each sentence
    q = re.sub(r"\[\s*\.\.\.\s*\]|\(\s*\.\.\.\s*\)", "...", q)  # [...] and (...)
    q = q.strip().strip("\"'").strip()  # surrounding quote marks
    pieces = [p.strip() for p in q.split("...") if p.strip()]
    if (
        not pieces
        or any(not re.search(r"\w", p) for p in pieces)
        or sum(ch.isalnum() for p in pieces for ch in p) < 3
    ):
        return False
    for s in sentences:
        hay, pos = _norm(s), 0
        for piece in pieces:
            found = _find_word(hay, piece, pos)
            if found < 0:
                break
            pos = found + len(piece)
        else:
            return True
    return False


def parse_verdict(raw_text: str, inp: JudgeInput) -> ClaimVerdict:
    try:
        data = json.loads(_extract_json(raw_text))
    except ValueError as exc:
        raise SchemaError(f"verdict is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"verdict", "evidence", "quote", "rationale"}:
        raise SchemaError("verdict must have exactly verdict, evidence, quote, rationale")
    verdict, evidence, quote, rationale = (
        data[k] for k in ("verdict", "evidence", "quote", "rationale")
    )
    if verdict not in ("supported", "unsupported"):
        raise SchemaError(f"verdict {verdict!r} is not supported/unsupported")
    if not isinstance(evidence, list) or any(type(e) is not int for e in evidence):
        raise SchemaError("evidence must be a list of integers")
    outside = sorted(set(evidence) - set(inp.cited))
    if outside:
        raise SchemaError(f"evidence {outside} is not among the claim's citations")
    if verdict == "supported" and not evidence:
        raise SchemaError("a supported verdict must name its evidence")
    if verdict == "unsupported" and evidence:
        raise SchemaError("an unsupported verdict cannot cite evidence")
    if quote is not None:
        if not isinstance(quote, str) or not quote.strip():
            raise SchemaError("quote must be a non-empty string or null")
        if not quote_in(quote, inp.sentences):
            raise SchemaError("quote does not appear in the cited sentences (B5)")
    if not isinstance(rationale, str):
        raise SchemaError("rationale must be a string")
    return ClaimVerdict(
        claim_id=inp.claim_id,
        judge=inp.judge_id,
        verdict=verdict,
        evidence=tuple(sorted(set(evidence))),
        quote=quote,
        rationale=rationale,
        judge_prompt_hash=JUDGE_PROMPT_HASH,
    )


def _request(inp: JudgeInput) -> CompletionRequest:
    return CompletionRequest(
        model="judge",
        model_id=inp.judge_id,
        prompt=render_judge_prompt(inp),
        max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS,
        params={"effort": inp.effort} if inp.effort else {},
        json_schema=verdict_schema(),
    )


def _cache_key(inp: JudgeInput) -> str:
    return sha256_hex({"node": "judge", "code_version": JUDGE_CODE_VERSION, "input": inp})


def custom_id_for(key: str) -> str:
    """Provider-safe batch id (^[a-zA-Z0-9_-]{1,64}$) that reveals nothing about the claim."""
    return "j" + key[:40]


@dataclass
class JudgingResult:
    judge: str
    verdicts: dict[str, ClaimVerdict] = field(default_factory=dict)  # claim_id -> verdict
    status: Literal["complete", "pending"] = "complete"
    n_errors: int = 0  # claims excluded for this judge after a retry, or failed this run
    cost_usd: float = 0.0
    batch_ids: list[str] = field(default_factory=list)


class _Cached(Model):
    verdict: ClaimVerdict | None
    error: str | None = None
    cost_usd: float = 0.0


_META = "judge_batch"


def _store_verdict(
    executor: Executor, key: str, verdict: ClaimVerdict | None, err: str | None, cost: float
) -> None:
    ts = datetime.now(UTC)
    executor.store.put(
        key,
        _Cached(verdict=verdict, error=err, cost_usd=cost),
        Lineage(key=key, node="judge", started_at=ts, finished_at=ts, cost_usd=cost),
    )
    executor.store.delete_meta(key, _META)


def _estimate(price: Price, req: CompletionRequest, *, batch: bool) -> float:
    return call_cost(
        price, tokens_in=len(req.prompt) // 3 + 1, tokens_out=req.max_output_tokens, batch=batch
    )


_FATAL_STATUS = frozenset({401, 403, 404})
_FATAL_NAMES = frozenset({"AuthenticationError", "PermissionDeniedError", "NotFoundError"})


def _is_fatal(exc: BaseException) -> bool:
    """Configuration problems that will fail every call: stop the run instead of excluding."""
    if getattr(exc, "status_code", None) in _FATAL_STATUS:
        return True
    return any(cls.__name__ in _FATAL_NAMES for cls in type(exc).__mro__)


async def _retry_live(
    executor: Executor, adapter: ModelAdapter, inp: JudgeInput, price: Price
) -> tuple[ClaimVerdict | None, str | None, float]:
    """Tech Spec §2.4, D-35: one live retry with the same prompt for an unparseable verdict.
    Returns (verdict, error label, cost). Budget and provider exceptions propagate."""
    req = _request(inp)
    estimate = _estimate(price, req, batch=False)
    await executor.reserve(estimate)
    cost = 0.0
    try:
        raw = await adapter.complete(req)
        cost = raw.cost_usd
        if raw.refusal:
            return None, "refusal", cost
        try:
            return parse_verdict(raw.raw_text, inp), None, cost
        except SchemaError:
            return None, "schema", cost
    finally:
        await executor.settle(estimate, cost)


def _forget(executor: Executor, key: str, result: JudgingResult) -> None:
    """Leave a claim unjudged and uncached so the next run resubmits it; count it now."""
    executor.store.delete_meta(key, _META)
    result.n_errors += 1


# One collect (status check plus downloading a finished batch's results, which for ~20k claims
# can take minutes) may run this long past max_wait before it is abandoned as "not ready".
# Adapters bound their own retries, so this is a backstop against a hung connection.
COLLECT_TIMEOUT_FLOOR_S = 900.0


async def judge_claims(
    executor: Executor,
    items: Sequence[tuple[Case, Claim]],
    judge: str,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    *,
    run_id: str,
    poll_interval_s: float = 30.0,
    max_wait_s: float = 0.0,
) -> JudgingResult:
    """Judge every claim with one judge. Cached verdicts are reused; claims already in a
    submitted batch wait for that batch; the rest go into one new batch. With max_wait_s=0
    the call returns "pending" instead of waiting for unfinished batches.

    Budget: a new batch reserves its estimate and keeps it held until collected; on resume,
    batches an earlier process submitted are re-held first (their money is committed), so
    fresh claims only get what is left under the cap. If fresh claims do not fit, the batches
    already paid for are still collected before BudgetExceeded is raised.

    The whole call holds a per-judge lock, so concurrent calls for one judge never submit,
    collect or retry the same claims twice; the second caller finds them cached. The lock is
    held while polling too, so callers should judge all claims for a judge in one call."""
    provider = provider_for(judge, config.endpoints)
    adapter = adapters[adapter_key(provider, judge)]
    price = price_for(prices, judge, free_if_unknown=provider == "openai_compatible")
    result = JudgingResult(judge=judge)
    by_batch: dict[str, dict[str, JudgeInput]] = {}  # batch id -> custom id -> input
    budget_stop: BudgetExceeded | None = None

    async with executor.named_lock(f"judge:{judge}"):
        fresh: dict[str, JudgeInput] = {}
        held: dict[str, float] = {}
        for case, claim in items:
            pre = precheck(claim, case, judge)
            if pre is not None:
                result.verdicts[claim.id] = pre
                continue
            inp = judge_input(claim, case, judge, config.judge_effort)
            key = _cache_key(inp)
            cached = executor.store.get(key, _Cached)
            if cached is not None:
                if cached.verdict is not None:
                    result.verdicts[claim.id] = cached.verdict
                else:
                    result.n_errors += 1
                continue
            meta = executor.store.get_meta(key, _META)
            if meta is not None:
                bid = meta["batch_id"]
                by_batch.setdefault(bid, {})[custom_id_for(key)] = inp
                est = meta.get("estimate")
                if not isinstance(est, int | float):  # meta from older code: recompute
                    est = _estimate(price, _request(inp), batch=True)
                held[bid] = held.get(bid, 0.0) + float(est)
            else:
                fresh[custom_id_for(key)] = inp
        for bid, est in held.items():
            await executor.rehold_batch(bid, est)

        if fresh:
            reqs = {cid: _request(inp) for cid, inp in fresh.items()}
            item_est = {cid: _estimate(price, r, batch=True) for cid, r in reqs.items()}
            estimate = sum(item_est.values())
            try:
                await executor.reserve(estimate)
            except BudgetExceeded as exc:
                budget_stop = exc  # collect what is already paid for, then stop
                result.n_errors += len(fresh)
            else:
                try:
                    batch_id = await adapter.submit_batch(
                        [BatchRequest(custom_id=cid, request=r) for cid, r in reqs.items()]
                    )
                except BaseException:
                    await executor.settle(estimate, 0.0)
                    raise
                await executor.hold_batch(batch_id, estimate)  # stays reserved until collected
                for cid, inp in fresh.items():
                    executor.store.put_meta(
                        _cache_key(inp),
                        _META,
                        {"batch_id": batch_id, "provider": provider, "estimate": item_est[cid]},
                    )
                by_batch[batch_id] = dict(fresh)

        deadline = time.monotonic() + max_wait_s
        open_batches = dict(by_batch)
        while open_batches:
            for batch_id in list(open_batches):
                batch = open_batches[batch_id]
                reqs = {cid: _request(inp) for cid, inp in batch.items()}
                timeout = max(deadline - time.monotonic(), COLLECT_TIMEOUT_FLOOR_S)
                try:
                    collected = await asyncio.wait_for(
                        adapter.collect_batch(batch_id, reqs), timeout
                    )
                except TimeoutError:
                    collected = None  # treat a hung status check as "not ready yet"
                if collected is None:
                    continue
                del open_batches[batch_id]
                result.batch_ids.append(batch_id)
                stop = await _process_batch(
                    executor, adapter, price, batch_id, batch, collected, result
                )
                budget_stop = budget_stop or stop
            if not open_batches:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.status = "pending"
                result.batch_ids.extend(open_batches)
                break
            await asyncio.sleep(min(poll_interval_s, remaining))
    if budget_stop is not None:
        raise budget_stop
    return result


async def _process_batch(
    executor: Executor,
    adapter: ModelAdapter,
    price: Price,
    batch_id: str,
    batch: Mapping[str, JudgeInput],
    collected: Sequence[Any],
    result: JudgingResult,
) -> BudgetExceeded | None:
    """Parse and cache one collected batch. The batch's hold is always released and the cost
    of every claim resolved here (cached or forgotten) is recorded exactly once, even if
    processing stops early (N2); claims not reached keep their batch meta and are collected,
    and paid for, next run. Returns a budget stop instead of raising it."""
    spent = 0.0
    budget_stop: BudgetExceeded | None = None
    seen: set[str] = set()
    try:
        for br in collected:
            inp = batch.get(br.custom_id)
            if inp is None or br.custom_id in seen:  # not ours, or a duplicate line
                continue
            seen.add(br.custom_id)
            key = _cache_key(inp)
            raw: RawCompletion | None = br.completion
            cost = raw.cost_usd if raw else 0.0
            done = executor.store.get(key, _Cached)
            if done is not None:  # resolved by another process: report it, pay nothing more
                if done.verdict is not None:
                    result.verdicts[inp.claim_id] = done.verdict
                else:
                    result.n_errors += 1
                continue
            if raw is None:  # expired, cancelled or errored item: resubmit next run
                _forget(executor, key, result)
                spent += cost
                continue
            if raw.refusal:
                _store_verdict(executor, key, None, "refusal", cost)
                result.n_errors += 1
                spent += cost
                continue
            try:
                verdict, err = parse_verdict(raw.raw_text, inp), None
            except SchemaError:
                verdict, err = None, "schema"
                if budget_stop is None:
                    try:
                        verdict, err, retry_cost = await _retry_live(executor, adapter, inp, price)
                        result.cost_usd += retry_cost  # settled by _retry_live (N1)
                    except BudgetExceeded as exc:
                        budget_stop = exc
                        _forget(executor, key, result)
                        spent += cost
                        continue
                    except Exception as exc:
                        if _is_fatal(exc):
                            raise
                        # Transient or not (a 400 such as "credit balance too low" can clear),
                        # a provider error is never cached: count it, resubmit next run.
                        _forget(executor, key, result)
                        spent += cost
                        continue
                else:
                    _forget(executor, key, result)
                    spent += cost
                    continue
            _store_verdict(executor, key, verdict, err, cost)
            spent += cost
            if verdict is not None:
                result.verdicts[inp.claim_id] = verdict
            else:
                result.n_errors += 1
        for cid in set(batch) - seen:  # the provider returned nothing for it (N4)
            _forget(executor, _cache_key(batch[cid]), result)
    finally:
        await executor.settle_batch(batch_id, spent)  # releases the hold exactly once
        result.cost_usd += spent
    return budget_stop


async def judge_claims_live(
    executor: Executor,
    items: Sequence[tuple[Case, Claim]],
    judge: str,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    *,
    run_id: str,
    concurrency: int = 8,
) -> JudgingResult:
    """Judge every claim through the normal API (OWNER D-44 `--live-judges`): full price, no
    waiting. Same prompt, parsing, single retry and cache keys as batch judging, so verdicts
    from either path are interchangeable and a later batch run never pays for them again."""
    provider = provider_for(judge, config.endpoints)
    adapter = adapters[adapter_key(provider, judge)]
    price = price_for(prices, judge, free_if_unknown=provider == "openai_compatible")
    result = JudgingResult(judge=judge)
    sem = asyncio.Semaphore(max(1, config.concurrency.get(provider, concurrency)))

    async def one(case: Case, claim: Claim) -> None:
        pre = precheck(claim, case, judge)
        if pre is not None:
            result.verdicts[claim.id] = pre
            return
        inp = judge_input(claim, case, judge, config.judge_effort)
        key = _cache_key(inp)
        cached = executor.store.get(key, _Cached)
        if cached is not None:
            if cached.verdict is not None:
                result.verdicts[claim.id] = cached.verdict
            else:
                result.n_errors += 1
            return
        if executor.store.get_meta(key, _META) is not None:
            # Already in a submitted batch that the provider will bill: never pay twice.
            # The claim stays pending until a batch run collects it.
            result.status = "pending"
            return
        async with sem:
            req = _request(inp)
            estimate = _estimate(price, req, batch=False)
            await executor.reserve(estimate)
            cost = 0.0
            try:
                raw = await adapter.complete(req)
                cost = raw.cost_usd
            except Exception as exc:
                if _is_fatal(exc):
                    raise
                result.n_errors += 1  # provider error: never cached, retried next run
                return
            finally:
                await executor.settle(estimate, cost)
            result.cost_usd += cost
            if raw.refusal:
                _store_verdict(executor, key, None, "refusal", cost)
                result.n_errors += 1
                return
            try:
                verdict, err = parse_verdict(raw.raw_text, inp), None
            except SchemaError:
                try:
                    verdict, err, retry_cost = await _retry_live(executor, adapter, inp, price)
                except Exception as exc:
                    if _is_fatal(exc) or isinstance(exc, BudgetExceeded):
                        raise
                    result.n_errors += 1
                    return
                result.cost_usd += retry_cost
                cost += retry_cost
        _store_verdict(executor, key, verdict, err, cost)
        if verdict is not None:
            result.verdicts[claim.id] = verdict
        else:
            result.n_errors += 1

    async with executor.named_lock(f"judge:{judge}"):
        # Let every in-flight call finish and settle its spend, then surface the first stop.
        outcomes = await asyncio.gather(
            *(one(case, claim) for case, claim in items), return_exceptions=True
        )
    for o in outcomes:
        if isinstance(o, BaseException):
            raise o
    return result


def groundedness(
    claims: Sequence[Claim], verdicts: Mapping[str, ClaimVerdict]
) -> tuple[float, bool]:
    """(share of judged claims supported, fully grounded). Fully grounded needs every claim
    judged and supported, and at least one claim (JDG-06)."""
    judged = [verdicts[c.id] for c in claims if c.id in verdicts]
    if not judged:
        return 0.0, False
    supported = sum(v.verdict == "supported" for v in judged)
    fully = len(judged) == len(claims) and supported == len(claims)
    return supported / len(judged), fully


class PlantedClaim(Model):
    claim: str
    sentences: tuple[str, ...]  # what the judge sees; the claim is true in general but absent


PLANTED_CLAIMS: tuple[PlantedClaim, ...] = tuple(
    PlantedClaim(claim=c, sentences=(s,))
    for c, s in [
        ("Aspirin reduces the risk of heart attack.", "Patients received aspirin for 12 weeks."),
        ("Smoking causes lung cancer.", "Participants reported their smoking history."),
        ("Insulin lowers blood glucose.", "Insulin doses were recorded daily."),
        ("Vaccines prevent measles.", "Vaccination records were reviewed for 400 children."),
        ("Exercise improves cardiovascular fitness.", "Participants described exercise habits."),
        ("Statins lower LDL cholesterol.", "Statin use was documented at baseline."),
        ("Antibiotics do not treat viral infections.", "Antibiotic prescriptions were counted."),
        ("Obesity increases the risk of type 2 diabetes.", "Body mass index was measured."),
        ("Hand washing reduces infection transmission.", "Hand hygiene audits ran monthly."),
        ("Folic acid reduces neural tube defects.", "Folic acid intake was assessed."),
    ]
)


def planted_inputs(judge: str, effort: str | None = "medium") -> list[JudgeInput]:
    return [
        JudgeInput(
            claim_id=f"planted:{i}",
            claim_text=p.claim,
            cited=(1,),
            sentences=p.sentences,
            judge_id=judge,
            effort=effort,
        )
        for i, p in enumerate(PLANTED_CLAIMS)
    ]


def planted_claim_check(verdicts: Iterable[ClaimVerdict]) -> list[str]:
    """Problems with a judge's planted-claim verdicts (B6); empty means the judge passed."""
    got = {v.claim_id: v for v in verdicts}
    problems = []
    for i in range(len(PLANTED_CLAIMS)):
        v = got.get(f"planted:{i}")
        if v is None:
            problems.append(f"planted:{i} has no verdict")
        elif v.verdict != "unsupported":
            problems.append(f"planted:{i} marked supported: the judge used outside knowledge (B6)")
    return problems


class JudgeSelection(Model):
    judges: tuple[Name, ...]
    judge_mode: JudgeMode
    n_available: NonNegInt


def select_judges(config: RunConfig, available_providers: Iterable[str]) -> JudgeSelection:
    """Dual mode needs two usable judges from different providers (cross-family, D-4);
    otherwise single (D-10). No usable judge is an error."""
    if len(config.judges) > 2:
        raise ValueError("at most two judges are supported")
    available = set(available_providers)
    usable = tuple(j for j in config.judges if provider_for(j, config.endpoints) in available)
    if not usable:
        raise ValueError(
            f"no judge is usable: {list(config.judges)} need {sorted(available) or 'keys'}"
        )
    families = {provider_for(j, config.endpoints) for j in usable}
    if len(usable) == 2 and len(families) == 2:
        return JudgeSelection(judges=usable, judge_mode="dual", n_available=2)
    return JudgeSelection(judges=usable[:1], judge_mode="single", n_available=len(usable))
