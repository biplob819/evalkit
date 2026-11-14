import math

import pytest

from evalkit.scorers import mrr, ndcg_at_k, recall_at_k

# ---- RET-01 ----------------------------------------------------------------------------------


def test_recall_at_k():
    assert recall_at_k(["a", "x", "b", "c"], {"a", "b", "c"}, 2).value == pytest.approx(1 / 3)
    assert recall_at_k(["a", "x", "b", "c"], {"a", "b", "c"}, 4).value == 1.0
    assert recall_at_k(["a", "a", "a", "b"], {"a", "b"}, 2).value == 0.5  # a repeat wastes a slot
    assert recall_at_k(["a", "a", "b"], {"a", "b"}, 3).value == 1.0


@pytest.mark.parametrize("k", [0, -1, 1.5, True])
def test_recall_rejects_bad_k(k):
    with pytest.raises(ValueError):
        recall_at_k(["a"], {"a"}, k)


def test_recall_and_ndcg_are_undefined_without_relevant_ids():
    with pytest.raises(ValueError):
        recall_at_k(["a"], set(), 1)
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], set(), 1)


# ---- RET-02 ----------------------------------------------------------------------------------


def test_mrr():
    assert mrr(["x", "y", "a"], {"a"}).value == pytest.approx(1 / 3)
    assert mrr(["a"], {"a"}).value == 1.0
    assert mrr(["x", "y"], {"a"}).value == 0.0
    assert mrr(["x", "x", "a"], {"a"}).value == pytest.approx(1 / 3)  # positions as returned
    assert mrr(["x"], set()).value == 0.0


# ---- RET-03 ----------------------------------------------------------------------------------


def test_ndcg_binary_hand_computed():
    # rel at ranks 1 and 3: DCG = 1 + 1/log2(4) = 1.5 ; IDCG = 1 + 1/log2(3)
    expected = 1.5 / (1 + 1 / math.log2(3))
    assert ndcg_at_k(["a", "x", "b"], {"a", "b"}, 3).value == pytest.approx(expected)


def test_ndcg_graded_hand_computed():
    gains = {"a": 3.0, "b": 1.0}
    # retrieved b then a: DCG = 1 + 3/log2(3); IDCG = 3 + 1/log2(3)
    expected = (1 + 3 / math.log2(3)) / (3 + 1 / math.log2(3))
    assert ndcg_at_k(["b", "a"], {"a", "b"}, 2, gains).value == pytest.approx(expected)


def test_ndcg_bounds_and_validation():
    assert ndcg_at_k(["a", "b"], {"a", "b"}, 2).value == pytest.approx(1.0)
    assert ndcg_at_k(["x", "y"], {"a"}, 2).value == 0.0
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], {"a"}, 1, {"a": -1.0})


def test_metrics_are_deterministic():
    args = (["c", "a", "b"], {"a", "b"})
    assert [recall_at_k(*args, 2), mrr(*args), ndcg_at_k(*args, 3)] == [
        recall_at_k(*args, 2),
        mrr(*args),
        ndcg_at_k(*args, 3),
    ]


def test_repeated_relevant_ids_earn_no_extra_ndcg():
    once = ndcg_at_k(["a", "x"], {"a", "b"}, 2).value
    twice = ndcg_at_k(["a", "a"], {"a", "b"}, 2).value
    assert twice == pytest.approx(once)
