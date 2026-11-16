"""Demo page logic (D3.2): samples, the replay file, card rendering, the live flow and the
aggregate-only request log. The Streamlit app (demo/app.py) is a thin shell over this module,
so everything a visitor can trigger is testable without a browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from evalkit.adapters import ModelAdapter
from evalkit.cost import Price
from evalkit.demo.engine import (
    MAX_QUESTION_CHARS,
    NOTICE,
    PMID,
    Abstract,
    AnswerCard,
    check_limits,
    clean_sentences,
    compare,
    estimate_request_usd,
    is_advice_seeking,
    replay_key,
    request_bound_usd,
)
from evalkit.demo.guard import Guard, GuardRefused, PostgresSpend
from evalkit.demo.pubmed import PubMedError, fetch_abstract
from evalkit.schemas import RunConfig
from evalkit.schemas._base import Model, NonBlank

LOG = logging.getLogger("evalkit.demo")
BANNER = (
    "Research demo, not medical advice. Answers summarise study abstracts from PubMedQA or "
    "the abstract you provide; they are not guidance for anyone's treatment."
)
REPORT_URL = "https://biplob819.github.io/evalkit/"
# The four deployment secrets (DEM-06). Live mode stays off until all four are set.
SECRETS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "EVALKIT_DEMO_PASSCODE",
    "EVALKIT_DEMO_DATABASE_URL",
)
PER_REQUEST_CAP_USD = 2.0  # a typical abstract's strict bound is about $1.1


class Sample(Model):
    id: NonBlank
    question: NonBlank
    sentences: tuple[str, ...]
    source: str = ""


def load_samples(path: Path) -> list[Sample]:
    return [Sample.model_validate(s) for s in json.loads(path.read_text(encoding="utf-8"))]


def load_replay(path: Path) -> dict[str, list[AnswerCard]]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: [AnswerCard.model_validate(c) for c in v] for k, v in raw.items()}


def save_replay(path: Path, replay: Mapping[str, list[AnswerCard]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: [c.model_dump(mode="json") for c in v] for k, v in sorted(replay.items())}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sample_key(s: Sample) -> str:
    return replay_key(s.question, s.sentences)


def is_cached(replay: Mapping[str, list[AnswerCard]], s: Sample) -> bool:
    """A sample counts as cached only when every one of its cards has an answer: a card that
    failed (a provider blip) is answered again by the next `demo-cache` run."""
    cards = replay.get(sample_key(s))
    return bool(cards) and all(c.error is None and c.answer for c in cards)


def live_problem(env: Mapping[str, str]) -> str | None:
    """Why live mode is unavailable on this deployment, or None. Names secrets, never values."""
    missing = [k for k in SECRETS if not env.get(k)]
    if missing:
        return "Live mode is not configured on this deployment; replay mode works."
    return None


def make_guard(env: Mapping[str, str], connect: Callable[[str], Any] | None = None) -> Guard:
    """The live guard from the deployment secrets: passcode plus Postgres daily spend (D-53)."""
    return Guard(
        env["EVALKIT_DEMO_PASSCODE"],
        PostgresSpend(env["EVALKIT_DEMO_DATABASE_URL"], connect),
        per_request_cap=PER_REQUEST_CAP_USD,
    )


def log_request(
    *, mode: str, models: Sequence[str], latency_ms: int, cost_usd: float, outcome: str
) -> str:
    """One aggregate line per live request (NFR8): time, mode, models, latency, cost and an
    outcome word. Never the question, abstract, PMID, passcode, session token or IP."""
    line = json.dumps(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mode": mode,
            "models": list(models),
            "latency_ms": int(latency_ms),
            "cost_usd": round(float(cost_usd), 6),
            "outcome": outcome,
        },
        sort_keys=True,
    )
    LOG.info(line)
    return line


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def card_html(card: AnswerCard) -> str:
    """One answer card as HTML (escaped): label, answer, cited sentences, claim marks,
    groundedness, cost, latency and the research-only notice."""
    parts = [f"<div class='card'><h4>{escape(card.model)}</h4>"]
    if card.error and not card.answer:
        parts.append(f"<p class='err'>No answer: {escape(card.error)}</p>")
    else:
        parts.append(f"<p><b>Label:</b> {escape(card.label or '–')}</p>")
        parts.append(f"<p>{escape(card.answer or '')}</p>")
        if card.cited:
            parts.append(
                "<details><summary>Cited sentences</summary>"
                + "".join(f"<p>[{c.n}] {escape(c.text)}</p>" for c in card.cited)
                + "</details>"
            )
        if card.claims:
            marks = {
                "supported": "✓ supported",
                "unsupported": "✗ unsupported",
                "unchecked": "· not checked",
            }
            parts.append(
                "<ul class='claims'>"
                + "".join(f"<li>{marks[c.verdict]}: {escape(c.text)}</li>" for c in card.claims)
                + "</ul>"
            )
        parts.append(
            f"<p><b>Grounded:</b> {_pct(card.groundedness)}"
            + (" (every claim supported)" if card.fully_grounded else "")
            + f" · <b>cost</b> ${card.cost_usd:.4f} · <b>latency</b> "
            f"{card.latency_ms / 1000:.1f}s</p>"
        )
        if card.error:
            parts.append(f"<p class='err'>{escape(card.error)}</p>")
    if card.note:
        parts.append(f"<p class='note'>{escape(card.note)}</p>")
    if card.notice:
        cls = "notice loud" if card.advice else "notice"
        parts.append(f"<p class='{cls}'>{escape(card.notice)}</p>")
    parts.append("</div>")
    return "".join(parts)


class LiveResult(Model):
    cards: tuple[AnswerCard, ...] = ()
    refused: str | None = None  # a message safe to show the visitor
    bound_usd: float = 0.0
    estimate_usd: float = 0.0


def own_key_adapters(keys: Mapping[str, str], prices: dict[str, Price]) -> dict[str, ModelAdapter]:
    """Adapters on a visitor's own keys, for one request. The keys live only in these client
    objects: never in the environment, a file or a log line."""
    from evalkit.adapters import get_adapter

    out: dict[str, ModelAdapter] = {}
    if keys.get("anthropic"):
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=keys["anthropic"], max_retries=0)
        out["anthropic"] = get_adapter("anthropic", prices=prices, client=client)
    if keys.get("openai"):
        import openai

        client = openai.AsyncOpenAI(api_key=keys["openai"], max_retries=0)
        out["openai"] = get_adapter("openai", prices=prices, client=client)
    return out


async def run_live(
    question: str,
    abstract_text: str,
    pmid: str,
    *,
    passcode: str = "",
    session: str,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    guard: Guard,
    own_keys: Mapping[str, str] | None = None,
    on_card: Callable[[int, AnswerCard], None] | None = None,
    split: Callable[[str], list[str]] | None = None,
    fetch: Callable[[str], list[str]] | None = None,
) -> LiveResult:
    """The page's live request: build the source, run the guarded comparison, log one
    aggregate line. Refusals and input problems come back as `refused` messages.

    With `own_keys` ({"anthropic": ..., "openai": ...}) the request runs on the visitor's keys
    and `adapters` must be built on them (`own_key_adapters`): the passcode is still required
    (D-59), nothing is held against the owner's daily budget, and the per-request cap and the
    rate limit still apply. Without
    an OpenAI key the OpenAI models get a "no key" card; an Anthropic key is required because
    the claim extractor and the live judge are Claude models."""
    from evalkit.text.sentences import split_sentences

    t0 = time.perf_counter()
    fetch = fetch or fetch_abstract
    source: Abstract | PMID
    if pmid.strip():
        source = PMID(pmid.strip())
    else:
        source = Abstract(tuple((split or split_sentences)(abstract_text)))
    mode = "live" if own_keys is None else "own_keys"
    outcome, cards, refused = "ok", [], None
    bound = estimate = 0.0
    try:
        # first in both modes, as in compare(): a wrong passcode learns nothing (D-59)
        guard.check_passcode(passcode)
        if own_keys is not None and not own_keys.get("anthropic"):
            raise GuardRefused(
                "An Anthropic key is needed: the claim checker and the judge are Claude models."
            )
        q = question.strip()
        if not q:
            raise ValueError("ask a question")
        if len(q) > MAX_QUESTION_CHARS:
            raise ValueError(f"the question is longer than {MAX_QUESTION_CHARS} characters")
        slot_taken = False
        if isinstance(source, PMID):
            if own_keys is not None:  # no budget hold here: the rate limit comes before NCBI
                guard.admit(session, 0.0)
                slot_taken = True
            # fetched here so the request's bound is known (and shown) before any model call
            source = Abstract(tuple(await asyncio.to_thread(fetch, source.id)))
        if own_keys is None:
            bound, estimate = _bounds(question, source, config, prices)
            cards = await compare(
                question,
                source,
                config=config,
                adapters=adapters,
                prices=prices,
                guard=guard,
                passcode=passcode,
                session=session,
                on_card=on_card,
                fetch=fetch,
            )
        else:
            cards, bound, estimate = await _run_own_keys(
                question,
                source,
                own_keys,
                session,
                config,
                adapters,
                prices,
                guard,
                on_card,
                slot_taken=slot_taken,
            )
    except GuardRefused as exc:
        outcome, refused = "refused", exc.reason
    except (ValueError, PubMedError) as exc:
        outcome, refused = "bad_input", str(exc)
    cost = sum(c.cost_usd for c in cards)
    log_request(
        mode=mode,
        models=config.models,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        cost_usd=cost,
        outcome=outcome,
    )
    return LiveResult(cards=tuple(cards), refused=refused, bound_usd=bound, estimate_usd=estimate)


def _bounds(
    question: str, source: Abstract, config: RunConfig, prices: dict[str, Price]
) -> tuple[float, float]:
    sentences = clean_sentences(source.sentences)
    check_limits(question.strip(), sentences)
    bound = request_bound_usd(question, sentences, config=config, prices=prices)
    return bound, estimate_request_usd(config, prices, config.judges[0], question, sentences)


async def _run_own_keys(
    question: str,
    source: Abstract,
    keys: Mapping[str, str],
    session: str,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    guard: Guard,
    on_card: Callable[[int, AnswerCard], None] | None,
    *,
    slot_taken: bool = False,
) -> tuple[list[AnswerCard], float, float]:
    from evalkit.pipeline.generate import provider_for

    keyed = [m for m in config.models if keys.get(provider_for(m, config.endpoints))]
    sub = config.model_copy(update={"models": tuple(keyed)})
    bound, estimate = _bounds(question, source, sub, prices)
    if slot_taken:  # a PMID request already took its rate-limit slot before the fetch
        guard.check_cap(bound)
    else:
        guard.admit(session, bound)  # the cap protects the visitor's keys too
    index = [config.models.index(m) for m in keyed]
    advice = is_advice_seeking(question)
    missing = {
        m: AnswerCard(
            model=m,
            judge=config.judges[0],
            error=f"no {provider_for(m, config.endpoints)} key given",
            notice=NOTICE,
            advice=advice,
        )
        for m in config.models
        if m not in keyed
    }
    if on_card is not None:  # shown at once, not after the keyed models finish
        for m, card in missing.items():
            on_card(config.models.index(m), card)

    def mapped(i: int, card: AnswerCard) -> None:
        if on_card is not None:
            on_card(index[i], card)

    got = await compare(
        question, source, config=sub, adapters=adapters, prices=prices, on_card=mapped
    )
    by_model = {c.model: c for c in got}
    return [by_model.get(m) or missing[m] for m in config.models], bound, estimate


async def build_replay(
    samples: Sequence[Sample],
    replay: dict[str, list[AnswerCard]],
    *,
    config: RunConfig,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    redo: bool = False,
) -> int:
    """Answer every sample not yet cached in `replay` (live, unguarded: this is the owner's
    run). Returns how many were answered; fully answered samples are never paid for again,
    and with `redo` a sample with a failed card is answered again."""
    added = 0
    for s in samples:
        key = sample_key(s)
        if is_cached(replay, s) or (key in replay and not redo):
            continue
        replay[key] = await compare(
            s.question, Abstract(s.sentences), config=config, adapters=adapters, prices=prices
        )
        added += 1
    return added
