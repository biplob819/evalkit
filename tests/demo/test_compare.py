"""D3.1: compare(), the PubMed fetch and the live guard. Scripted adapters: no network, no spend."""

import asyncio
import json
import os
import threading
from typing import Any

import pytest

from evalkit.adapters import CompletionRequest
from evalkit.cost import load_prices
from evalkit.demo import engine as C
from evalkit.demo.guard import Guard, GuardRefused, PostgresSpend, SqliteSpend
from evalkit.demo.pubmed import PubMedError, fetch_abstract, parse_abstract
from evalkit.pipeline.extract import extraction_schema
from evalkit.pipeline.judge import verdict_schema
from evalkit.runtime.canonical import to_jsonable
from evalkit.schemas import RunConfig
from tests.pipeline.fakes import ScriptedAdapter

PRICES = load_prices()
MODELS = ("claude-sonnet-5-5", "claude-haiku-5-5", "gpt-6.1-sol", "gpt-6-luna")
CONFIG = RunConfig(
    models=MODELS, judges=("claude-opus-5-5", "gpt-6.1-sol"), extractor="claude-haiku-5-5"
)
SENTENCES = (
    "Thirty patients received the new dressing.",
    "Wounds healed faster with the new dressing than with standard care.",
    "No serious adverse events were reported.",
)
ABSTRACT = C.Abstract(SENTENCES)
QUESTION = "Does the new dressing speed wound healing?"


def reply(
    req: CompletionRequest,
    *,
    fail: frozenset[str] | set[str] = frozenset(),
    slow: frozenset[str] | set[str] = frozenset(),
):
    schema = to_jsonable(req.json_schema)
    if req.model_id in fail and schema not in (extraction_schema(), verdict_schema()):
        return ConnectionError("provider down")
    if schema == extraction_schema():
        return json.dumps(
            {
                "claims": [
                    {
                        "text": "Wounds healed faster with the new dressing.",
                        "cited": [2],
                        "addresses_question": True,
                    }
                ]
            }
        )
    if schema == verdict_schema():
        return json.dumps(
            {
                "verdict": "supported",
                "evidence": [2],
                "quote": None,
                "rationale": "sentence 2 says so",
            }
        )
    return json.dumps(
        {"label": "yes", "answer": "Wounds healed faster with the new dressing.", "citations": [2]}
    )


class Slow(ScriptedAdapter):
    def __init__(self, reply_fn, slow: set[str]):
        super().__init__(reply_fn)
        self.slow = slow

    async def complete(self, req):
        if req.model_id in self.slow and to_jsonable(req.json_schema) not in (
            extraction_schema(),
            verdict_schema(),
        ):
            await asyncio.sleep(1.0)
        return await super().complete(req)


def adapters(**kw: Any):
    a = ScriptedAdapter(lambda r: reply(r, **kw))
    return {"anthropic": a, "openai": a}


def run(**kw: Any):
    kw.setdefault("adapters", adapters())
    return asyncio.run(
        C.compare(QUESTION, kw.pop("source", ABSTRACT), config=CONFIG, prices=PRICES, **kw)
    )


# ---- CMP-01 -----------------------------------------------------------------------------------


def test_one_card_per_model_in_order():
    cards = run()
    assert [c.model for c in cards] == list(MODELS)
    c = cards[0]
    assert c.label == "yes" and c.cited[0].n == 2 and c.cited[0].text == SENTENCES[1]
    assert c.groundedness == 1.0 and c.fully_grounded and c.n_claims == 1
    assert c.judge == "claude-opus-5-5" and c.cost_usd > 0 and c.error is None


def test_a_failing_model_gets_an_error_card_not_a_crash():
    cards = run(adapters=adapters(fail={"gpt-6-luna"}))
    by = {c.model: c for c in cards}
    assert by["gpt-6-luna"].error == "transport" and by["gpt-6-luna"].answer is None
    assert all(by[m].error is None for m in MODELS[:3])


def test_replay_makes_no_calls():
    live = run()
    a = adapters()
    key = C.replay_key(QUESTION, SENTENCES)
    replayed = run(mode="replay", replay={key: live}, adapters=a)
    assert replayed == live and a["anthropic"].requests == []
    with pytest.raises(KeyError, match="live mode"):
        run(mode="replay", replay={}, adapters=a)


# ---- CMP-02 -----------------------------------------------------------------------------------

XML = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><Abstract>
<AbstractText Label="BACKGROUND">Dressings matter.</AbstractText>
<AbstractText Label="RESULTS">Wounds healed faster. No adverse events occurred.</AbstractText>
</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""


def test_pmid_is_fetched_and_split_into_sentences():
    seen = []

    def get(url, timeout):
        seen.append(url)
        return XML

    sents = fetch_abstract("12345678", get=get)
    assert sents == ["Dressings matter.", "Wounds healed faster.", "No adverse events occurred."]
    assert "id=12345678" in seen[0] and "tool=evalkit" in seen[0] and "email" not in seen[0]
    cards = run(source=C.PMID("12345678"), fetch=lambda pmid: fetch_abstract(pmid, get=get))
    assert cards[0].cited[0].text == "Wounds healed faster."


@pytest.mark.parametrize(
    ("pmid", "body", "needle"),
    [
        ("12a", XML, "not a PubMed ID"),
        ("123", b"<PubmedArticleSet/>", "no abstract"),
        ("123", b"<not xml", "malformed"),
    ],
)
def test_fetch_failures_are_clear(pmid, body, needle):
    with pytest.raises(PubMedError, match=needle):
        fetch_abstract(pmid, get=lambda url, t: body)


def test_unreachable_pubmed_is_a_clear_error():
    def down(url, timeout):
        raise OSError("network down")

    with pytest.raises(PubMedError, match="could not reach"):
        fetch_abstract("123", get=down)


def test_live_mode_never_writes_the_question_or_abstract_to_disk(monkeypatch):
    import evalkit.runtime.store as store_mod

    def no_disk(*a, **k):
        raise AssertionError("live mode wrote to disk")

    monkeypatch.setattr(store_mod, "_atomic_write", no_disk)
    cards = run()
    assert all(c.error is None for c in cards)
    assert parse_abstract(XML).startswith("Dressings matter.")


# ---- CMP-03 -----------------------------------------------------------------------------------


def test_a_slow_model_times_out_without_blocking_the_others():
    slow = Slow(lambda r: reply(r), {"claude-haiku-5-5"})
    cards = run(adapters={"anthropic": slow, "openai": slow}, gen_timeout=0.3)
    by = {c.model: c for c in cards}
    assert by["claude-haiku-5-5"].error == "timed out"
    assert by["claude-sonnet-5-5"].groundedness == 1.0
    assert all("one judge" in (c.note or "") for c in cards)


def test_live_mode_uses_one_judge_at_low_effort():
    a = adapters()
    run(adapters=a)
    judged = [r for r in a["anthropic"].requests if to_jsonable(r.json_schema) == verdict_schema()]
    assert judged and {r.model_id for r in judged} == {"claude-opus-5-5"}
    assert all(r.params == {"effort": "low"} for r in judged)


# ---- CMP-04 / CMP-05 --------------------------------------------------------------------------


def guard(tmp_path, **kw: Any):
    kw.setdefault("per_request_cap", 2.0)  # the strict bound is ~$1.06 for this abstract
    return Guard("open-sesame", SqliteSpend(tmp_path / "spend.db"), **kw)


def test_wrong_passcode_is_refused_before_any_call(tmp_path):
    a = adapters()
    with pytest.raises(GuardRefused, match="passcode"):
        run(adapters=a, guard=guard(tmp_path), passcode="nope", session="s1")
    assert a["anthropic"].requests == []


def test_a_session_over_its_rate_limit_is_refused(tmp_path):
    g = guard(tmp_path, rate_limit=2)
    for _ in range(2):
        run(guard=g, passcode="open-sesame", session="s1")
    with pytest.raises(GuardRefused, match="Rate limit"):
        run(guard=g, passcode="open-sesame", session="s1")
    run(guard=g, passcode="open-sesame", session="s2")  # other sessions unaffected


def test_a_request_over_the_per_request_cap_is_refused(tmp_path):
    a = adapters()
    with pytest.raises(GuardRefused, match="per-request limit"):
        run(
            adapters=a,
            guard=guard(tmp_path, per_request_cap=0.0001),
            passcode="open-sesame",
            session="s1",
        )
    assert a["anthropic"].requests == []


def test_spend_is_reserved_then_settled_to_the_actual_cost(tmp_path):
    g = guard(tmp_path)
    run(guard=g, passcode="open-sesame", session="s1")
    spent = g.spend.spent(g._today())
    assert 0 < spent < C.estimate_request_usd(CONFIG, PRICES, "claude-opus-5-5")


def test_the_daily_cap_holds_under_concurrency_and_restarts(tmp_path):
    path = tmp_path / "spend.db"
    ok = []

    def worker():
        ok.append(SqliteSpend(path).reserve("2026-11-15", 0.4, 3.0))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(ok) == 7  # 7 * 0.4 = 2.8 <= 3.0; the 8th would pass the cap
    restarted = SqliteSpend(path)  # a new process sees the same counter
    assert restarted.spent("2026-11-15") == pytest.approx(2.8)
    assert not restarted.reserve("2026-11-15", 0.4, 3.0)
    assert restarted.reserve("2026-11-16", 0.4, 3.0)  # a new UTC day starts fresh


def test_the_daily_budget_refusal_reaches_the_visitor(tmp_path):
    g = guard(tmp_path, daily_cap=0.0002, per_request_cap=5.0)
    with pytest.raises(GuardRefused, match="budget is used up"):
        run(guard=g, passcode="open-sesame", session="s1")


@pytest.mark.skipif(
    not os.environ.get("EVALKIT_TEST_POSTGRES_URL"),
    reason="set EVALKIT_TEST_POSTGRES_URL to test against a real Postgres",
)
def test_postgres_daily_cap_holds_under_concurrency():
    url = os.environ["EVALKIT_TEST_POSTGRES_URL"]
    store = PostgresSpend(url)
    day = "2031-01-01"
    with __import__("psycopg").connect(url) as con:
        con.execute("DELETE FROM demo_spend WHERE day = %s", (day,))
    ok = []
    threads = [
        threading.Thread(target=lambda: ok.append(PostgresSpend(url).reserve(day, 0.4, 3.0)))
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(ok) == 7 and store.spent(day) == pytest.approx(2.8)
    store.settle(day, 0.4, 0.1)
    assert store.spent(day) == pytest.approx(2.5)


# ---- CMP-06 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "Should I stop taking metformin before surgery?",
        "My mother has COPD; can she use this inhaler?",
        "What dose of vitamin D is right for me?",
        "I've been having chest pain, is this drug safe?",
    ],
)
def test_advice_seeking_questions_get_the_notice(q):
    assert C.is_advice_seeking(q)


@pytest.mark.parametrize(
    "q",
    [
        "Does the new dressing speed wound healing?",
        "Is aspirin associated with lower colorectal cancer risk in this cohort?",
        "Did the trial find a difference in mortality?",
    ],
)
def test_research_questions_do_not(q):
    assert not C.is_advice_seeking(q)


def test_the_notice_is_on_every_card_and_advice_is_flagged():
    """D-56: the research-only notice is on every card (a missed advice question must never
    go without it); advice-seeking questions are flagged so the page shows it prominently."""
    a = adapters()
    cards = asyncio.run(
        C.compare(
            "Should I use this dressing on my wound?",
            ABSTRACT,
            config=CONFIG,
            adapters=a,
            prices=PRICES,
        )
    )
    assert all(c.notice == C.NOTICE and c.advice for c in cards)
    plain = run()
    assert all(c.notice == C.NOTICE and not c.advice for c in plain)


# ---- verifier round 1 scenarios ---------------------------------------------------------------


def test_a_cap_below_the_request_bound_is_refused_before_any_call(tmp_path):
    bound = C.request_bound_usd(QUESTION, SENTENCES, config=CONFIG, prices=PRICES)
    a = adapters()
    with pytest.raises(GuardRefused, match="could cost up to"):
        run(
            adapters=a,
            guard=guard(tmp_path, per_request_cap=bound * 0.99),
            passcode="open-sesame",
            session="s1",
        )
    assert a["anthropic"].requests == []


class ManyClaims(ScriptedAdapter):
    """Every answer splits into 10 claims: more than live mode judges."""

    def __init__(self):
        super().__init__(self.answer)

    def answer(self, req):
        if to_jsonable(req.json_schema) == extraction_schema():
            claims = [
                {
                    "text": f"Wounds healed faster, point {i}.",
                    "cited": [2],
                    "addresses_question": True,
                }
                for i in range(10)
            ]
            return json.dumps({"claims": claims})
        return reply(req)


def test_a_cap_equal_to_the_bound_grades_every_card(tmp_path):
    """The bound is exact: at cap == bound nothing stops early, even with many claims."""
    bound = C.request_bound_usd(QUESTION, SENTENCES, config=CONFIG, prices=PRICES)
    a = ManyClaims()
    g = guard(tmp_path, per_request_cap=bound, daily_cap=10.0)
    cards = run(
        adapters={"anthropic": a, "openai": a}, guard=g, passcode="open-sesame", session="s1"
    )
    assert all(c.error is None and c.groundedness == 1.0 for c in cards)
    assert all(c.n_claims == C.MAX_CLAIMS and "2 more claims" in (c.note or "") for c in cards)
    judged = [r for r in a.requests if to_jsonable(r.json_schema) == verdict_schema()]
    assert all(r.max_output_tokens == C.LIVE_JUDGE_MAX_TOKENS for r in judged)
    assert 0 < g.spend.spent(g._today()) < bound  # settled to the real spend


def test_timed_out_calls_are_counted_not_free():
    slow = Slow(lambda r: reply(r), {"claude-haiku-5-5"})
    cards = run(adapters={"anthropic": slow, "openai": slow}, gen_timeout=0.3)
    by = {c.model: c for c in cards}
    assert by["claude-haiku-5-5"].error == "timed out" and by["claude-haiku-5-5"].cost_usd > 0


class SlowJudge(ScriptedAdapter):
    async def complete(self, req):
        if to_jsonable(req.json_schema) == verdict_schema():
            await asyncio.sleep(1.0)
        return await super().complete(req)


def test_a_slow_judge_marks_grading_timed_out():
    a = SlowJudge(lambda r: reply(r))
    cards = run(adapters={"anthropic": a, "openai": a}, grade_timeout=0.3)
    assert all(c.error == "grading timed out" and c.answer for c in cards)
    assert all(c.cost_usd > 0 for c in cards)


def test_wrong_passcode_never_fetches(tmp_path):
    calls = []

    def fetch(pmid):
        calls.append(pmid)
        return list(SENTENCES)

    with pytest.raises(GuardRefused, match="passcode"):
        run(source=C.PMID("123"), fetch=fetch, guard=guard(tmp_path), passcode="x", session="s")
    assert calls == []
    run(
        source=C.PMID("123"),
        fetch=fetch,
        guard=guard(tmp_path),
        passcode="open-sesame",
        session="s",
    )
    assert calls == ["123"]  # fetched exactly once per request


def test_rate_limited_requests_make_no_model_calls(tmp_path):
    g = guard(tmp_path, rate_limit=1)
    run(guard=g, passcode="open-sesame", session="s1")
    a = adapters()
    with pytest.raises(GuardRefused, match="Rate limit"):
        run(adapters=a, guard=g, passcode="open-sesame", session="s1")
    assert a["anthropic"].requests == []


def test_over_long_abstracts_are_refused_before_any_call():
    a = adapters()
    long_abstract = C.Abstract(tuple(f"Sentence number {i} about the trial." for i in range(80)))
    with pytest.raises(ValueError, match="too long"):
        run(adapters=a, source=long_abstract)
    assert a["anthropic"].requests == []


def test_nothing_is_written_to_the_working_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run()
    assert list(tmp_path.iterdir()) == []


def test_replay_key_ignores_whitespace_like_live_mode():
    assert C.replay_key(QUESTION, [" a ", "", "b"]) == C.replay_key(f" {QUESTION} ", ["a", "b"])


def test_two_processes_share_one_daily_cap(tmp_path):
    """CMP-05 through the Guard: guards in two 'processes' share the database; together they
    can never hold more than the daily cap, and settling returns what was not spent."""
    spend_path = tmp_path / "spend.db"
    g1 = Guard("pw", SqliteSpend(spend_path), per_request_cap=1.0, daily_cap=2.5)
    g2 = Guard("pw", SqliteSpend(spend_path), per_request_cap=1.0, daily_cap=2.5)
    t1 = g1.reserve("pw", "a", 1.0)
    t2 = g2.reserve("pw", "b", 1.0)
    with pytest.raises(GuardRefused, match="budget"):
        g1.reserve("pw", "c", 1.0)  # 1.0 + 1.0 held; another 1.0 would pass 2.5
    g1.settle(t1, 0.05)
    g2.settle(t2, 0.07)
    assert g1.spend.spent(t1.day) == pytest.approx(0.12)
    g2.reserve("pw", "c", 1.0)  # room again after settling


@pytest.mark.parametrize(
    "q",
    [
        "Is it safe to take ibuprofen while pregnant?",
        "I'm on warfarin, is turmeric okay?",
        "Should we switch our son to this inhaler?",
        "Do you recommend statins for a 60-year-old woman?",
        "Is 2000 IU of vitamin D too much?",
        "My dad was prescribed metformin, should he keep taking it?",
        "Can I give my baby melatonin?",
        "What dose should I take for back pain?",
    ],
)
def test_more_advice_phrasings_get_the_notice(q):
    assert C.is_advice_seeking(q)


@pytest.mark.parametrize(
    "q",
    [
        "What dose was used in the trial?",
        "How much do patients take on average in this study?",
        "Do I-131 treatments reduce thyroid volume?",
        "Was the intervention safe in older adults?",
        "Did patients on warfarin have more bleeding?",
    ],
)
def test_research_questions_about_doses_and_safety_do_not(q):
    assert not C.is_advice_seeking(q)


def test_non_ascii_text_is_estimated_by_bytes_not_characters():
    from evalkit.cost import estimate_tokens

    english = "Wounds healed faster with the new dressing. " * 10
    cjk = "新敷料组的伤口愈合更快。" * 10
    assert estimate_tokens(english) <= len(english) // 3 + 1  # unchanged for English
    assert estimate_tokens(cjk) >= len(cjk.encode("utf-8"))  # a true upper bound
    a = adapters()
    long_cjk = C.Abstract(
        tuple("新敷料组的伤口愈合更快，且无严重不良事件。" * 8 for _ in range(30))
    )
    with pytest.raises(ValueError, match="too long"):
        run(adapters=a, source=long_cjk)  # fits the character limit, not the token one
    assert a["anthropic"].requests == []
    short_cjk = C.Abstract(("三十名患者接受了新敷料。", "新敷料组的伤口愈合更快。"))
    b_en = C.request_bound_usd(QUESTION, SENTENCES, config=CONFIG, prices=PRICES)
    b_cjk = C.request_bound_usd(QUESTION, short_cjk.sentences, config=CONFIG, prices=PRICES)
    assert b_cjk > 0 and b_en > 0


def test_dense_ascii_is_bounded_too():
    """Under the demo's strict count, digits and symbols are bounded by bytes like any text."""
    from evalkit.cost import STRICT_TOKENS, estimate_tokens

    dense = "1 2 " * 500
    assert estimate_tokens(dense) < len(dense)  # the realistic count used elsewhere
    with C.strict_tokens():
        assert STRICT_TOKENS.get() and estimate_tokens(dense) >= len(dense.encode())
    assert not STRICT_TOKENS.get()  # nothing leaks outside the demo
    prose = C.request_bound_usd(QUESTION, SENTENCES, config=CONFIG, prices=PRICES)
    digits = C.request_bound_usd(QUESTION, ("1 2 " * 300,), config=CONFIG, prices=PRICES)
    assert digits > prose  # denser text, larger hold


def test_the_token_limit_message_names_every_limit():
    with pytest.raises(ValueError, match="bytes of text"):
        C.check_limits(QUESTION, ("新敷料组的伤口愈合更快。" * 200,))
