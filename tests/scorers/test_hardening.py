"""Regression tests for the A2.2 verifier findings (lesson L-19)."""

import importlib.metadata
import pickle
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from evalkit.schemas import Answer, Case, Completion
from evalkit.scorers import ScoreResult, cost, get_scorer, label_correct, latency, scorer
from evalkit.scorers import base as sb

CASE = Case(id="c", question="q", context=("a", "b"), reference_label="yes", source="t")


def comp(label="yes") -> Completion:
    return Completion(
        model="m",
        model_snapshot="",
        params={},
        raw_text="{}",
        answer=Answer(label=label, answer="x", citations=(1,)),
        tokens_in=1,
        tokens_out=1,
        latency_ms=7,
        cost_usd=0.5,
    )


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(sb, "_REGISTRY", dict(sb._REGISTRY))
    monkeypatch.setattr(sb, "_BUILTIN", dict(sb._BUILTIN))
    monkeypatch.setattr(sb, "_PLUGIN_SOURCE", {})
    monkeypatch.setattr(sb, "_FAILED", set())


def _eps(monkeypatch, eps):
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps)


def test_plugin_cannot_replace_a_builtin_via_the_decorator(monkeypatch, isolated_registry):
    def sneaky_load():
        @scorer("label_correct")
        def evil(case, completion, **_):
            return ScoreResult(value=1.0)

        return evil

    _eps(monkeypatch, [NS(name="myplug", value="pkg:evil", load=sneaky_load)])
    with pytest.warns(UserWarning, match="failed to load"):
        found = sb.discover()
    assert found["label_correct"] is label_correct
    assert "myplug" not in sb._BUILTIN


def test_tampering_with_the_registry_is_restored(monkeypatch, isolated_registry):
    def tamper():
        sb._REGISTRY["cost"] = latency
        return sb.FunctionScorer(lambda c, x, **_: ScoreResult(value=0.0), "fine", "1")

    _eps(monkeypatch, [NS(name="fine", value="pkg:fine", load=tamper)])
    with pytest.warns(UserWarning, match="replaced built-in scorer 'cost'"):
        found = sb.discover()
    assert found["cost"] is cost and "fine" in found


def test_duplicate_plugin_names_warn(monkeypatch, isolated_registry):
    def mk(name):
        return lambda: sb.FunctionScorer(lambda c, x, **_: ScoreResult(value=0.0), name, "1")

    _eps(
        monkeypatch,
        [
            NS(name="dup", value="a:one", load=mk("dup")),
            NS(name="dup", value="b:two", load=mk("dup")),
        ],
    )
    with pytest.warns(UserWarning, match="already provided by a:one"):
        sb.discover()


def test_broken_plugins_load_once_and_bad_metadata_is_survivable(monkeypatch, isolated_registry):
    calls = []

    def boom():
        calls.append(1)
        raise ImportError("dep")

    _eps(monkeypatch, [NS(name="broken", value="pkg:broken", load=boom)])
    with pytest.warns(UserWarning):
        sb.discover()
    sb.discover()
    sb.discover()
    assert calls == [1]

    def explode(group):
        raise RuntimeError("metadata is corrupt")

    monkeypatch.setattr(importlib.metadata, "entry_points", explode)
    with pytest.warns(UserWarning, match="could not read scorer plugins"):
        assert get_scorer("cost") is cost


def test_plugin_name_must_match_its_entry_point(monkeypatch, isolated_registry):
    other = sb.FunctionScorer(lambda c, x, **_: ScoreResult(value=0.0), "other", "1")
    _eps(monkeypatch, [NS(name="mismatch", value="pkg:m", load=lambda: other)])
    with pytest.warns(UserWarning, match="names must match"):
        assert "mismatch" not in sb.discover()


def test_scorer_attributes_must_be_strings():
    class Bad:
        name, version = 42, None

        def __call__(self, case, completion, **ctx):
            return ScoreResult(value=0.0)

    assert not sb.is_valid_scorer(Bad())
    assert sb.is_valid_scorer(label_correct)


def test_builtin_scorers_pickle_and_keep_function_metadata():
    assert pickle.loads(pickle.dumps(label_correct)) is label_correct
    assert label_correct.__name__ == "label_correct" and label_correct.__wrapped__ is not None


@pytest.mark.parametrize("bad", ["0.5", True, float("nan")])
def test_score_values_are_strict_finite_floats(bad):
    with pytest.raises(ValidationError):
        ScoreResult(value=bad)


@pytest.mark.parametrize("label", ["ＹＥＳ", "​yes", "yes ", " Yes "])
def test_labels_are_unicode_normalised(label):
    assert label_correct(CASE, comp(label=label)).value == 1.0


def test_cost_latency_and_registry_are_deterministic():
    c = comp()
    assert [cost(CASE, c), latency(CASE, c)] == [cost(CASE, c), latency(CASE, c)]
    assert get_scorer("latency") is get_scorer("latency")
