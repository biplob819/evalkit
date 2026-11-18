"""Clustered clarification intervals (agentic-analytics OWNER D-100, EvalKit L-36).

With k trials per question the answers are clustered; `clarification_rates(..., cluster=)`
switches both intervals to the two-level cluster bootstrap in `evalkit.stats`.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from evalkit.metrics import Metric
from evalkit.scorers import clarification_rates
from evalkit.stats import Resamples, clopper_pearson, interval, two_level_interval

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "clarify" / "seventeen_configs.json"
N = 2000  # resamples in the property tests; the fixture uses the default 10,000


def _flatten(ambiguous: list[str], clear: list[str]) -> tuple[list[bool], list[bool], list[str]]:
    """'TFT' strings per question -> one entry per answer, with its question id."""
    expected, asked, qid = [], [], []
    for kind, rows in (("a", ambiguous), ("c", clear)):
        for j, s in enumerate(rows):
            for ch in s:
                expected.append(kind == "a")
                asked.append(ch == "T")
                qid.append(f"{kind}{j:03d}")
    return expected, asked, qid


def _width(iv) -> float:
    return iv.hi - iv.lo


def test_default_result_is_byte_identical_to_0_3():
    """Without `cluster` nothing changes: the JSON below is what 0.3.0 wrote."""
    e = [True] * 5 + [False] * 5
    a = [True, True, False, True, False, False, True, False, False, False]
    # 0.3.0 computed each rate's interval with Clopper-Pearson over answers; compare with that
    # computation in this process (the last float digits differ across platforms, so a
    # literal JSON string from one machine is not a portable oracle)
    from evalkit.scorers.clarify import _clopper_pearson

    got = json.loads(clarification_rates(e, a).model_dump_json())
    assert list(got) == [
        "n_ambiguous",
        "n_clear",
        "asked",
        "false_clarifications",
        "ask_rate",
        "false_clarification_rate",
        "method",
    ]
    assert (got["n_ambiguous"], got["n_clear"], got["asked"], got["false_clarifications"]) == (
        5,
        5,
        3,
        1,
    )
    assert got["method"] == "clopper-pearson"
    for key, (k, n) in {"ask_rate": (3, 5), "false_clarification_rate": (1, 5)}.items():
        iv = _clopper_pearson(k, n, 0.05)
        assert got[key] == {"point": iv.point, "lo": iv.lo, "hi": iv.hi} and iv.point == k / n
    assert abs(got["ask_rate"]["lo"] - 0.14663279963467474) < 1e-12  # the 0.3.0 value
    assert clarification_rates(e, a, cluster=None) == clarification_rates(e, a)


def test_agrees_with_agentic_analytics_two_level_interval_on_seventeen_configs():
    """The vendored fixture holds agentic-analytics' `aggregate.two_level_interval` output
    (Bootstrap(10000, seed=0)) for 17 synthetic configurations (16 ambiguous and 84 clear
    questions, k = 3). Points are equal; each interval end is within one step of the rate's
    lattice, 1/(k * questions) (0.021 for 16 x 3, 0.004 for 84 x 3): both draw questions from
    the same `Resamples` matrix and differ only in the level-two trial stream (measured worst
    case: one step; Monte Carlo sd of an end across seeds up to 0.010)."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(doc["configs"]) == 17
    k = doc["k"]
    for c in doc["configs"]:
        e, a, q = _flatten(c["ambiguous"], c["clear"])
        r = clarification_rates(e, a, cluster=q, seed=doc["seed"], n_resamples=doc["n_resamples"])
        assert r.method == "cluster-bootstrap"
        for name, n_q in (
            ("ask_rate", len(c["ambiguous"])),
            ("false_clarification_rate", len(c["clear"])),
        ):
            ours, ref = getattr(r, name), c["reference"][name]
            assert ours.point == pytest.approx(ref["point"], abs=1e-12), (c["name"], name)
            step = 1 / (k * n_q) + 1e-9
            assert abs(ours.lo - ref["lo"]) <= step, (c["name"], name, ours, ref)
            assert abs(ours.hi - ref["hi"]) <= step, (c["name"], name, ours, ref)


def test_clustered_interval_is_wider_when_trials_within_a_question_agree():
    """Every question's 3 trials agree: 48 answers carry the information of 16 questions, so
    Clopper-Pearson over answers is too narrow and the cluster bootstrap is wider."""
    rows = ["TTT"] * 8 + ["FFF"] * 8
    e, a, q = _flatten(rows, [])
    exact = clarification_rates(e, a).ask_rate
    clustered = clarification_rates(e, a, cluster=q, n_resamples=N).ask_rate
    assert exact is not None and clustered is not None
    assert clustered.point == exact.point == 0.5
    assert _width(clustered) > 1.3 * _width(exact)
    # it is close to Clopper-Pearson over the 16 questions, the honest sample size
    per_question = clopper_pearson(8, 16)
    assert _width(clustered) == pytest.approx(_width(per_question), rel=0.25)


@pytest.mark.parametrize("seed", [1, 2, 3, 11])
def test_clustered_interval_is_close_to_clopper_pearson_when_trials_are_independent(seed):
    """Independent trials: no clustering to correct for, so the widths are close. The two-level
    bootstrap is conservative here (both stages add the within-question variance, so its width
    can reach sqrt(2) times the one-level width; measured 1.16-1.35x Clopper-Pearson over 80
    questions x 3), and it is never narrower."""
    rng = random.Random(seed)
    rows = ["".join("T" if rng.random() < 0.5 else "F" for _ in range(3)) for _ in range(80)]
    e, a, q = _flatten(rows, [])
    exact = clarification_rates(e, a).ask_rate
    clustered = clarification_rates(e, a, cluster=q, n_resamples=N).ask_rate
    assert exact is not None and clustered is not None
    assert 0.95 * _width(exact) <= _width(clustered) <= 1.45 * _width(exact)


def test_one_answer_per_question_keeps_the_exact_interval():
    """k = 1 has no clustering: the exact Clopper-Pearson intervals stand, with the question
    counts filled in, rather than a bootstrap that collapses at 0/n."""
    e = [True] * 10 + [False] * 20
    a = [True] * 4 + [False] * 6 + [False] * 20
    q = [f"q{i}" for i in range(30)]
    plain = clarification_rates(e, a)
    one = clarification_rates(e, a, cluster=q)
    assert one.method == "clopper-pearson" and one.ci_method == "clopper-pearson"
    assert (one.ask_rate, one.false_clarification_rate) == (
        plain.ask_rate,
        plain.false_clarification_rate,
    )
    assert (one.n_ambiguous_questions, one.n_clear_questions) == (10, 20)
    # the shared bootstrap itself reduces to the case-only bootstrap at k = 1
    rs = Resamples(10, N, seed=0)
    vals = [1.0] * 4 + [0.0] * 6
    assert two_level_interval([[v] for v in vals], rs, "s") == interval(vals, rs)


def test_seeded_reproducible_and_labelled():
    rows = ["TTF", "FFF", "TTT", "TFF", "FFT", "TTT", "FFF", "TFT"]
    e, a, q = _flatten(rows, ["FFF", "FTF", "FFF"])
    one = clarification_rates(e, a, cluster=q, seed=3, n_resamples=500)
    assert one == clarification_rates(e, a, cluster=q, seed=3, n_resamples=500)
    # answer order inside the data does not move the result: questions are taken in id order
    order = list(range(len(a)))
    random.Random(0).shuffle(order)
    shuffled = clarification_rates(
        [e[i] for i in order],
        [a[i] for i in order],
        cluster=[q[i] for i in order],
        seed=3,
        n_resamples=500,
    )
    assert shuffled.ask_rate is not None and one.ask_rate is not None
    assert shuffled.ask_rate.point == pytest.approx(one.ask_rate.point)
    other = clarification_rates(e, a, cluster=q, seed=4, n_resamples=500)
    assert other.ask_rate is not None
    assert other.ask_rate.point == one.ask_rate.point
    assert one.ci_method.startswith("two-level cluster bootstrap")
    assert "500 resamples, seed 3" in one.ci_method
    dumped = json.loads(one.model_dump_json())
    assert dumped["method"] == "cluster-bootstrap"
    assert (dumped["seed"], dumped["n_resamples"]) == (3, 500)
    assert (dumped["n_ambiguous_questions"], dumped["n_clear_questions"]) == (8, 3)


def test_a_project_writes_the_clustered_interval_into_metrics_1():
    """docs/plugins.md: ci_low / ci_high / ci_method come from the rate and its `ci_method`;
    `evalkit gate` carries a supplied interval exactly, never recomputing Clopper-Pearson."""
    from evalkit.gates import _interval

    e, a, q = _flatten(["TTT", "FFF", "TTF", "TTT", "FFF", "FFF"], [])
    r = clarification_rates(e, a, cluster=q, n_resamples=500)
    iv = r.ask_rate
    assert iv is not None
    m = Metric(
        name="clarification_rate",
        unit="rate",
        value=iv.point,
        n=r.n_ambiguous,
        k=r.asked,  # every question has 3 answers, so the point is asked / n
        ci_low=iv.lo,
        ci_high=iv.hi,
        ci_method=r.ci_method,
        labels={"model": "m"},
    )
    assert _interval(m) == (iv.lo, iv.hi, r.ci_method)


@pytest.mark.parametrize(
    ("cluster", "match"),
    [
        (["q1"] * 5, "one question id per answer"),
        ("q1q2q", "one question id per answer"),
        (["q1", "q1", "q1", "q2", None, "q3"], "strings or integers"),
        (["q1", "q1", "q1", "q2", 1.5, "q3"], "strings or integers"),
        (["q1", "q1", "q2", "q3", "q3", "q1"], "both ambiguous and clear"),
        ([1, 1, 1, 2, "2", 3], "read the same"),
    ],
)
def test_cluster_ids_are_checked(cluster, match):
    e = [True, True, True, False, False, False]
    a = [True, False, True, False, False, True]
    with pytest.raises(ValueError, match=match):
        clarification_rates(e, a, cluster=cluster)
