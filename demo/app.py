"""EvalKit demo page (D3.2): four models answer one question from a study abstract, and every
claim is checked against the sentences each answer cites.

Replay mode (the default) shows prepared answers and makes no model call. Live mode needs the
passcode, on the owner's keys or the visitor's own (D-59); it is guarded per request and per
day (evalkit.demo.guard). All logic lives in
evalkit.demo.page so it is testable without a browser; this file is layout only.

Run locally:  uv run --all-extras streamlit run demo/app.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:  # the hosted app runs from a checkout, not an install
    sys.path.insert(0, str(ROOT / "src"))

from evalkit.config import load_run_config  # noqa: E402
from evalkit.cost import load_prices  # noqa: E402
from evalkit.demo import page as P  # noqa: E402
from evalkit.demo.engine import MAX_QUESTION_CHARS, Abstract, AnswerCard, compare  # noqa: E402
from evalkit.demo.guard import STORE_DOWN, Guard  # noqa: E402
from evalkit.env import load_dotenv  # noqa: E402

CSS = """
<style>
.card {border:1px solid rgba(128,128,128,.35); border-radius:8px; padding:.75rem; height:100%}
.card h4 {margin:0 0 .4rem 0; font-size:1rem}
.card .claims {padding-left:1rem; font-size:.9rem}
.card .err {color:#b42318}
.card .note {font-size:.8rem; opacity:.75}
.card .notice {font-size:.75rem; opacity:.7; border-top:1px solid rgba(128,128,128,.3);
  padding-top:.4rem}
.card .notice.loud {font-size:.9rem; opacity:1; font-weight:600; color:#b54708}
</style>
"""


def secret_env() -> dict[str, str]:
    """Deployment secrets: the host's secret store (st.secrets) over the environment and a
    local, git-ignored .env. Values never leave the server."""
    load_dotenv(ROOT / ".env")
    env = dict(os.environ)
    with contextlib.suppress(Exception):  # no secrets file: a local run
        env.update({k: str(v) for k, v in st.secrets.items() if isinstance(v, str | int)})
    return env


@st.cache_resource
def shared_guard() -> Guard | None:
    """One guard per server process: the rate-limit memory is shared by every session."""
    env = secret_env()
    if P.live_problem(env):
        return None
    return P.make_guard(env)  # a failure raises, and Streamlit does not cache it: retried


def live_guard() -> Guard | None:
    try:
        return shared_guard()
    except Exception as exc:  # never show the store's message: it can quote the URL
        logging.getLogger("evalkit.demo").warning("guard unavailable (%s)", type(exc).__name__)
        return None


@st.cache_resource  # read-only objects shared as-is (cache_data would pickle them)
def assets():
    config = load_run_config(ROOT / "configs" / "pubmedqa.yaml")
    samples = P.load_samples(ROOT / "demo" / "samples.json")
    replay = P.load_replay(ROOT / "demo" / "replay.json")
    return config, samples, replay


def show_cards(cards: list[AnswerCard | None], slots) -> None:
    for slot, card in zip(slots, cards, strict=True):
        if card is not None:
            slot.markdown(P.card_html(card), unsafe_allow_html=True)


def replay_view(config, samples, replay) -> None:
    ready = [s for s in samples if P.is_cached(replay, s)]
    if not ready:
        st.warning("No prepared answers yet.")
        return
    pick = st.selectbox("Sample question", ready, format_func=lambda s: s.question)
    if pick is None:
        return
    with st.expander("Abstract"):
        for i, sentence in enumerate(pick.sentences, 1):
            st.markdown(f"**[{i}]** {sentence}")
        if pick.source:
            st.caption(pick.source)
    cards = asyncio.run(
        compare(
            pick.question,
            Abstract(pick.sentences),
            config=config,
            adapters={},
            prices={},
            mode="replay",
            replay=replay,
        )
    )
    show_cards(list(cards), st.columns(len(cards)))


def live_view(config) -> None:
    env = secret_env()
    if problem := P.live_problem(env):
        st.warning(problem)
        return
    guard = live_guard()
    if guard is None:
        st.warning(STORE_DOWN)
        return
    session = st.session_state.setdefault("session", secrets.token_urlsafe(16))
    left = guard.remaining_today()
    budget = "unavailable" if left is None else f"${left:.2f} of ${guard.daily_cap:.2f}"
    st.caption(
        f"Today's live budget left: {budget} · "
        f"each request holds at most ${guard.per_request_cap:.2f} · "
        f"{guard.rate_limit} requests per hour per visitor"
    )
    pay = st.radio("Run on", ["The owner's keys", "My own API keys"], horizontal=True, index=0)
    own = pay.startswith("My own")
    with st.form("live"):
        passcode = st.text_input("Passcode", type="password", help="Ask the owner for one.")
        if own:
            st.caption(
                "Your keys are used for your requests only: never saved or logged, held in "
                "this server's memory only while your tab is open. An Anthropic key is needed "
                "(the claim checker and judge are Claude models); without an OpenAI key the "
                "two GPT models are skipped."
            )
            key_a = st.text_input("Anthropic API key", type="password")
            key_o = st.text_input("OpenAI API key (optional)", type="password")
        else:
            key_a = key_o = ""
        question = st.text_area("Question", max_chars=MAX_QUESTION_CHARS)
        pmid = st.text_input("PubMed ID (or paste the abstract below)")
        abstract = st.text_area("Abstract", height=180)
        go = st.form_submit_button("Ask all four models")
    if not go:
        return
    prices = load_prices()
    # Fresh clients per request: each request runs on its own event loop.
    own_keys = {"anthropic": key_a.strip(), "openai": key_o.strip()} if own else None
    if own_keys is not None:
        adapters = P.own_key_adapters(own_keys, prices)
    else:  # the owner's keys, passed to the clients (secrets need not be env variables)
        adapters = P.own_key_adapters(
            {"anthropic": env["ANTHROPIC_API_KEY"], "openai": env["OPENAI_API_KEY"]}, prices
        )
    # one placeholder per card: each card replaces its 'answering…' line, never stacks
    slots = [col.empty() for col in st.columns(len(config.models))]
    for slot, m in zip(slots, config.models, strict=True):
        slot.info(f"{m}: answering…")
    filled: list[AnswerCard | None] = [None] * len(config.models)

    def on_card(i: int, card: AnswerCard) -> None:
        filled[i] = card
        slots[i].markdown(P.card_html(card), unsafe_allow_html=True)

    try:
        result = asyncio.run(
            P.run_live(
                question,
                abstract,
                pmid,
                passcode=passcode,
                session=session,
                config=config,
                adapters=adapters,
                prices=prices,
                guard=guard,
                own_keys=own_keys,
                on_card=on_card,
            )
        )
    except Exception as exc:  # never a traceback: it could carry request text
        logging.getLogger("evalkit.demo").warning("live request failed (%s)", type(exc).__name__)
        for slot in slots:
            slot.empty()
        st.error("Something went wrong with this request. Replay mode always works.")
        return
    if result.refused:
        for slot in slots:
            slot.empty()
        st.error(result.refused)
        return
    show_cards(list(result.cards), slots)
    spent = sum(c.cost_usd for c in result.cards)
    bound = f" (held at most ${result.bound_usd:.2f})" if result.bound_usd else ""
    st.caption(f"This request cost ${spent:.4f}{bound}.")


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # only the aggregate line at INFO
    st.set_page_config(page_title="EvalKit demo", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.title("Do the models stick to the abstract?")
    st.info(P.BANNER)
    st.markdown(
        "Four models answer from a study abstract and cite its sentences; a judge checks each "
        f"claim against the cited sentences. [Full benchmark report]({P.REPORT_URL})"
    )
    config, samples, replay = assets()
    mode = st.radio("Mode", ["Replay (prepared answers)", "Live"], horizontal=True, index=0)
    if mode.startswith("Replay"):
        replay_view(config, samples, replay)
    else:
        live_view(config)


main()
