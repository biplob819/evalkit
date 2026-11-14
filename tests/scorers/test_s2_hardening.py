"""Regression tests for the S2.1/S2.2 verifier findings (lesson L-22)."""

import random
import time
from decimal import Decimal

import pytest

from evalkit.schemas import ResultSet
from evalkit.scorers import mrr, ndcg_at_k, recall_at_k, sql_match


def rs(rows, cols=("v",), ordered=False):
    return ResultSet(columns=cols, rows=tuple(tuple(r) for r in rows), ordered=ordered)


# ---- SQL blocker: the unordered path must honour the tolerance exactly ---------------------


@pytest.mark.parametrize(
    ("p", "g", "kw"),
    [(1e-9, 4e-7, {}), (0.0, 4.9e-7, {}), (0.1000001, 0.1000004, {"rel_tol": 0, "abs_tol": 0})],
)
def test_unordered_never_accepts_values_outside_tolerance(p, g, kw):
    assert sql_match(rs([[p]]), rs([[g]]), **kw).value == 0.0
    assert sql_match(rs([[p]]), rs([[g]], ordered=True), **kw).value == 0.0


def test_ordered_and_unordered_agree_on_random_noise():
    rng = random.Random(7)
    for _ in range(200):
        g = [[rng.uniform(-1, 1)] for _ in range(3)]
        p = [[v[0] + rng.choice([0, 1e-12, 1e-5])] for v in g]
        unordered = sql_match(rs(p), rs(g)).value
        ordered = sql_match(rs(p), rs(g, ordered=True)).value
        assert unordered >= ordered  # same pairing is available to the unordered matcher


# ---- SQL: bipartite matching, not greedy ----------------------------------------------------


def test_tolerant_pairing_finds_a_matching_greedy_misses():
    pred, gold = rs([[1.5], [0.6]]), rs([[1.0], [2.4]])
    assert sql_match(pred, gold, rel_tol=0, abs_tol=1).value == 1.0


def test_matching_respects_non_numeric_columns():
    pred = rs([["a", 1.0], ["b", 2.0]], cols=("k", "v"))
    gold = rs([["a", 2.0], ["b", 1.0]], cols=("k", "v"))
    assert sql_match(pred, gold, rel_tol=0, abs_tol=0.5).value == 0.0
    assert sql_match(pred, gold, rel_tol=0, abs_tol=1.5).value == 1.0


# ---- SQL: performance ---------------------------------------------------------------------


def test_noisy_large_sets_match_quickly():
    rng = random.Random(1)
    gold = [[rng.uniform(0, 1000)] for _ in range(20_000)]
    pred = [[v[0] * (1 + rng.uniform(-5e-7, 5e-7))] for v in gold]
    rng.shuffle(pred)
    t0 = time.perf_counter()
    assert sql_match(rs(pred), rs(gold)).value == 1.0
    assert time.perf_counter() - t0 < 3.0


# ---- SQL minors ----------------------------------------------------------------------------


def test_huge_integers_are_compared_exactly_at_zero_tolerance():
    assert sql_match(rs([[10**17]]), rs([[10**17 + 1]]), rel_tol=0, abs_tol=0).value == 0.0
    assert sql_match(rs([[10**17]]), rs([[10**17]]), rel_tol=0, abs_tol=0).value == 1.0


def test_decimals_are_numbers():
    assert sql_match(rs([[Decimal("1.0")]]), rs([[Decimal("1.00")]])).value == 1.0
    assert sql_match(rs([[Decimal("2.5")]]), rs([[2.5]])).value == 1.0


def test_unordered_mismatch_names_an_example_row():
    r = sql_match(rs([[1], [2]]), rs([[1], [3]]))
    assert r.value == 0.0 and r.details["reason"] == "rows_differ"
    assert r.details["example_gold_row"] in ((3,), (2,))


def test_bad_tolerances_raise():
    with pytest.raises(ValueError):
        sql_match(rs([[1]]), rs([[1]]), rel_tol=1.5)


# ---- retrieval minors ----------------------------------------------------------------------


def test_any_negative_gain_raises_and_all_zero_is_undefined():
    with pytest.raises(ValueError):
        ndcg_at_k(["x"], {"a"}, 1, {"x": -5.0})
    with pytest.raises(ValueError, match="zero"):
        ndcg_at_k(["a"], {"a"}, 1, {"a": 0.0})


def test_strings_are_not_id_lists():
    for call in (
        lambda: recall_at_k("abc", {"a"}, 1),
        lambda: mrr(["a"], "a"),
        lambda: ndcg_at_k(["a"], "a", 1),
    ):
        with pytest.raises(TypeError):
            call()


def test_mrr_contract_edges():
    assert mrr([], {"a"}).value == 0.0
    assert mrr(["a"], {"a"}).details == {"first_relevant_rank": 1}


def test_ndcg_is_reproducible():
    args = (["b", "a", "c"], {"a", "b"}, 3, {"a": 2.0, "b": 1.0})
    assert ndcg_at_k(*args) == ndcg_at_k(*args)


def test_bipartite_path_when_sorted_pairing_fails():
    pred = rs([[0.0, 5.0], [0.6, 0.0]], cols=("a", "b"))
    gold = rs([[0.5, 0.0], [0.9, 5.0]], cols=("a", "b"))
    assert sql_match(pred, gold, rel_tol=0, abs_tol=1).value == 1.0


def test_matcher_agrees_with_brute_force_on_small_sets():
    import itertools
    import math as m

    rng = random.Random(11)
    for _ in range(300):
        n = rng.randint(1, 5)
        gold = [[round(rng.uniform(0, 3), 1), round(rng.uniform(0, 3), 1)] for _ in range(n)]
        pred = [[round(rng.uniform(0, 3), 1), round(rng.uniform(0, 3), 1)] for _ in range(n)]

        def close(a, b):
            return all(
                abs(x - y) <= 0.5 or m.isclose(x, y, abs_tol=0.5) for x, y in zip(a, b, strict=True)
            )

        brute = any(
            all(close(p, gold[j]) for p, j in zip(pred, perm, strict=True))
            for perm in itertools.permutations(range(n))
        )
        got = sql_match(
            rs(pred, cols=("a", "b")), rs(gold, cols=("a", "b")), rel_tol=0, abs_tol=0.5
        )
        assert (got.value == 1.0) == brute, (pred, gold)
