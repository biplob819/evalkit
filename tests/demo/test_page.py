"""D3.2: the demo page, the replay cache and aggregate-only logging. Scripted adapters only: no
network, no spend."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from html import escape
from pathlib import Path
from typing import Any

import pytest

from evalkit import cli
from evalkit.adapters import CompletionRequest
from evalkit.config import load_run_config
from evalkit.cost import load_prices
from evalkit.demo import engine as C
from evalkit.demo import page as P
from evalkit.demo.guard import Guard, PostgresSpend, SqliteSpend
from evalkit.demo.pubmed import PubMedError
from tests.demo.test_compare import CONFIG, QUESTION, SENTENCES, adapters, reply
from tests.pipeline.fakes import ScriptedAdapter

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "demo" / "app.py"
PRICES = load_prices()
PASS = "open-sesame"


def guard(tmp_path: Path, **kw: Any) -> Guard:
    kw.setdefault("per_request_cap", 2.0)
    return Guard(PASS, SqliteSpend(tmp_path / "spend.db"), **kw)


def live(tmp_path: Path, question: str = QUESTION, abstract: str = "", pmid: str = "", **kw: Any):
    kw.setdefault("adapters", adapters())
    kw.setdefault("guard", guard(tmp_path))
    kw.setdefault("passcode", PASS)
    return asyncio.run(
        P.run_live(
            question,
            abstract or " ".join(SENTENCES),
            pmid,
            session="s1",
            config=CONFIG,
            prices=PRICES,
            **kw,
        )
    )


# ---- DEM-01: demo-cache -----------------------------------------------------------------------


def write_samples(path: Path, n: int = 2) -> None:
    samples = [
        {"id": f"s{i}", "question": f"{QUESTION} (variant {i})", "sentences": list(SENTENCES)}
        for i in range(n)
    ]
    path.write_text(json.dumps(samples), encoding="utf-8")


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    calls: list[CompletionRequest] = []
    state = {"fail": set()}

    def fake_get_adapter(provider: str, **_: Any):
        a = ScriptedAdapter(lambda r: reply(r, fail=state["fail"]))
        a.requests = calls  # one shared log of every call
        return a

    monkeypatch.setattr("evalkit.adapters.get_adapter", fake_get_adapter)
    (tmp_path / "configs").mkdir()
    cfg = tmp_path / "configs" / "demo.yaml"
    cfg.write_text(
        (ROOT / "configs" / "pubmedqa.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    write_samples(tmp_path / "samples.json")
    return tmp_path, calls, state


def demo_cache(*extra: str) -> int:
    return cli.main(
        [
            "demo-cache",
            "--samples",
            "samples.json",
            "--out",
            "replay.json",
            "--config",
            "configs/demo.yaml",
            "--json",
            *extra,
        ]
    )


def test_demo_cache_dry_run_prints_the_estimate_and_calls_nothing(cache_env, capsys):
    _, calls, _ = cache_env
    assert demo_cache("--dry-run") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["to_answer"] == 2 and out["estimate_usd"] > 0
    assert out["worst_case_usd"] >= out["estimate_usd"]
    assert calls == [] and not Path("replay.json").exists()


def test_demo_cache_writes_every_sample_and_a_rerun_pays_nothing(cache_env, capsys):
    _, calls, _ = cache_env
    assert demo_cache() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["answered"] == 2 and first["spent_usd"] > 0 and calls
    replay = P.load_replay(Path("replay.json"))
    samples = P.load_samples(Path("samples.json"))
    assert all(P.is_cached(replay, s) for s in samples)
    n = len(calls)
    assert demo_cache() == 0
    assert len(calls) == n  # cached: nothing answered again


def test_demo_cache_stops_before_a_sample_that_could_pass_the_cap(cache_env, capsys):
    _, calls, _ = cache_env
    assert demo_cache("--max-cost", "0.01") == 3  # one sample's bound is ~$1
    out = json.loads(capsys.readouterr().out)
    assert out["answered"] == 0 and out["left"] == 2 and calls == []


def test_demo_cache_answers_a_sample_with_a_failed_card_again(cache_env, capsys):
    _, _, state = cache_env
    state["fail"] = {"gpt-6-luna"}
    assert demo_cache() == 0
    replay = P.load_replay(Path("replay.json"))
    samples = P.load_samples(Path("samples.json"))
    assert not any(P.is_cached(replay, s) for s in samples)
    state["fail"] = set()
    capsys.readouterr()
    assert demo_cache() == 0
    assert json.loads(capsys.readouterr().out)["answered"] == 2
    replay = P.load_replay(Path("replay.json"))
    assert all(P.is_cached(replay, s) for s in samples)


def test_every_shipped_sample_renders_in_replay_mode():
    samples = P.load_samples(ROOT / "demo" / "samples.json")
    replay = P.load_replay(ROOT / "demo" / "replay.json")
    config = load_run_config(ROOT / "configs" / "pubmedqa.yaml")
    assert len(samples) >= 5
    for s in samples:
        assert P.is_cached(replay, s), s.id
        cards = asyncio.run(
            C.compare(
                s.question,
                C.Abstract(s.sentences),
                config=config,
                adapters={},
                prices={},
                mode="replay",
                replay=replay,
            )
        )
        assert [c.model for c in cards] == list(config.models)
        for c in cards:
            assert c.error is None and c.answer and c.n_claims > 0
            assert c.groundedness is not None
            judged = [m for m in c.claims if m.verdict != "unchecked"]
            assert len(judged) == min(len(c.claims), C.MAX_CLAIMS)
            assert escape(C.NOTICE) in P.card_html(c)


def test_unchecked_claims_are_an_error_not_a_clean_card():
    def no_extraction(req):
        from evalkit.pipeline.extract import extraction_schema
        from evalkit.runtime.canonical import to_jsonable

        if to_jsonable(req.json_schema) == extraction_schema():
            return ConnectionError("extractor down")
        return reply(req)

    a = ScriptedAdapter(no_extraction)
    cards = asyncio.run(
        C.compare(
            QUESTION,
            C.Abstract(SENTENCES),
            config=CONFIG,
            adapters={"anthropic": a, "openai": a},
            prices=PRICES,
        )
    )
    assert all(c.answer and c.error == "claims not checked (transport)" for c in cards)
    assert all(c.groundedness is None for c in cards)


# ---- DEM-03: live mode ------------------------------------------------------------------------


def test_live_request_returns_four_cards_with_its_bound_and_estimate(tmp_path):
    seen: list[int] = []
    res = live(tmp_path, on_card=lambda i, c: seen.append(i))
    assert res.refused is None and len(res.cards) == 4
    assert sorted(seen) == [0, 1, 2, 3]
    assert 0 < res.estimate_usd < res.bound_usd <= P.PER_REQUEST_CAP_USD


def test_wrong_passcode_is_a_readable_refusal_with_no_call(tmp_path):
    a = adapters()
    res = live(tmp_path, passcode="nope", adapters=a)
    assert res.refused == "Wrong passcode." and res.cards == ()
    assert a["anthropic"].requests == []


def test_wrong_passcode_wins_over_bad_input(tmp_path):
    res = live(tmp_path, passcode="nope", abstract="   ")
    assert res.refused == "Wrong passcode."


def test_daily_budget_refusal_is_readable(tmp_path):
    res = live(tmp_path, guard=guard(tmp_path, daily_cap=0.01))
    assert res.refused is not None and "budget" in res.refused


def test_bad_pmid_is_a_readable_refusal(tmp_path):
    def fetch(pmid: str) -> list[str]:
        raise PubMedError("no abstract for that PMID")

    res = live(tmp_path, pmid="123", fetch=fetch)
    assert res.refused == "no abstract for that PMID"


def test_pmid_source_is_fetched(tmp_path):
    res = live(tmp_path, pmid="31415", fetch=lambda pmid: list(SENTENCES))
    assert res.refused is None and len(res.cards) == 4 and res.cards[0].cited


def test_remaining_budget_drops_after_a_request(tmp_path):
    g = guard(tmp_path)
    before = g.remaining_today()
    live(tmp_path, guard=g)
    after = g.remaining_today()
    assert before == g.daily_cap and after is not None and 0 < g.daily_cap - after < 0.1


def test_live_mode_needs_all_four_secrets():
    env = {k: "x" for k in P.SECRETS}
    assert P.live_problem(env) is None
    for k in P.SECRETS:
        problem = P.live_problem({**env, k: ""})
        assert problem is not None and "x" not in problem


def own(tmp_path: Path, keys: dict[str, str], **kw: Any):
    return live(tmp_path, own_keys=keys, passcode="", **kw)


def test_own_keys_need_no_passcode_and_leave_the_owner_budget_alone(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    g = guard(tmp_path)
    res = own(tmp_path, {"anthropic": "k1", "openai": "k2"}, guard=g)
    assert res.refused is None and len(res.cards) == 4
    assert all(c.error is None and c.answer for c in res.cards)
    assert g.remaining_today() == g.daily_cap
    assert any('"mode": "own_keys"' in r.getMessage() for r in caplog.records)


def test_own_keys_without_openai_give_no_key_cards_in_place(tmp_path):
    a = ScriptedAdapter(reply)
    seen: dict[int, str] = {}
    res = own(
        tmp_path,
        {"anthropic": "k1"},
        adapters={"anthropic": a},
        on_card=lambda i, c: seen.__setitem__(i, c.model),
    )
    assert [c.model for c in res.cards] == list(CONFIG.models)
    by = {c.model: c for c in res.cards}
    assert by["gpt-6.1-sol"].error == "no openai key given" and by["gpt-6.1-sol"].notice
    assert by["claude-sonnet-5-5"].answer and by["claude-haiku-5-5"].answer
    assert seen == dict(enumerate(CONFIG.models))
    assert {r.model_id for r in a.requests} <= {
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-opus-5-5",
    }


def test_own_keys_need_an_anthropic_key(tmp_path):
    a = adapters()
    res = own(tmp_path, {"openai": "k2"}, adapters=a)
    assert res.refused is not None and "Anthropic key" in res.refused
    assert a["anthropic"].requests == []


def test_own_keys_still_meet_the_per_request_cap_and_rate_limit(tmp_path):
    keys = {"anthropic": "k1", "openai": "k2"}
    a = adapters()
    res = own(tmp_path, keys, adapters=a, guard=guard(tmp_path, per_request_cap=0.0001))
    assert res.refused is not None and "per-request limit" in res.refused
    assert a["anthropic"].requests == []
    g = guard(tmp_path, rate_limit=1)
    assert own(tmp_path, keys, guard=g).refused is None
    res = own(tmp_path, keys, guard=g)
    assert res.refused is not None and "Rate limit" in res.refused


def test_own_keys_with_a_pmid_meet_the_rate_limit_before_any_fetch(tmp_path):
    fetched: list[str] = []

    def fetch(pmid: str) -> list[str]:
        fetched.append(pmid)
        return list(SENTENCES)

    keys = {"anthropic": "k1", "openai": "k2"}
    g = guard(tmp_path, rate_limit=1)
    res = own(tmp_path, keys, guard=g, pmid="31415", fetch=fetch)
    assert res.refused is None and res.cards[0].cited and res.bound_usd > 0
    assert fetched == ["31415"]
    for q in (QUESTION, "   ", "x" * 600):  # rate-limited, empty, too long: no fetch at all
        assert own(tmp_path, keys, guard=g, question=q, pmid="2", fetch=fetch).refused
    assert own(tmp_path, keys, guard=guard(tmp_path), question="", pmid="3", fetch=fetch).refused
    assert fetched == ["31415"]


def test_owner_pmid_request_shows_its_bound(tmp_path):
    res = live(tmp_path, pmid="31415", fetch=lambda pmid: list(SENTENCES))
    assert res.refused is None and 0 < res.estimate_usd < res.bound_usd


def test_missing_key_cards_are_shown_before_the_keyed_models_answer(tmp_path):
    order: list[int] = []
    own(
        tmp_path,
        {"anthropic": "k1"},
        adapters={"anthropic": ScriptedAdapter(reply)},
        on_card=lambda i, c: order.append(i),
    )
    assert order[:2] == [2, 3] and sorted(order) == [0, 1, 2, 3]


def test_a_visitor_key_reaches_no_log_file_env_or_card(tmp_path, monkeypatch, caplog):
    """DEM-05 for API keys: real SDK clients on a mock transport whose 401 quotes the key."""
    import os

    import anthropic
    import httpx2
    import openai

    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.DEBUG)
    marker = f"sk-ant-{uuid.uuid4().hex}"
    seen: list[str] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(str(req.url))
        k = req.headers.get("x-api-key") or req.headers.get("authorization", "")
        body = {"type": "error", "error": {"type": "authentication_error", "message": f"bad {k}"}}
        return httpx2.Response(401, json=body)

    real_a, real_o = anthropic.AsyncAnthropic, openai.AsyncOpenAI

    def mock(real):
        return lambda **kw: real(
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), **kw
        )

    monkeypatch.setattr(anthropic, "AsyncAnthropic", mock(real_a))
    monkeypatch.setattr(openai, "AsyncOpenAI", mock(real_o))
    keys = {"anthropic": marker, "openai": marker + "o"}
    g = guard(tmp_path)
    res = own(tmp_path, keys, adapters=P.own_key_adapters(keys, PRICES), guard=g)
    assert seen and res.refused is None  # the clients really sent the key
    assert all(c.error == "the provider rejected the API key" for c in res.cards)
    text = "\n".join(
        r.getMessage() + (repr(r.exc_info) if r.exc_info else "") for r in caplog.records
    )
    assert marker not in text
    assert all(marker not in P.card_html(c) for c in res.cards)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert marker.encode() not in f.read_bytes(), f
    assert marker not in "".join(os.environ.values())
    assert g.remaining_today() == g.daily_cap


def test_own_key_adapters_hold_the_keys_only_in_their_clients(monkeypatch):
    import os

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    marker = f"sk-test-{uuid.uuid4().hex}"
    ad = P.own_key_adapters({"anthropic": marker, "openai": marker + "o"}, PRICES)
    assert set(ad) == {"anthropic", "openai"}
    assert ad["anthropic"].client.api_key == marker  # type: ignore[attr-defined]
    assert ad["openai"].client.api_key == marker + "o"  # type: ignore[attr-defined]
    assert marker not in "".join(os.environ.values())
    assert P.own_key_adapters({"anthropic": marker}, PRICES).keys() == {"anthropic"}


def test_a_rejected_key_is_named_without_echoing_the_provider_message(tmp_path):
    secret = f"sk-{uuid.uuid4().hex}"

    class AuthenticationError(Exception):
        pass

    def bad_key(req):
        return AuthenticationError(f"Incorrect API key provided: {secret}")

    a = ScriptedAdapter(bad_key)
    res = own(tmp_path, {"anthropic": "k1", "openai": "k2"}, adapters={"anthropic": a, "openai": a})
    assert res.refused is None
    for c in res.cards:
        assert c.error == "the provider rejected the API key"
        assert secret not in P.card_html(c)


# ---- DEM-04: cards ----------------------------------------------------------------------------


def test_card_shows_label_answer_citations_claims_grounding_cost_latency(tmp_path):
    card = live(tmp_path).cards[0]
    html = P.card_html(card)
    for part in ("Label:", "yes", "Cited sentences", SENTENCES[1], "✓ supported"):
        assert part in html
    assert "Grounded:</b> 100%" in html and "cost</b> $" in html and "latency</b>" in html
    assert escape(C.NOTICE) in html and "notice loud" not in html


def test_advice_question_makes_the_notice_prominent(tmp_path):
    card = live(tmp_path, question="Should I stop taking my blood pressure pills?").cards[0]
    assert card.advice and "notice loud" in P.card_html(card)


def test_card_html_escapes_every_text_field():
    bad = "<script>x()</script>"
    full = C.AnswerCard(
        model=bad,
        answer=bad,
        label=bad,
        cited=(C.CitedSentence(n=1, text=bad),),
        claims=(C.ClaimMark(text=bad, verdict="supported"),),
        error=bad,
        note=bad,
        notice=bad,
    )
    failed = C.AnswerCard(model="m", error=bad, notice="n")
    for card in (full, failed):
        html = P.card_html(card)
        assert "<script>" not in html and "&lt;script&gt;" in html
    assert P.card_html(full).count("&lt;script&gt;") == 8


def test_unjudged_claims_are_an_error_and_not_cached(tmp_path):
    def no_judge(req):
        from evalkit.pipeline.judge import verdict_schema
        from evalkit.runtime.canonical import to_jsonable

        if to_jsonable(req.json_schema) == verdict_schema():
            return ConnectionError("judge down")
        return reply(req)

    a = ScriptedAdapter(no_judge)
    res = live(tmp_path, adapters={"anthropic": a, "openai": a})
    for c in res.cards:
        assert c.error == "claims not checked (judge)" and c.groundedness is None
        assert c.claims and all(m.verdict == "unchecked" for m in c.claims)
        assert "Grounded:</b> –" in P.card_html(c)
    sample = P.Sample(id="s", question=QUESTION, sentences=SENTENCES)
    assert not P.is_cached({P.sample_key(sample): list(res.cards)}, sample)


class _DownStore:
    def reserve(self, day, amount, cap):
        raise OSError("connection to db.example.invalid user=owner password=hunter2 failed")

    settle = reserve

    def spent(self, day):
        raise OSError("db.example.invalid")


def test_a_spend_store_outage_is_a_readable_refusal(tmp_path, caplog):
    g = Guard(PASS, _DownStore(), per_request_cap=2.0, rate_limit=1)  # type: ignore[arg-type]
    a = adapters()
    res = live(tmp_path, guard=g, adapters=a)
    assert res.refused is not None and "unavailable" in res.refused
    assert "hunter2" not in res.refused and a["anthropic"].requests == []
    assert g.remaining_today() is None
    assert not any("hunter2" in r.getMessage() for r in caplog.records)
    # the rate-limit slot was given back: own keys (no store) still work for this session
    assert own(tmp_path, {"anthropic": "k1", "openai": "k2"}, guard=g).refused is None


def test_a_failed_settle_keeps_the_reservation_and_does_not_crash(tmp_path):
    class SettleDown(SqliteSpend):
        def settle(self, day, reserved, actual):
            raise OSError("down")

    g = Guard(PASS, SettleDown(tmp_path / "s.db"), per_request_cap=2.0)
    res = live(tmp_path, guard=g)
    assert res.refused is None and len(res.cards) == 4
    assert g.daily_cap - (g.remaining_today() or 0) == pytest.approx(res.bound_usd, rel=1e-6)


def test_postgres_spend_does_not_connect_until_used():
    connects: list[str] = []

    def connect(url):
        connects.append(url)
        raise OSError("unreachable")

    store = PostgresSpend("postgresql://example.invalid/db", connect)
    assert connects == []
    with pytest.raises(OSError):
        store.spent("2031-01-01")
    assert connects


def test_error_card_says_why():
    html = P.card_html(C.AnswerCard(model="m", error="timed out", notice="n"))
    assert "No answer: timed out" in html


# ---- DEM-05: aggregate-only logging -----------------------------------------------------------


def test_log_line_has_only_aggregate_fields():
    line = P.log_request(mode="live", models=["a"], latency_ms=5, cost_usd=0.1, outcome="ok")
    assert set(json.loads(line)) == {"at", "mode", "models", "latency_ms", "cost_usd", "outcome"}


def test_a_marker_in_the_request_reaches_no_log_line_or_file(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    marker = f"zq{uuid.uuid4().hex}"
    caplog.set_level(logging.DEBUG)  # every logger, every level
    res = live(
        tmp_path,
        question=f"Does {marker} speed wound healing?",
        abstract=" ".join(SENTENCES) + f" The trial id was {marker}.",
    )
    assert res.refused is None
    lines = [r.getMessage() for r in caplog.records]
    assert any('"mode": "live"' in m for m in lines)  # the aggregate line was written
    assert not any(marker in m for m in lines)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert marker.encode() not in f.read_bytes(), f


# ---- DEM-02 / DEM-04 through the page (Streamlit AppTest) -------------------------------------


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    import streamlit as st

    st.cache_resource.clear()
    st.cache_data.clear()
    monkeypatch.chdir(tmp_path)
    for k in P.SECRETS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("evalkit.env.load_dotenv", lambda path: None)  # not the owner's .env
    calls: list[CompletionRequest] = []

    def fake_get_adapter(provider: str, **_: Any):
        a = ScriptedAdapter(reply)
        a.requests = calls
        return a

    monkeypatch.setattr("evalkit.adapters.get_adapter", fake_get_adapter)
    yield tmp_path, calls, monkeypatch
    st.cache_resource.clear()


def app():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def page_html(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_page_opens_in_replay_mode_with_cards_and_no_calls(app_env):
    _, calls, mp = app_env

    def no_clients(*a, **k):
        raise AssertionError("replay built a provider client")

    mp.setattr(P, "own_key_adapters", no_clients)
    mp.setattr(P, "run_live", no_clients)
    at = app()
    assert str(at.radio[0].value).startswith("Replay")
    assert at.selectbox[0].options and len(at.selectbox[0].options) >= 5
    html = page_html(at)
    assert html.count("class='card'") == 4 and P.REPORT_URL in html
    assert any(P.BANNER in i.value for i in at.info)
    at.selectbox[0].select_index(1).run()
    assert not at.exception and page_html(at).count("class='card'") == 4
    assert calls == []


def test_live_mode_is_off_without_the_secrets(app_env):
    _, calls, _ = app_env
    at = app()
    at.radio[0].set_value("Live").run()
    assert any("not configured" in w.value for w in at.warning)
    assert not at.text_input and calls == []


OWNER_KEYS: list[list[dict[str, str]]] = []


def configure_live(app_env, tmp_path: Path) -> list[CompletionRequest]:
    _, calls, mp = app_env
    for k in P.SECRETS:
        mp.setenv(k, PASS if k == "EVALKIT_DEMO_PASSCODE" else f"test-{k.lower()}")
    mp.setattr(P, "make_guard", lambda env, connect=None: guard(tmp_path))
    owner_keys: list[dict[str, str]] = []

    def scripted(keys, prices):  # never a real client in tests
        owner_keys.append(dict(keys))
        a = ScriptedAdapter(reply)
        a.requests = calls
        return {"anthropic": a, "openai": a}

    mp.setattr(P, "own_key_adapters", scripted)
    OWNER_KEYS[:] = []
    OWNER_KEYS.append(owner_keys)
    return calls


def ask(at, passcode: str, question: str, abstract: str):
    at.radio[0].set_value("Live").run()
    assert any("budget left: $3.00" in c.value for c in at.caption)
    at.text_input[0].input(passcode)
    at.text_area[0].input(question)
    at.text_area[1].input(abstract)
    at.button[0].click().run()
    assert not at.exception, at.exception
    return at


def test_live_mode_wrong_passcode_shows_the_refusal_and_calls_nothing(app_env, tmp_path):
    calls = configure_live(app_env, tmp_path)
    at = ask(app(), "nope", QUESTION, " ".join(SENTENCES))
    assert [e.value for e in at.error] == ["Wrong passcode."]
    assert calls == []


def test_live_mode_fills_four_cards_and_logs_no_question(app_env, tmp_path, caplog):
    calls = configure_live(app_env, tmp_path)
    marker = f"zq{uuid.uuid4().hex}"
    caplog.set_level(logging.DEBUG)
    at = ask(app(), PASS, f"Does {marker} speed wound healing?", " ".join(SENTENCES))
    assert not at.error
    assert page_html(at).count("class='card'") == 4 and calls
    assert OWNER_KEYS[0] == [  # passed to the clients explicitly, not read from env by the SDK
        {"anthropic": "test-anthropic_api_key", "openai": "test-openai_api_key"}
    ]
    assert any("This request cost $" in c.value for c in at.caption)
    assert any('"mode": "live"' in r.getMessage() for r in caplog.records)
    assert not any(marker in r.getMessage() for r in caplog.records)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert marker.encode() not in f.read_bytes(), f


def test_live_mode_on_own_keys_skips_the_passcode_and_the_owner_budget(app_env, tmp_path):
    calls = configure_live(app_env, tmp_path)
    _, _, mp = app_env
    built: list[dict[str, str]] = []

    def fake_own(keys, prices):
        built.append(dict(keys))
        a = ScriptedAdapter(reply)
        a.requests = calls
        return {"anthropic": a}  # no OpenAI key given

    mp.setattr(P, "own_key_adapters", fake_own)
    at = app()
    at.radio[0].set_value("Live").run()
    at.radio[1].set_value("My own API keys").run()
    assert any("never saved or logged" in c.value for c in at.caption)
    at.text_input[0].input("sk-ant-visitor")
    at.text_area[0].input(QUESTION)
    at.text_area[1].input(" ".join(SENTENCES))
    at.button[0].click().run()
    assert not at.exception and not at.error
    assert built == [{"anthropic": "sk-ant-visitor", "openai": ""}]
    html = page_html(at)
    assert html.count("class='card'") == 4 and "no openai key given" in html
    assert "sk-ant-visitor" not in html and calls


def test_demo_requirements_cover_the_core_and_the_demo_extras():
    import re
    import tomllib

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wanted = [*meta["dependencies"]]
    for extra in ("anthropic", "openai", "demo"):
        wanted += meta["optional-dependencies"][extra]
    lines = (ROOT / "demo" / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pinned = {
        re.split(r"[\[=<>]", ln)[0].strip().lower()
        for ln in lines
        if ln.strip() and not ln.startswith("#")
    }
    assert {re.split(r"[\[=<>]", w)[0].lower() for w in wanted} <= pinned
    assert all("==" in ln for ln in lines if ln.strip() and not ln.startswith("#"))


def test_live_cards_are_drawn_as_each_model_finishes(app_env, tmp_path):
    configure_live(app_env, tmp_path)
    _, _, mp = app_env
    drawn: list[str] = []
    real_card_html, real_run_live = P.card_html, P.run_live

    def counting(card):
        drawn.append(card.model)
        return real_card_html(card)

    during: list[int] = []

    async def watched(*a, **kw):
        assert callable(kw.get("on_card"))
        start = len(drawn)  # the replay view drew its own cards earlier
        res = await real_run_live(*a, **kw)
        during.append(len(drawn) - start)  # cards drawn before run_live returned
        return res

    mp.setattr(P, "card_html", counting)
    mp.setattr(P, "run_live", watched)
    ask(app(), PASS, QUESTION, " ".join(SENTENCES))
    assert during == [4]


def test_a_broken_spend_store_leaves_the_page_readable(app_env, tmp_path):
    configure_live(app_env, tmp_path)
    _, _, mp = app_env
    mp.setattr(
        P,
        "make_guard",
        lambda env, connect=None: Guard(PASS, _DownStore(), per_request_cap=2.0),  # type: ignore[arg-type]
    )
    at = app()
    at.radio[0].set_value("Live").run()
    assert not at.exception
    assert any("budget left: unavailable" in c.value for c in at.caption)
    at.text_input[0].input(PASS)
    at.text_area[0].input(QUESTION)
    at.text_area[1].input(" ".join(SENTENCES))
    at.button[0].click().run()
    assert not at.exception and any("unavailable" in e.value for e in at.error)
    assert "hunter2" not in page_html(at)


def test_a_failed_guard_build_is_retried_on_the_next_run(app_env, tmp_path):
    configure_live(app_env, tmp_path)
    _, _, mp = app_env
    attempts: list[int] = []

    def flaky(env, connect=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise ImportError("psycopg missing")
        return guard(tmp_path)

    mp.setattr(P, "make_guard", flaky)
    at = app()
    at.radio[0].set_value("Live").run()
    assert any("unavailable" in w.value for w in at.warning) and not at.exception
    at.run()
    assert not at.warning and any("budget left: $3.00" in c.value for c in at.caption)
    assert len(attempts) == 2
