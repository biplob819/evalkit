import asyncio
import json

import pytest

from evalkit.cost import load_prices
from evalkit.pipeline import judge as jm
from evalkit.pipeline.answer import SchemaError
from evalkit.pipeline.judge import (
    BLIND_DENYLIST,
    JUDGE_PROMPT_HASH,
    PLANTED_CLAIMS,
    groundedness,
    judge_claims,
    judge_input,
    parse_verdict,
    planted_claim_check,
    planted_inputs,
    precheck,
    render_judge_prompt,
    select_judges,
)
from evalkit.runtime import BudgetExceeded, Executor, Store
from evalkit.runtime.canonical import canonical_json
from evalkit.schemas import Case, Claim, ClaimVerdict, RunConfig
from tests.pipeline.fakes import ScriptedBatchAdapter

PRICES = load_prices()
CFG = RunConfig(
    models=("claude-sonnet-5-5", "gpt-6.1-sol"),
    judges=("claude-opus-5-5", "gpt-6.1-sol"),
    extractor="claude-haiku-5-5",
)
CASE = Case(
    id="pqa-1",
    question="q",
    context=("Readmissions fell by 12%.", "Costs rose.", "N=400."),
    reference_label="yes",
    source="t",
)


def claim(i=0, cited=(1,), text="Readmissions fell.") -> Claim:
    return Claim(id=f"pqa-1:m:{i}", text=text, cited=cited, extractor_version="ext2-x")


def supported(req):
    return json.dumps({"verdict": "supported", "evidence": [1], "quote": None, "rationale": "s1"})


def run(executor, items, adapter, judge="claude-opus-5-5", **kw):
    ads = {"anthropic": adapter, "openai": adapter}
    return asyncio.run(judge_claims(executor, items, judge, CFG, ads, PRICES, run_id="r", **kw))


# ---- JDG-01 rubric and hash ------------------------------------------------------------------


def test_rubric_hash_is_stamped_and_pinned():
    # The hash covers everything that shapes a verdict, including the blinding rules.
    parts = [
        jm.JUDGE_RUBRIC,
        jm.verdict_schema(),
        jm.SENTENCE_FORMAT,
        jm.JUDGE_CODE_VERSION,
        jm._BLIND_RE.pattern,
        jm._SPACED_RE.pattern,
        sorted(jm._STRONG),
        jm.JUDGE_MAX_OUTPUT_TOKENS,
        sorted(jm._BLIND_FOLD.items()),
        sorted(jm._FOLD.items()),
    ]
    expected = __import__("hashlib").sha256(canonical_json(parts).encode())
    assert expected.hexdigest()[:16] == JUDGE_PROMPT_HASH
    assert JUDGE_PROMPT_HASH == "9a3a8246049f6f85"  # pinned: a prompt edit must update this
    v = parse_verdict(supported(None), judge_input(claim(), CASE, "claude-opus-5-5", "low"))
    assert v.judge_prompt_hash == JUDGE_PROMPT_HASH
    with pytest.raises(ValueError, match="rubric_hash"):
        judge_input(claim(), CASE, "j", "low").model_copy(update={}).model_validate(
            {**judge_input(claim(), CASE, "j", "low").model_dump(), "rubric_hash": "old"}
        )


# ---- JDG-02 blinding ------------------------------------------------------------------------


@pytest.mark.safety
def test_judge_prompts_never_name_models_or_providers():
    for i, text in enumerate(
        ["Readmissions fell.", "Costs rose sharply.", "The cohort had 400 patients."]
    ):
        for judge in ("claude-opus-5-5", "gpt-6.1-sol"):
            prompt = render_judge_prompt(
                judge_input(claim(i, (1, 2), text), CASE, judge, "low")
            ).casefold()
            for name in BLIND_DENYLIST:
                assert name not in prompt, (name, prompt)
            assert "Readmissions fell by 12%." in render_judge_prompt(
                judge_input(claim(i, (1,), text), CASE, judge, "low")
            )
            assert "N=400." not in render_judge_prompt(
                judge_input(claim(i, (1,), text), CASE, judge, "low")
            )


# ---- JDG-03/04 batch flow ---------------------------------------------------------------------


@pytest.mark.parametrize("judge", ["claude-opus-5-5", "gpt-6.1-sol"])
def test_batch_once_cache_per_claim_second_run_free(tmp_path, judge):
    fake = ScriptedBatchAdapter(supported)
    items = [(CASE, claim(i, (1,), f"Readmissions fell, point {i}.")) for i in range(3)]
    res = run(Executor(Store(tmp_path)), items, fake, judge, max_wait_s=1)
    assert res.status == "complete" and len(res.verdicts) == 3 and len(fake.submitted) == 1
    assert {r.request.model_id for r in fake.submitted[0]} == {judge}
    assert all(r.request.params == {"effort": "medium"} for r in fake.submitted[0])
    res2 = run(Executor(Store(tmp_path)), items, fake, judge, max_wait_s=1)
    assert len(fake.submitted) == 1 and len(res2.verdicts) == 3 and res2.cost_usd == 0


def test_pending_returns_without_blocking_and_resume_reuses_the_batch(tmp_path):
    fake = ScriptedBatchAdapter(supported, pending_polls=1)
    items = [(CASE, claim())]
    first = run(Executor(Store(tmp_path)), items, fake, max_wait_s=0)
    assert first.status == "pending" and first.batch_ids == ["batch_1"]
    second = run(Executor(Store(tmp_path)), items, fake, max_wait_s=0)
    assert (
        second.status == "complete" and len(fake.submitted) == 1 and second.batch_ids == ["batch_1"]
    )


def test_batch_spend_obeys_the_cost_cap(tmp_path):
    fake = ScriptedBatchAdapter(supported)
    with pytest.raises(BudgetExceeded):
        run(Executor(Store(tmp_path), max_cost_usd=1e-9), [(CASE, claim())], fake, max_wait_s=1)
    assert fake.submitted == []
    ex = Executor(Store(tmp_path / "ok"), max_cost_usd=10)
    run(ex, [(CASE, claim())], fake, max_wait_s=1)
    assert ex.spent_usd > 0


# ---- JDG-05 parsing and exclusions ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"verdict": "maybe", "evidence": [], "quote": null, "rationale": "x"}',
        '{"verdict": "supported", "evidence": [2], "quote": null, "rationale": "x"}',
        '{"verdict": "supported", "evidence": ["1"], "quote": null, "rationale": "x"}',
        '{"verdict": "supported", "evidence": [1], "rationale": "x"}',
        'Supported. {"verdict": "supported", "evidence": [1], "quote": null, "rationale": "x"}',
    ],
)
def test_bad_verdicts_are_schema_errors(raw):
    with pytest.raises(SchemaError):
        parse_verdict(raw, judge_input(claim(), CASE, "j", "low"))


def test_parse_failures_and_batch_errors_exclude_and_count(tmp_path):
    items = [
        (CASE, claim(0, (1,), "Readmissions fell.")),
        (CASE, claim(1, (1,), "Readmissions dropped.")),
    ]
    fake = ScriptedBatchAdapter(
        lambda req: "garbage" if "dropped" in req.prompt else supported(req)
    )
    res = run(Executor(Store(tmp_path)), items, fake, max_wait_s=1)
    assert set(res.verdicts) == {"pqa-1:m:0"} and res.n_errors == 1
    fake2 = ScriptedBatchAdapter(supported, item_error=lambda cid: "errored:overloaded")
    res2 = run(Executor(Store(tmp_path / "b")), items, fake2, max_wait_s=1)
    assert res2.verdicts == {} and res2.n_errors == 2
    res3 = run(
        Executor(Store(tmp_path / "b")), items, ScriptedBatchAdapter(supported), max_wait_s=1
    )
    assert len(res3.verdicts) == 2  # transient batch failures are not cached; a rerun retries


# ---- JDG-06 groundedness and the B3 precheck -----------------------------------------------------


def test_precheck_marks_uncited_and_missing_citations_unsupported_without_calls(tmp_path):
    for cited in ((), (9,)):
        pre = precheck(claim(cited=cited), CASE, "j")
        assert pre is not None and pre.verdict == "unsupported"
    assert precheck(claim(cited=(1,)), CASE, "j") is None
    fake = ScriptedBatchAdapter(supported)
    res = run(Executor(Store(tmp_path)), [(CASE, claim(cited=()))], fake, max_wait_s=1)
    assert fake.submitted == [] and res.verdicts["pqa-1:m:0"].verdict == "unsupported"


def test_groundedness_and_fully_grounded():
    cs = [claim(0), claim(1), claim(2)]

    def v(i, verdict):
        return ClaimVerdict(
            claim_id=cs[i].id,
            judge="j",
            verdict=verdict,
            evidence=(),
            rationale="",
            judge_prompt_hash=JUDGE_PROMPT_HASH,
        )

    all_sup = {c.id: v(i, "supported") for i, c in enumerate(cs)}
    assert groundedness(cs, all_sup) == (1.0, True)
    missing = dict(list(all_sup.items())[:2])
    assert groundedness(cs, missing) == (1.0, False)  # one claim unjudged: not fully grounded
    one_bad = {**all_sup, cs[2].id: v(2, "unsupported")}
    assert groundedness(cs, one_bad) == (pytest.approx(2 / 3), False)
    assert groundedness([], {}) == (0.0, False)


# ---- JDG-07 planted claims ---------------------------------------------------------------------


def test_planted_claim_fixture_and_check():
    assert len(PLANTED_CLAIMS) == 10
    inputs = planted_inputs("claude-opus-5-5")
    for inp, p in zip(inputs, PLANTED_CLAIMS, strict=True):
        assert p.claim not in render_judge_prompt(inp).split("Cited source sentences:")[1]

    def verdicts(kind):
        return [
            ClaimVerdict(
                claim_id=i.claim_id,
                judge="j",
                verdict=kind,
                evidence=(),
                rationale="",
                judge_prompt_hash=JUDGE_PROMPT_HASH,
            )
            for i in inputs
        ]

    assert planted_claim_check(verdicts("unsupported")) == []
    assert len(planted_claim_check(verdicts("supported"))) == 10
    assert planted_claim_check(verdicts("unsupported")[:9]) == ["planted:9 has no verdict"]


# ---- JDG-08 judge selection -------------------------------------------------------------------


def test_select_judges_dual_single_none():
    assert select_judges(CFG, {"anthropic", "openai"}).judge_mode == "dual"
    single = select_judges(CFG, {"openai"})
    assert single.judges == ("gpt-6.1-sol",) and single.judge_mode == "single"
    with pytest.raises(ValueError, match="no judge"):
        select_judges(CFG, set())


# ---- A2.4 verifier regressions (L-24) ---------------------------------------------------------


def test_expired_batch_items_are_resubmitted_on_the_next_run(tmp_path):
    items = [(CASE, claim())]
    expired = ScriptedBatchAdapter(supported, item_error=lambda cid: "expired")
    first = run(Executor(Store(tmp_path)), items, expired, max_wait_s=1)
    assert first.verdicts == {} and first.n_errors == 1
    fresh = ScriptedBatchAdapter(supported)
    second = run(Executor(Store(tmp_path)), items, fresh, max_wait_s=1)
    assert len(fresh.submitted) == 1 and set(second.verdicts) == {"pqa-1:m:0"}


def test_partial_cache_only_submits_the_missing_claims(tmp_path):
    a, b = claim(0, (1,), "Readmissions fell."), claim(1, (1,), "Readmissions dropped.")
    fake = ScriptedBatchAdapter(supported)
    run(Executor(Store(tmp_path)), [(CASE, a)], fake, max_wait_s=1)
    res = run(Executor(Store(tmp_path)), [(CASE, a), (CASE, b)], fake, max_wait_s=1)
    assert len(fake.submitted) == 2 and len(fake.submitted[1]) == 1  # only b is paid for again
    assert set(res.verdicts) == {a.id, b.id}


def test_custom_ids_are_provider_safe_and_reveal_nothing(tmp_path):
    fake = ScriptedBatchAdapter(supported)
    secret = claim(0, (1,), "Readmissions fell among the pqa-1 cohort.")
    run(Executor(Store(tmp_path)), [(CASE, secret)], fake, max_wait_s=1)
    cid = fake.submitted[0][0].custom_id
    assert __import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,64}", cid)
    assert "pqa" not in cid and "Readmissions" not in cid


@pytest.mark.safety
def test_redaction_is_whole_word_on_claims_and_sentences():
    case = CASE.model_copy(
        update={"context": ("As Claude-Opus noted, readmissions fell in the cytosol arm.",)}
    )
    inp = judge_input(claim(0, (1,), "GPT-6.1-sol says readmissions fell."), case, "j", "low")
    prompt = render_judge_prompt(inp)
    assert "[model] says readmissions fell." in prompt
    assert "As [model] noted" in prompt and "cytosol" in prompt  # inside words is untouched
    assert all(n not in prompt.casefold().replace("cytosol", "") for n in BLIND_DENYLIST)


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ('{"verdict":"unsupported","evidence":[1],"quote":null,"rationale":"x"}', False),
        ('{"verdict":"supported","evidence":[],"quote":null,"rationale":"x"}', False),
        ('{"verdict":"supported","evidence":[1],"quote":"costs doubled","rationale":"x"}', False),
        (
            '{"verdict":"supported","evidence":[1],"quote":"readmissions FELL","rationale":"x"}',
            True,
        ),
        ('{"verdict":"unsupported","evidence":[],"quote":null,"rationale":"x"}', True),
    ],
)
def test_evidence_and_quote_must_agree_with_the_verdict(raw, ok):
    inp = judge_input(claim(), CASE, "j", "low")
    if ok:
        assert parse_verdict(raw, inp).claim_id == "pqa-1:m:0"
    else:
        with pytest.raises(SchemaError):
            parse_verdict(raw, inp)


def test_unparseable_batch_verdict_gets_one_live_retry(tmp_path):
    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else supported(req)

    fake = ScriptedBatchAdapter(reply)
    ex = Executor(Store(tmp_path), max_cost_usd=10)
    res = run(ex, [(CASE, claim())], fake, max_wait_s=1)
    assert set(res.verdicts) == {"pqa-1:m:0"} and res.n_errors == 0 and calls["n"] == 2
    assert ex.spent_usd == pytest.approx(0.002)  # batch call plus the live retry
    assert ex.in_flight_usd == pytest.approx(0.0)


def test_dual_mode_needs_two_providers():
    same = CFG.model_copy(update={"judges": ("claude-opus-5-5", "claude-sonnet-5-5")})
    sel = select_judges(same, {"anthropic", "openai"})
    assert sel.judge_mode == "single" and sel.judges == ("claude-opus-5-5",)
    assert select_judges(CFG, {"anthropic", "openai"}).judge_mode == "dual"
    three = CFG.model_copy(update={"judges": (*CFG.judges, "claude-sonnet-5-5")})
    with pytest.raises(ValueError, match="at most two"):
        select_judges(three, {"anthropic", "openai"})


def test_waiting_never_overruns_max_wait(tmp_path):
    fake = ScriptedBatchAdapter(supported, pending_polls=10**6)
    t0 = __import__("time").monotonic()
    res = run(
        Executor(Store(tmp_path)), [(CASE, claim())], fake, max_wait_s=0.2, poll_interval_s=60
    )
    assert res.status == "pending" and __import__("time").monotonic() - t0 < 1.5


def test_open_batch_holds_budget_until_collected_and_releases_once(tmp_path):
    fake = ScriptedBatchAdapter(supported, pending_polls=1)
    ex = Executor(Store(tmp_path), max_cost_usd=10)
    items = [(CASE, claim())]
    first = asyncio.run(
        judge_claims(ex, items, "claude-opus-5-5", CFG, {"anthropic": fake}, PRICES, run_id="r")
    )
    held = ex.in_flight_usd
    assert first.status == "pending" and held > 0 and ex.spent_usd == 0
    second = asyncio.run(
        judge_claims(ex, items, "claude-opus-5-5", CFG, {"anthropic": fake}, PRICES, run_id="r")
    )
    assert second.status == "complete"
    assert ex.in_flight_usd == pytest.approx(0.0) and ex.spent_usd == pytest.approx(0.001)


# ---- A2.4 verifier pass 2 regressions (L-24) --------------------------------------------------


@pytest.mark.safety
def test_redaction_never_changes_real_abstracts():
    fixture = __import__("pathlib").Path("tests/fixtures/pubmedqa_mini.json")
    data = json.loads(fixture.read_text(encoding="utf-8"))
    sentences = [s for row in data for s in row["contexts"]]
    assert sentences
    changed = [s for s in sentences if jm.redact(s) != s]
    assert changed == []


@pytest.mark.parametrize(
    "quote",
    ["patient’s 12 % drop", "PATIENT'S   12% DROP", "fell ... 12%", "costs – rose"],
)
def test_quote_matching_tolerates_typography(quote):
    assert jm.quote_in(quote, ["The patient's 12% drop: readmissions fell by 12%.", "costs - rose"])


def test_quote_matching_still_rejects_invented_text():
    assert not jm.quote_in("costs doubled", ["Costs rose."])
    assert not jm.quote_in("12% ... fell", ["Readmissions fell by 12%."])  # elision order


def test_resume_in_a_new_process_reholds_committed_batch_spend(tmp_path):
    fake = ScriptedBatchAdapter(supported, pending_polls=1)
    first_ex = Executor(Store(tmp_path), max_cost_usd=10)
    run(first_ex, [(CASE, claim(0))], fake, max_wait_s=0)
    committed = first_ex.in_flight_usd
    assert committed > 0
    # A new process whose cap only covers the committed batch cannot submit fresh claims.
    second = Executor(Store(tmp_path), max_cost_usd=committed * 1.5)
    with pytest.raises(BudgetExceeded):
        run(second, [(CASE, claim(0)), (CASE, claim(1, (1,), "Readmissions dropped."))], fake)
    assert len(fake.submitted) == 1


def test_a_failure_mid_collection_still_settles_the_batch(tmp_path):
    class AuthError(Exception):
        status_code = 401

    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else AuthError()

    ex = Executor(Store(tmp_path), max_cost_usd=10)
    with pytest.raises(AuthError):
        run(ex, [(CASE, claim())], ScriptedBatchAdapter(reply), max_wait_s=1)
    # The hold is released; the unresolved claim's cost is recorded when it is re-collected.
    assert ex.in_flight_usd == pytest.approx(0.0) and ex.spent_usd == pytest.approx(0.0)


def test_budget_stop_during_retry_settles_and_leaves_the_claim_for_later(tmp_path):
    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else supported(req)

    probe = Executor(Store(tmp_path / "probe"), max_cost_usd=10)
    run(probe, [(CASE, claim())], ScriptedBatchAdapter(supported, pending_polls=1), max_wait_s=0)
    cap = probe.in_flight_usd * 1.01  # room for the batch, not for a live retry
    ex = Executor(Store(tmp_path), max_cost_usd=cap)
    with pytest.raises(BudgetExceeded):
        run(ex, [(CASE, claim())], ScriptedBatchAdapter(reply), max_wait_s=1)
    assert ex.in_flight_usd == pytest.approx(0.0) and ex.spent_usd == pytest.approx(0.001)
    again = ScriptedBatchAdapter(supported)
    res = run(Executor(Store(tmp_path)), [(CASE, claim())], again, max_wait_s=1)
    assert len(again.submitted) == 1 and set(res.verdicts) == {"pqa-1:m:0"}


def test_concurrent_calls_for_one_judge_submit_once(tmp_path):
    fake = ScriptedBatchAdapter(supported)
    ex = Executor(Store(tmp_path))
    items = [(CASE, claim())]

    async def both():
        ads = {"anthropic": fake}
        return await asyncio.gather(
            judge_claims(ex, items, "claude-opus-5-5", CFG, ads, PRICES, run_id="r", max_wait_s=1),
            judge_claims(ex, items, "claude-opus-5-5", CFG, ads, PRICES, run_id="r", max_wait_s=1),
        )

    a, b = asyncio.run(both())
    assert len(fake.submitted) == 1 and set(a.verdicts) == set(b.verdicts) == {"pqa-1:m:0"}


def test_reported_cost_includes_the_live_retry(tmp_path):
    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else supported(req)

    ex = Executor(Store(tmp_path))
    res = run(ex, [(CASE, claim())], ScriptedBatchAdapter(reply), max_wait_s=1)
    assert res.cost_usd == pytest.approx(ex.spent_usd) == pytest.approx(0.002)


def test_transient_retry_failure_is_not_cached(tmp_path):
    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else ConnectionError("reset")

    res = run(
        Executor(Store(tmp_path)), [(CASE, claim())], ScriptedBatchAdapter(reply), max_wait_s=1
    )
    assert res.n_errors == 1 and res.verdicts == {}
    again = ScriptedBatchAdapter(supported)
    res2 = run(Executor(Store(tmp_path)), [(CASE, claim())], again, max_wait_s=1)
    assert len(again.submitted) == 1 and set(res2.verdicts) == {"pqa-1:m:0"}


def test_ids_missing_from_batch_results_are_counted_and_resubmitted(tmp_path):
    items = [(CASE, claim(0)), (CASE, claim(1, (1,), "Readmissions dropped."))]
    first = ScriptedBatchAdapter(supported, drop=lambda cid: True)
    res = run(Executor(Store(tmp_path)), items, first, max_wait_s=1)
    assert res.verdicts == {} and res.n_errors == 2 and res.status == "complete"
    again = ScriptedBatchAdapter(supported)
    res2 = run(Executor(Store(tmp_path)), items, again, max_wait_s=1)
    assert len(again.submitted) == 1 and len(res2.verdicts) == 2


# ---- A2.4 verifier pass 3 regressions (L-24) --------------------------------------------------

KEEP = [
    "Saline solution reduced pain.",
    "solid tumours, lunar cycles, a terrace",
    "Opus Dei, a sonnet, Gemini surveys",
    "cytosol and sol-gel",
    "serum GPT 120 and GOT 80",  # glutamate pyruvate transaminase, not a model
    "GPT 45 IU/L",
    "the GEMINI 1 trial and the LUNA 2 study",
    "SOL-1 trial, sol 1 mol/L",
    "GPT2 gene expression",
]
REDACT = [
    ("claude_opus and Sonnet 5.5", "[model] and [model]"),
    ("gpt4o, Qwen3 and DeepSeek-R1", "[model], [model] and [model]"),
    ("claude-opus-5-5 vs gpt-6-luna.", "[model] vs [model]."),
    ("Opus 5.5 and Gemini 2.5 Pro", "[model] and [model] Pro"),
    ("GPT-6.1\u2011sol and claude\u2010opus", "[model] and [model]"),  # non-breaking hyphens
    ("Gemini\u20112.5", "[model]"),
    ("Claudes", "[model]"),
    ("\uff27\uff30\uff34-4", "[model]"),  # fullwidth
    ("opus-5-5, GPT-4.", "[model], [model]."),
    (
        "I am Claude, as an OpenAI model; ChatGPT's view",
        "I am [model], as an [model] model; [model]'s view",
    ),
    ("Claude 3 patients", "[model] 3 patients"),  # a bare integer after a space is kept
    ("As Chat GPT, built by Open AI; chat-gpt", "As [model], built by [model]; [model]"),
]


@pytest.mark.safety
@pytest.mark.parametrize("text", KEEP)
def test_redaction_keeps_clinical_and_ordinary_text(text):
    assert jm.redact(text) == text


@pytest.mark.safety
@pytest.mark.parametrize(("text", "expected"), REDACT)
def test_redaction_catches_model_names_in_any_spelling(text, expected):
    assert jm.redact(text) == expected


@pytest.mark.safety
def test_redaction_never_changes_the_full_pqa_l_set_when_cached():
    from evalkit.datasets.pubmedqa import cache_dir, load_pqa_l

    if not (cache_dir() / "pubmedqa_pqa_l.json").is_file():
        pytest.skip("PQA-L not cached on this machine")
    sents = [s for c in load_pqa_l() for s in (*c.context, c.question)]
    assert len(sents) > 10_000
    changed = [s for s in sents if jm.redact(s) != s.translate(jm._BLIND_FOLD)]
    assert changed == []


@pytest.mark.parametrize(
    ("quote", "ok"),
    [
        ("by 1", False),  # cuts "12%" in half
        (".", False),
        ("e", False),
        ("p = 0.04", True),
        ("readmissions fell by 12%", True),
    ],
)
def test_quotes_match_on_word_boundaries(quote, ok):
    assert jm.quote_in(quote, ["Readmissions fell by 12% (p=0.04)."]) is ok


def test_concurrent_callers_retry_and_pay_once(tmp_path):
    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else supported(req)

    fake = ScriptedBatchAdapter(reply)
    ex = Executor(Store(tmp_path))
    items = [(CASE, claim())]

    async def both():
        ads = {"anthropic": fake}
        call = judge_claims(
            ex, items, "claude-opus-5-5", CFG, ads, PRICES, run_id="r", max_wait_s=1
        )
        call2 = judge_claims(
            ex, items, "claude-opus-5-5", CFG, ads, PRICES, run_id="r", max_wait_s=1
        )
        return await asyncio.gather(call, call2)

    a, b = asyncio.run(both())
    assert len(fake.submitted) == 1 and calls["n"] == 2  # one batch answer, one live retry
    assert ex.spent_usd == pytest.approx(0.002)
    assert set(a.verdicts) == set(b.verdicts) == {"pqa-1:m:0"}


def test_tight_cap_on_resume_still_collects_the_paid_batch(tmp_path):
    fake = ScriptedBatchAdapter(supported, pending_polls=1)
    first = Executor(Store(tmp_path), max_cost_usd=10)
    run(first, [(CASE, claim(0))], fake, max_wait_s=0)
    committed = first.in_flight_usd
    second = Executor(Store(tmp_path), max_cost_usd=committed * 1.5)
    with pytest.raises(BudgetExceeded):
        run(second, [(CASE, claim(0)), (CASE, claim(1, (1,), "Readmissions dropped."))], fake)
    # The finished batch was collected and cached despite the budget stop.
    res = run(Executor(Store(tmp_path)), [(CASE, claim(0))], ScriptedBatchAdapter(supported))
    assert set(res.verdicts) == {"pqa-1:m:0"} and res.cost_usd == 0
    assert second.in_flight_usd == pytest.approx(0.0)


def test_a_bad_request_on_one_retry_does_not_block_the_batch(tmp_path):
    class BadRequest(Exception):
        status_code = 400

    def reply(req):
        if "dropped" in req.prompt:
            return "garbage" if not getattr(reply, "batch_done", False) else BadRequest()
        return supported(req)

    items = [
        (CASE, claim(0, (1,), "Readmissions dropped.")),
        (CASE, claim(1, (1,), "Readmissions fell.")),
        (CASE, claim(2, (1,), "Readmissions fell sharply.")),
    ]

    class Fake(ScriptedBatchAdapter):
        async def collect_batch(self, batch_id, reqs):
            out = await super().collect_batch(batch_id, reqs)
            reply.batch_done = True  # type: ignore[attr-defined]
            return out

    res = run(Executor(Store(tmp_path)), items, Fake(reply), max_wait_s=1)
    assert set(res.verdicts) == {"pqa-1:m:1", "pqa-1:m:2"} and res.n_errors == 1


def test_auth_failure_on_retry_still_stops_the_run(tmp_path):
    class AuthError(Exception):
        status_code = 401

    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else AuthError()

    with pytest.raises(AuthError):
        run(Executor(Store(tmp_path)), [(CASE, claim())], ScriptedBatchAdapter(reply), max_wait_s=1)


def test_partial_collection_records_each_claims_cost_once(tmp_path):
    class AuthError(Exception):
        status_code = 401

    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        if "dropped" in req.prompt:
            return "garbage" if calls["n"] <= 2 else AuthError()
        return supported(req)

    items = [(CASE, claim(0)), (CASE, claim(1, (1,), "Readmissions dropped."))]
    ex1 = Executor(Store(tmp_path))
    fake = ScriptedBatchAdapter(reply)
    with pytest.raises(AuthError):
        run(ex1, items, fake, max_wait_s=1)
    first_spent = ex1.spent_usd
    fake.reply = supported  # the provider still holds the batch; re-collect it
    ex2 = Executor(Store(tmp_path))
    run(ex2, items, fake, max_wait_s=1)
    assert first_spent == pytest.approx(0.001)  # only the claim that was cached
    assert ex2.spent_usd == pytest.approx(0.001)  # the other claim, paid once more in total


def test_refusal_on_retry_is_labelled_refusal(tmp_path):
    from evalkit.adapters import RawCompletion

    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return "garbage"
        return RawCompletion(
            model=req.model,
            model_snapshot="s",
            params={},
            raw_text="",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
            cost_usd=0.001,
            refusal=True,
        )

    ex = Executor(Store(tmp_path))
    run(ex, [(CASE, claim())], ScriptedBatchAdapter(reply), max_wait_s=1)
    key = jm._cache_key(judge_input(claim(), CASE, "claude-opus-5-5", CFG.judge_effort))
    cached = ex.store.get(key, jm._Cached)
    assert cached is not None and cached.error == "refusal"


def test_old_meta_without_an_estimate_is_reheld_at_a_recomputed_estimate(tmp_path):
    fake = ScriptedBatchAdapter(supported, pending_polls=1)
    run(Executor(Store(tmp_path)), [(CASE, claim())], fake, max_wait_s=0)
    store = Store(tmp_path)
    key = jm._cache_key(judge_input(claim(), CASE, "claude-opus-5-5", CFG.judge_effort))
    meta = dict(store.get_meta(key, jm._META) or {})
    meta.pop("estimate")
    store.put_meta(key, jm._META, meta)
    ex = Executor(store, max_cost_usd=10)

    async def peek():
        ads = {"anthropic": ScriptedBatchAdapter(supported, pending_polls=10**6)}
        return await judge_claims(
            ex, [(CASE, claim())], "claude-opus-5-5", CFG, ads, PRICES, run_id="r"
        )

    asyncio.run(peek())
    assert ex.in_flight_usd > 0


def test_a_hung_status_check_does_not_block_past_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "COLLECT_TIMEOUT_FLOOR_S", 0.05)

    class Hung(ScriptedBatchAdapter):
        async def collect_batch(self, batch_id, reqs):
            await asyncio.sleep(30)

    t0 = __import__("time").monotonic()
    res = run(Executor(Store(tmp_path)), [(CASE, claim())], Hung(supported), max_wait_s=0)
    assert res.status == "pending" and __import__("time").monotonic() - t0 < 2


# ---- A2.4 verifier pass 4 regressions ---------------------------------------------------------


@pytest.mark.parametrize(
    "quote",
    [
        '"Readmissions fell by 12%"',
        "'Readmissions fell'",
        "[1] Readmissions fell by 12%",
        "readmissions fell [...] arm",
        "readmissions fell (...) arm",
    ],
)
def test_quote_wrappers_the_prompt_invites_are_accepted(quote):
    assert jm.quote_in(quote, ["Readmissions fell by 12% in the treated arm."])


def test_judge_sees_superscripts_as_written():
    text = "platelets 150\u00d710\u2079/L, p<10\u207b\u2075"
    assert jm.redact(text) == text


def test_claims_resolved_by_another_process_are_reported(tmp_path):
    fake = ScriptedBatchAdapter(supported, pending_polls=1)
    run(Executor(Store(tmp_path)), [(CASE, claim())], fake, max_wait_s=0)  # leaves batch open
    run(Executor(Store(tmp_path)), [(CASE, claim())], fake, max_wait_s=1)  # process B resolves
    # Process A collects the same batch afterwards: the verdict must still be reported.
    inp = judge_input(claim(), CASE, "claude-opus-5-5", CFG.judge_effort)
    res = jm.JudgingResult(judge="claude-opus-5-5")
    batch = {jm.custom_id_for(jm._cache_key(inp)): inp}
    collected = asyncio.run(
        fake.collect_batch("batch_1", {k: jm._request(v) for k, v in batch.items()})
    )
    assert collected is not None
    asyncio.run(
        jm._process_batch(
            Executor(Store(tmp_path)),
            fake,
            PRICES["claude-opus-5-5"],
            "batch_1",
            batch,
            collected,
            res,
        )
    )
    assert set(res.verdicts) == {"pqa-1:m:0"} and res.cost_usd == 0


def test_provider_errors_on_retry_are_never_cached(tmp_path):
    class CreditLow(Exception):
        status_code = 400

    calls = {"n": 0}

    def reply(req):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else CreditLow()

    res = run(
        Executor(Store(tmp_path)), [(CASE, claim())], ScriptedBatchAdapter(reply), max_wait_s=1
    )
    assert res.n_errors == 1
    again = ScriptedBatchAdapter(supported)
    res2 = run(Executor(Store(tmp_path)), [(CASE, claim())], again, max_wait_s=1)
    assert len(again.submitted) == 1 and set(res2.verdicts) == {"pqa-1:m:0"}


def test_full_run_batch_holds_fit_under_the_default_cap():
    """Real 12-case run (D-46): worst-case holds would need ~$33 for Opus alone at 200 cases."""
    from evalkit.cost import TokenAssumptions

    case = CASE.model_copy(
        update={
            "context": tuple(
                "A typical abstract sentence of about thirty words." * 3 for _ in range(8)
            )
        }
    )
    inp = judge_input(claim(0, (1, 2)), case, "claude-opus-5-5", "medium")
    claims = 800 * TokenAssumptions().claims_per_answer
    opus = jm._estimate(PRICES["claude-opus-5-5"], jm._request(inp), batch=True) * claims
    sol = jm._estimate(PRICES["gpt-6.1-sol"], jm._request(inp), batch=True) * claims
    assert opus + sol + 1.5 < 20  # both holds at once, plus generation and extraction
    from evalkit.cost import call_cost

    worst = call_cost(
        PRICES["claude-opus-5-5"], tokens_in=600, tokens_out=jm.JUDGE_MAX_OUTPUT_TOKENS, batch=True
    )
    assert worst * claims > 20  # the old rule could never have fitted
