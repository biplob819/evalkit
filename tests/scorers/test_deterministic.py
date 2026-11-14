import importlib.metadata
from types import SimpleNamespace as NS

import pytest

import evalkit.scorers as scorers
from evalkit.schemas import Answer, Case, Completion
from evalkit.scorers import (
    Scorer,
    ScoreResult,
    citation_valid,
    cost,
    get_scorer,
    label_correct,
    latency,
)
from evalkit.scorers import base as scorer_base

CASE = Case(id="pqa-1", question="q", context=("a", "b", "c"), reference_label="yes", source="t")


def comp(label="yes", citations=(1,), error=None, answer=True) -> Completion:
    return Completion(
        model="m",
        model_snapshot="",
        params={},
        raw_text="{}",
        answer=Answer(label=label, answer="x", citations=citations) if answer else None,
        tokens_in=10,
        tokens_out=5,
        latency_ms=1234,
        cost_usd=0.002,
        error=error,
    )


# ---- DET-01 ------------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "expected"), [("yes", 1.0), (" YES ", 1.0), ("no", 0.0)])
def test_label_correct(label, expected):
    assert label_correct(CASE, comp(label=label)).value == expected


def test_errors_and_missing_answers_count_as_wrong():
    assert label_correct(CASE, comp(error="schema", answer=False)).value == 0.0
    assert label_correct(CASE, comp(error="refusal", answer=False)).details == {"reason": "refusal"}
    unlabelled = CASE.model_copy(update={"reference_label": None})
    assert label_correct(unlabelled, comp()).details == {"reason": "unlabelled_case"}


# ---- DET-02 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("citations", "value", "invalid"),
    [((1, 2), 1.0, ()), ((1, 4), 0.5, (4,)), ((3, 3, 3), 1.0, ()), ((9,), 0.0, (9,))],
)
def test_citation_valid(citations, value, invalid):
    r = citation_valid(CASE, comp(citations=citations))
    assert r.value == value and r.details["invalid"] == invalid


def test_no_citations_or_no_answer_scores_zero():
    assert citation_valid(CASE, comp(citations=())).details == {"reason": "no_citations"}
    assert citation_valid(CASE, comp(error="transport", answer=False)).value == 0.0


# ---- DET-03 ------------------------------------------------------------------------------


def test_cost_and_latency_report_even_for_errors():
    errored = comp(error="transport", answer=False)
    assert cost(CASE, errored).value == 0.002
    assert latency(CASE, errored).value == 1234.0


def test_scorers_are_pure():
    c = comp(citations=(1, 4))
    assert [s(CASE, c) for s in (label_correct, citation_valid)] == [
        s(CASE, c) for s in (label_correct, citation_valid)
    ]


# ---- DET-04 ------------------------------------------------------------------------------


@pytest.mark.contract
def test_public_api_and_protocol():
    for name in ("label_correct", "citation_valid", "cost", "latency", "get_scorer", "Scorer"):
        assert name in scorers.__all__, name
    for s in (label_correct, citation_valid, cost, latency):
        assert isinstance(s, Scorer) and s.version == "1"
        assert get_scorer(s.name) is s
        assert isinstance(s(CASE, comp()), ScoreResult)
    with pytest.raises(KeyError, match="available"):
        get_scorer("nope")


# ---- DET-05 ------------------------------------------------------------------------------


def test_plugin_scorers_are_discovered_and_isolated(monkeypatch):
    class Shout:
        name, version = "shout", "1"

        def __call__(self, case, completion, **ctx):
            return ScoreResult(value=1.0)

    def boom():
        raise ImportError("plugin_dep")

    eps = [
        NS(name="shout", load=lambda: Shout()),
        NS(name="broken", load=boom),
        NS(name="label_correct", load=lambda: Shout()),
        NS(name="notascorer", load=lambda: object()),
    ]
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps)
    monkeypatch.setattr(scorer_base, "_REGISTRY", dict(scorer_base._REGISTRY))
    monkeypatch.setattr(scorer_base, "_FAILED", set())
    monkeypatch.setattr(scorer_base, "_PLUGIN_SOURCE", {})
    with pytest.warns(UserWarning) as caught:
        found = scorer_base.discover()
    assert found["shout"](CASE, comp()).value == 1.0
    assert found["label_correct"] is label_correct
    assert "notascorer" not in found and "broken" not in found
    text = " ".join(str(w.message) for w in caught)
    assert "failed to load" in text and "built in" in text and "not a Scorer" in text
