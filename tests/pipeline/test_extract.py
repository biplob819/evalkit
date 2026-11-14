import asyncio
import json
from typing import Any

import pytest

from evalkit.cost import load_prices
from evalkit.pipeline import extract as ex
from evalkit.pipeline.answer import SchemaError
from evalkit.pipeline.extract import (
    EXTRACTOR_VERSION,
    ClaimSet,
    ExtractClaims,
    ExtractInput,
    coverage,
    extract_all,
    parse_claims,
)
from evalkit.pipeline.generate import Generated
from evalkit.runtime import Executor, RunContext, Store
from evalkit.schemas import Answer, Case, Completion, RunConfig
from tests.pipeline.fakes import ScriptedAdapter

PRICES = load_prices()
CFG = RunConfig(
    models=("claude-sonnet-5-5",), judges=("claude-opus-5-5",), extractor="claude-haiku-5-5"
)
ANSWER = Answer(label="yes", answer="Readmissions fell. Costs were unchanged.", citations=(2, 4))


def inp(**over: Any) -> ExtractInput:
    base: dict[str, Any] = dict(
        case_id="pqa-1",
        model="sonnet",
        question="Did readmissions fall?",
        answer_text=ANSWER.answer,
        citations=ANSWER.citations,
        extractor_id="claude-haiku-5-5",
        provider="anthropic",
        effort="low",
    )
    base.update(over)
    return ExtractInput(**base)


def reply(claims):
    return json.dumps({"claims": claims})


GOOD = reply(
    [
        {"text": "Readmissions fell.", "cited": [2], "addresses_question": True},
        {"text": "Costs were unchanged.", "cited": [4], "addresses_question": False},
    ]
)


# ---- EXT-01 pinned extractor ---------------------------------------------------------------


def test_extractor_version_is_pinned_to_the_prompt():
    assert ex.version_for(ex.EXTRACTOR_PROMPT) == EXTRACTOR_VERSION
    assert EXTRACTOR_VERSION.startswith("ext2-") and len(EXTRACTOR_VERSION) == 13
    assert ex.version_for(ex.EXTRACTOR_PROMPT + " ") != EXTRACTOR_VERSION
    with pytest.raises(ValueError, match="not the current"):
        inp(extractor_version="ext0-oldold00")


# ---- EXT-02 the node ---------------------------------------------------------------------


def test_extract_node_sends_structured_low_effort_request_and_names_claims():
    fake = ScriptedAdapter(lambda req: GOOD)
    out = asyncio.run(ExtractClaims({"anthropic": fake}, PRICES).run(inp(), RunContext(run_id="r")))
    req = fake.requests[0]
    assert req.model_id == "claude-haiku-5-5" and req.params == {"effort": "low"}
    assert req.json_schema is not None and "Readmissions fell." in req.prompt
    assert [c.id for c in out.claims] == ["pqa-1:sonnet:0", "pqa-1:sonnet:1"]
    assert all(c.extractor_version == EXTRACTOR_VERSION for c in out.claims)
    assert out.claims[0].cited == (2,) and out.error is None
    assert "Readmissions fell." in req.prompt


def _generated(error=None, answer=ANSWER):
    case = Case(
        id="pqa-1",
        question="Did readmissions fall?",
        context=("a",) * 5,
        reference_label="yes",
        source="t",
    )
    comp = Completion(
        model="claude-sonnet-5-5",
        model_snapshot="",
        params={},
        raw_text="",
        answer=answer if error is None else None,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
        cost_usd=0.0,
        error=error,
    )
    return Generated(case=case, model="claude-sonnet-5-5", completion=comp)


def test_extract_all_skips_unusable_answers_and_caches(tmp_path):
    fake = ScriptedAdapter(lambda req: GOOD)
    gens = [_generated(), _generated(error="schema"), _generated(error="transport")]
    out = asyncio.run(
        extract_all(Executor(Store(tmp_path)), gens, CFG, {"anthropic": fake}, PRICES, run_id="r")
    )
    assert (
        len(out) == 3 and len(out[0].claims) == 2 and out[1] == ClaimSet() and out[2] == ClaimSet()
    )
    assert len(fake.requests) == 1
    asyncio.run(
        extract_all(Executor(Store(tmp_path)), gens, CFG, {"anthropic": fake}, PRICES, run_id="r2")
    )
    assert len(fake.requests) == 1


# ---- EXT-03 B11 rules --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        reply([{"text": "x", "cited": [9], "addresses_question": True}]),  # invented citation
        reply(
            [
                {"text": "A.", "cited": [], "addresses_question": True},
                {"text": " a. ", "cited": [], "addresses_question": True},
            ]
        ),  # duplicate
        reply([]),  # dropped everything
        reply([{"text": "  ", "cited": [], "addresses_question": True}]),
        reply([{"text": "x", "cited": ["2"], "addresses_question": True}]),
        reply([{"text": "x", "cited": [2], "addresses_question": "yes"}]),
        reply([{"text": "x", "cited": [2]}]),
        "not json",
        "Here you go: " + GOOD,
    ],
)
def test_b11_violations_are_schema_errors(raw):
    with pytest.raises(SchemaError):
        parse_claims(raw, inp())


def test_unparseable_extraction_is_recorded_not_retried():
    fake = ScriptedAdapter(lambda req: "nope")
    out = asyncio.run(ExtractClaims({"anthropic": fake}, PRICES).run(inp(), RunContext(run_id="r")))
    assert out.error == "schema" and out.claims == () and len(fake.requests) == 1


# ---- EXT-04 coverage ---------------------------------------------------------------------


def test_coverage_is_the_share_of_claims_addressing_the_question():
    claims, flags = parse_claims(GOOD, inp())
    cov = coverage(ClaimSet(claims=claims, addresses_question=flags))
    assert cov.value == 0.5
    assert cov.details == {"n_claims": 2, "n_addressing": 1, "extractor_version": EXTRACTOR_VERSION}
    assert coverage(ClaimSet()).value == 0.0
    evasive_inp = inp(answer_text="The abstract describes a study.", citations=())
    evasive = parse_claims(
        reply(
            [{"text": "The abstract describes a study.", "cited": [], "addresses_question": False}]
        ),
        evasive_inp,
    )
    assert coverage(ClaimSet(claims=evasive[0], addresses_question=evasive[1])).value == 0.0


def test_extraction_is_deterministic_for_the_same_output():
    assert parse_claims(GOOD, inp()) == parse_claims(GOOD, inp())
    assert ex.extraction_schema() == ex.extraction_schema()


# ---- A2.3 verifier regressions (lesson L-23) -----------------------------------------------


@pytest.mark.parametrize(
    ("claims", "why"),
    [
        (
            [
                {"text": "Readmissions fell.", "cited": [2], "addresses_question": True},
                {"text": "Costs were unchanged.", "cited": [4], "addresses_question": False},
                {
                    "text": "Mortality doubled in elderly smokers.",
                    "cited": [2],
                    "addresses_question": True,
                },
            ],
            "adds content",
        ),
        ([{"text": "Readmissions fell.", "cited": [2], "addresses_question": True}], "drop"),
    ],
)
def test_claims_that_add_or_drop_content_are_rejected(claims, why):
    long_answer = inp(
        answer_text="Readmissions fell sharply. Costs were unchanged across hospitals."
    )
    with pytest.raises(SchemaError, match=why):
        parse_claims(reply(claims), long_answer)


def test_extra_top_level_keys_are_rejected():
    with pytest.raises(SchemaError):
        parse_claims(json.dumps({"claims": [], "notes": "x"}), inp(answer_text=""))


def test_case_ids_with_colons_are_refused():
    with pytest.raises(ValueError, match="':'"):
        inp(case_id="a:b")


def test_claim_set_lengths_must_align():
    claims, _ = parse_claims(GOOD, inp())
    with pytest.raises(ValueError, match="same length"):
        ClaimSet(claims=claims[:1], addresses_question=(True, True, True))


def test_extraction_transport_failures_are_per_item(tmp_path):
    from tests.adapters.fakes import StatusError

    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return ConnectionError("down") if calls["n"] == 1 else GOOD

    case2 = _generated()
    case2 = Generated(
        case=case2.case.model_copy(update={"id": "pqa-2"}),
        model=case2.model,
        completion=case2.completion,
    )
    gens = [_generated(), case2]
    out = asyncio.run(
        extract_all(
            Executor(Store(tmp_path)),
            gens,
            CFG,
            {"anthropic": ScriptedAdapter(flaky)},
            PRICES,
            run_id="r",
        )
    )
    assert sorted([cs.error or "ok" for cs in out]) == ["ok", "transport"]
    with pytest.raises(StatusError):
        asyncio.run(
            extract_all(
                Executor(Store(tmp_path / "b")),
                gens,
                CFG,
                {"anthropic": ScriptedAdapter(lambda r: StatusError(401))},
                PRICES,
                run_id="r",
            )
        )


def test_prompt_edits_are_caught_by_the_version_check(tmp_path):
    import importlib.util
    import sys
    import textwrap

    from evalkit.runtime import versions

    def load(prompt, tag):
        path = tmp_path / f"m_{tag}.py"
        path.write_text(
            textwrap.dedent(f"""
            from pydantic import BaseModel
            from evalkit.runtime import Node
            class N(BaseModel):
                x: int
            PROMPT = {prompt!r}
            class P(Node[N, N]):
                name = "p"
                code_version = "1"
                prompt = PROMPT
                In = N
                Out = N
                async def run(self, inp, ctx):
                    return inp
        """),
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location(f"evk_prompt_{tag}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"evk_prompt_{tag}"] = mod
        spec.loader.exec_module(mod)
        return versions.node_classes(mod)

    manifest = versions.build_manifest(load("Say A.", "a"))
    problems = versions.check(manifest, load("Say B.", "b"))
    assert len(problems) == 1 and versions.BUMP_HINT in problems[0]
