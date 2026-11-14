"""`compare()` (D3.1, CMP-01/03/06): one answer card per model for a question and an abstract.

Live mode: every model answers in parallel; each answer is split into claims and checked by one
judge (live, not batch), each model's pipeline under its own stage timeouts and its own share of
the request's cost cap, so a slow or expensive model never blocks or starves the others.
Everything runs on in-memory stores: the question and the abstract are never written to disk
(CMP-02). A guard, when given, checks the passcode before any outside call, then reserves the
request's cap against today's budget and is settled with the actual spend once every pipeline
has finished (D-54). Replay mode reads cards that `evalkit demo-cache` prepared (D3.2) and
makes no calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from evalkit.adapters import ModelAdapter
from evalkit.cost import (
    STRICT_TOKENS,
    Price,
    TokenAssumptions,
    estimate_run_cost,
    estimate_tokens,
    price_for,
)
from evalkit.demo.guard import Guard, Ticket
from evalkit.demo.pubmed import fetch_abstract
from evalkit.pipeline.extract import (
    ExtractClaims,
    ExtractInput,
    extract_all,
    ground_on_answer_citations,
)
from evalkit.pipeline.generate import (
    Generate,
    Generated,
    generate_and_validate,
    generate_inputs,
    provider_for,
)
from evalkit.pipeline.judge import groundedness, judge_claims_live, live_call_estimate
from evalkit.runtime import BudgetExceeded, Executor, MemoryStore, RunContext
from evalkit.schemas import Case, Claim, RunConfig
from evalkit.schemas._base import Model, NonNegFloat, NonNegInt
from evalkit.tasks import builtin_task

GENERATE_TIMEOUT_S = 15.0  # Tech Spec §7: four parallel generations under 15 s
GRADE_TIMEOUT_S = 10.0  # extraction plus one judge under 10 s
MAX_QUESTION_CHARS = 500
MAX_SENTENCES = 60
MAX_ABSTRACT_CHARS = 12_000
MAX_ABSTRACT_TOKENS = 6_000  # FR16's per-request token cap, strict count (one per byte)
MAX_CLAIMS = 8  # claims judged per answer in live mode (the rest are noted, not judged)
LIVE_JUDGE_MAX_TOKENS = 800  # hard cap per live verdict: makes each call's cost bounded
JUDGE_CALLS = MAX_CLAIMS + 2  # budget room for a couple of unparseable-verdict retries
WORST_ANSWER_CHARS = 1024 * 5  # an answer at its output limit (1,024 tokens)
WORST_CLAIM_CHARS = 1500
NOTICE = (
    "Research demo, not medical advice. These answers summarise a study abstract; they are not "
    "guidance for any person's treatment. Ask a clinician about your own situation."
)
LIVE_NOTE = "Live mode checks claims with one judge ({judge}); the benchmark uses two."

# Questions about a person's own (or someone they care for) care rather than what a study found
# (B7). Erring towards showing the notice: a false positive costs a sentence, a miss does not.
_ADVICE = re.compile(
    r"\b("
    r"should (i|we|you|he|she|they)|can (i|we)|could (i|we)|would (i|we)|do (i|we) need|"
    r"am i|are we|is it (ok|okay|safe|wise|bad|dangerous) (for|to)|safe to (take|use|give|"
    r"stop|start|mix|combine|eat|drink)|is \w+ (ok|okay|safe) (for|with|while|during)|"
    r"(do|would) you recommend|recommend(ed)? for (me|my|a \d+)|for me\b|"
    r"(my|our) (doctor|gp|child|kid|baby|son|daughter|mother|mom|mum|father|dad|wife|husband|"
    r"partner|parents?|symptoms?|medications?|meds|condition|treatment|prescription|dose)|"
    r"i('m| am) (on|taking|pregnant|breastfeeding)|i have|i've been|i was (told|prescribed)|"
    r"we('re| are) (on|taking)|while (pregnant|breastfeeding)|"
    r"how much .{0,30}(should|can|do) (i|we|you) (take|give)|what dose (should|for)|"
    r"(too much|too little) (\w+ )?(for|to take)|is \d+ ?(mg|mcg|iu|g)\b.{0,20}(too much|safe|ok)|"
    r"stop taking|start taking|switch (my|our|him|her|them)"
    r")",
    re.IGNORECASE,
)


def is_advice_seeking(question: str) -> bool:
    return bool(_ADVICE.search(question))


@dataclass(frozen=True)
class Abstract:
    sentences: tuple[str, ...]


@dataclass(frozen=True)
class PMID:
    id: str


class CitedSentence(Model):
    n: NonNegInt
    text: str


class ClaimMark(Model):
    text: str
    verdict: Literal["supported", "unsupported", "unchecked"]


class AnswerCard(Model):
    model: str
    label: str | None = None
    answer: str | None = None
    cited: tuple[CitedSentence, ...] = ()
    groundedness: float | None = None  # share of the judged claims the judge supported
    fully_grounded: bool | None = None
    n_claims: NonNegInt = 0
    claims: tuple[ClaimMark, ...] = ()  # each claim marked as the judge found it (Flow B)
    judge: str | None = None
    cost_usd: NonNegFloat = 0.0  # everything this card cost: answer, extraction and judging
    latency_ms: NonNegInt = 0
    error: str | None = None  # "timed out", "transport", "cost cap", "refusal", "schema", ...
    notice: str | None = None  # the research-only notice, on every card (B7, D-56)
    advice: bool = False  # the question asks about someone's own care: show the notice loudly
    note: str | None = None  # e.g. live mode uses one judge


def clean_sentences(sentences: Sequence[str]) -> tuple[str, ...]:
    return tuple(s.strip() for s in sentences if s.strip())


def replay_key(question: str, sentences: Sequence[str]) -> str:
    """Identity of a (question, abstract) pair; the same cleaning live mode applies."""
    payload = json.dumps([question.strip(), list(clean_sentences(sentences))])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def check_limits(question: str, sentences: Sequence[str]) -> None:
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"the question is longer than {MAX_QUESTION_CHARS} characters")
    if not sentences:
        raise ValueError("the abstract is empty")
    too_long = (
        len(sentences) > MAX_SENTENCES
        or sum(len(s) for s in sentences) > MAX_ABSTRACT_CHARS
        or estimate_tokens(*sentences) > MAX_ABSTRACT_TOKENS  # non-ASCII text counts by bytes
    )
    if too_long:
        raise ValueError(
            f"the abstract is too long for live mode (at most {MAX_SENTENCES} sentences, "
            f"{MAX_ABSTRACT_CHARS:,} characters and {MAX_ABSTRACT_TOKENS:,} bytes of text)"
        )


def estimate_request_usd(
    config: RunConfig,
    prices: dict[str, Price],
    judge: str,
    question: str = "",
    sentences: Sequence[str] = (),
) -> float:
    """§9 formula for one live request (n = 1, one judge, no batch), with prompt sizes taken
    from the actual question and abstract rather than the averages."""
    a = TokenAssumptions()
    tokens = estimate_tokens(question, *sentences) + 2 * len(sentences)
    a = a.model_copy(
        update={
            "t_in": max(a.t_in, 300 + tokens),
            "t_j_in": max(a.t_j_in, 250 + tokens),
            "claims_per_answer": min(a.claims_per_answer, MAX_CLAIMS),
        }
    )
    return estimate_run_cost(
        prices,
        models=config.models,
        extractor=config.extractor,
        judges=(judge,),
        n_cases=1,
        assumptions=a,
        batch_judges=False,
    ).total_usd


def model_bound_usd(
    model: str, case: Case, config: RunConfig, prices: dict[str, Price], judge: str
) -> float:
    """The most one model's live pipeline can reserve, built from the very estimates the
    executor reserves: its answer at the output limit, extracting a worst-case answer, and
    JUDGE_CALLS verdicts capped at LIVE_JUDGE_MAX_TOKENS. Every call stops at its output
    limit, so this is a true upper bound on the model's spend (D-54)."""
    one = config.model_copy(update={"models": (model,)})
    ctx = RunContext(run_id="demo", seed=config.seed)
    task = builtin_task("pubmedqa")
    gen = Generate({}, prices).estimate_cost(generate_inputs([case], task, one)[0], ctx)
    all_cited = tuple(range(1, len(case.context) + 1))
    ext_inp = ExtractInput(
        case_id=case.id,
        model=model,
        question=case.question,
        answer_text="x" * WORST_ANSWER_CHARS,
        citations=all_cited,
        extractor_id=config.extractor,
        provider=provider_for(config.extractor, config.endpoints),
        effort=config.extractor_effort,
    )
    ext = ExtractClaims({}, prices).estimate_cost(ext_inp, ctx)
    claim = Claim(
        id=f"{case.id}:{model}:0",
        text="x" * WORST_CLAIM_CHARS,
        cited=all_cited,
        extractor_version="bound",
    )
    per_verdict = live_call_estimate(
        case, claim, judge, config.judge_effort, price_for(prices, judge), LIVE_JUDGE_MAX_TOKENS
    )
    return gen + ext + JUDGE_CALLS * per_verdict


@contextlib.contextmanager
def strict_tokens() -> Iterator[None]:
    """Count every token estimate as one per UTF-8 byte (a true upper bound for any text),
    for everything run inside: the demo's bounds, limits and per-call reservations (D-54)."""
    token = STRICT_TOKENS.set(True)
    try:
        yield
    finally:
        STRICT_TOKENS.reset(token)


def request_bound_usd(
    question: str,
    sentences: Sequence[str],
    *,
    config: RunConfig,
    prices: dict[str, Price],
    judge: str | None = None,
) -> float:
    """The most one live request can spend (the sum of the models' bounds): what the guard
    holds against today's budget and checks against the per-request cap."""
    judge = judge or config.judges[0]
    live = config.model_copy(update={"judge_effort": "low", "extractor_effort": "low"})
    sents = clean_sentences(sentences)
    with strict_tokens():
        return _request_bound(question, sents, live, prices, judge)


def _request_bound(
    question: str, sents: tuple[str, ...], live: RunConfig, prices: dict[str, Price], judge: str
) -> float:
    case = Case(
        id=f"demo-{replay_key(question, sents)}",
        question=question.strip(),
        context=sents,
        source="demo",
    )
    return math.fsum(model_bound_usd(m, case, live, prices, judge) for m in live.models)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


async def _one_model(
    ex: Executor,
    case: Case,
    model: str,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    judge: str,
    run_id: str,
    gen_timeout: float,
    grade_timeout: float,
) -> AnswerCard:
    t0 = time.perf_counter()
    task = builtin_task("pubmedqa")
    one = config.model_copy(update={"models": (model,)})
    card = AnswerCard(model=model, judge=judge)

    def done(**update: object) -> AnswerCard:
        return card.model_copy(update={**update, "cost_usd": ex.spent_usd, "latency_ms": _ms(t0)})

    try:
        gen: list[Generated] = await asyncio.wait_for(
            generate_and_validate(ex, [case], task, one, adapters, prices, run_id=run_id),
            gen_timeout,
        )
    except TimeoutError:
        return done(error="timed out")
    except BudgetExceeded:
        return done(error="cost cap")
    c = gen[0].completion
    if c.answer is None:
        return done(error=c.error or "no answer")
    cites = sorted({n for n in c.answer.citations if 1 <= n <= len(case.context)})
    card = card.model_copy(
        update={
            "label": c.answer.label,
            "answer": c.answer.answer,
            "cited": tuple(CitedSentence(n=n, text=case.context[n - 1]) for n in cites),
        }
    )
    try:
        claim_sets = await asyncio.wait_for(
            extract_all(ex, gen, one, adapters, prices, run_id=run_id), grade_timeout
        )
        cs = ground_on_answer_citations(gen, claim_sets)[0]
        claims = cs.claims[:MAX_CLAIMS]
        res = await asyncio.wait_for(
            judge_claims_live(
                ex,
                [(case, cl) for cl in claims],
                judge,
                one,
                adapters,
                prices,
                run_id=run_id,
                max_output_tokens=LIVE_JUDGE_MAX_TOKENS,
            ),
            grade_timeout,
        )
    except TimeoutError:
        return done(error="grading timed out")
    except BudgetExceeded:
        return done(error="grading stopped at the cost cap")
    if cs.error is not None:  # the answer stands, but its claims could not be checked
        return done(error=f"claims not checked ({cs.error})")
    if not claims:
        return done(n_claims=0)
    extra = len(cs.claims) - len(claims)
    note = f"{extra} more claims not checked in live mode." if extra > 0 else None
    marks = tuple(
        ClaimMark(
            text=cl.text,
            verdict=res.verdicts[cl.id].verdict if cl.id in res.verdicts else "unchecked",
        )
        for cl in cs.claims
    )
    if any(cl.id not in res.verdicts for cl in claims):
        # a judge call failed: a share over the judged claims would look like a real score
        return done(
            error="claims not checked (judge)", n_claims=len(claims), claims=marks, note=note
        )
    share, fully = groundedness(claims, res.verdicts)
    return done(
        groundedness=share, fully_grounded=fully, n_claims=len(claims), claims=marks, note=note
    )


async def compare(
    question: str,
    source: Abstract | PMID,
    *,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    mode: Literal["live", "replay"] = "live",
    replay: Mapping[str, list[AnswerCard]] | None = None,
    guard: Guard | None = None,
    passcode: str = "",
    session: str = "",
    judge: str | None = None,
    fetch: Callable[[str], list[str]] = fetch_abstract,
    gen_timeout: float = GENERATE_TIMEOUT_S,
    grade_timeout: float = GRADE_TIMEOUT_S,
    on_card: Callable[[int, AnswerCard], None] | None = None,
) -> list[AnswerCard]:
    """One AnswerCard per model in `config.models`, in that order. `on_card(i, card)` is called
    as each model finishes (live mode), so a page can fill cards in as they arrive."""
    question = question.strip()
    if not question:
        raise ValueError("ask a question")
    advice = is_advice_seeking(question)
    if mode == "replay":
        if not isinstance(source, Abstract):
            raise ValueError("replay mode works from the prepared sample abstracts")
        cards = (replay or {}).get(replay_key(question, source.sentences))
        if cards is None:
            raise KeyError("no prepared answers for this question; use live mode")
        return [c.model_copy(update={"notice": NOTICE, "advice": advice}) for c in cards]
    with strict_tokens():
        if guard is not None:  # passcode first: a wrong one never triggers even the PubMed fetch
            guard.check_passcode(passcode)
        if isinstance(source, PMID):
            sentences = clean_sentences(await asyncio.to_thread(fetch, source.id))
        else:
            sentences = clean_sentences(source.sentences)
        check_limits(question, sentences)
        judge = judge or config.judges[0]
        # Live: speed over depth (one judge at low effort), and nothing reaches disk.
        live = config.model_copy(update={"judge_effort": "low", "extractor_effort": "low"})
        case = Case(
            id=f"demo-{replay_key(question, sentences)}",
            question=question,
            context=sentences,
            source="demo",
        )
        bounds = [model_bound_usd(m, case, live, prices, judge) for m in config.models]
        hold = math.fsum(bounds)
        ticket = None
        if guard is not None:
            ticket = await _reserve(guard, passcode, session, hold)
        # Each model's executor is capped at its own exact bound, so its card cost is exact and no
        # pipeline can starve another; cancelled calls count at their reservation.
        executors = [Executor(MemoryStore(), max_cost_usd=b, count_cancelled=True) for b in bounds]
        try:

            async def tracked(i: int, ex: Executor, m: str) -> AnswerCard:
                card = await _one_model(
                    ex,
                    case,
                    m,
                    live,
                    adapters,
                    prices,
                    judge,
                    "demo",
                    gen_timeout,
                    grade_timeout,
                )
                if on_card is not None:
                    on_card(i, card.model_copy(update=_decorate(card, judge, advice)))
                return card

            results = await asyncio.gather(
                *(
                    tracked(i, ex, m)
                    for i, (ex, m) in enumerate(zip(executors, config.models, strict=True))
                ),
                return_exceptions=True,  # every pipeline finishes (or fails) before settling
            )
        finally:
            spent = math.fsum(ex.spent_usd for ex in executors)
            if ticket is not None and guard is not None:
                await asyncio.to_thread(guard.settle, ticket, spent)
        cards = []
        for m, ex, r in zip(config.models, executors, results, strict=True):
            if isinstance(r, BaseException):
                if not isinstance(r, Exception):
                    raise r  # KeyboardInterrupt and friends are not a card
                r = AnswerCard(model=m, judge=judge, cost_usd=ex.spent_usd, error=_why(r))
            cards.append(r)
        return [c.model_copy(update=_decorate(c, judge, advice)) for c in cards]


def _why(exc: Exception) -> str:
    """A card's error text. Provider messages are not echoed: some quote part of the key."""
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return "the provider rejected the API key"
    return f"failed: {name}"


def _decorate(card: AnswerCard, judge: str, advice: bool) -> dict[str, object]:
    live_note = LIVE_NOTE.format(judge=judge)
    return {
        "notice": NOTICE,
        "advice": advice,
        "note": f"{live_note} {card.note}" if card.note else live_note,
    }


async def _reserve(guard: Guard, passcode: str, session: str, hold: float) -> Ticket:
    """Reserve in a worker thread (the database call blocks). If this request is cancelled
    meanwhile, wait for the reservation to land and release it: nothing ran."""
    task = asyncio.ensure_future(asyncio.to_thread(guard.reserve, passcode, session, hold))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Release from a callback, so further cancellations cannot skip it and a refusal that
        # lands after the cancel never replaces the CancelledError.
        loop = asyncio.get_running_loop()

        def release(t: asyncio.Future[Ticket]) -> None:
            if not t.cancelled() and t.exception() is None:
                loop.run_in_executor(None, guard.settle, t.result(), 0.0)

        task.add_done_callback(release)
        raise
