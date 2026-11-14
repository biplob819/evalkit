import random

import pytest

from evalkit.stats import Resamples, holm, interval, paired, percentile_ms


def test_same_seed_same_intervals_and_point_is_the_mean():
    vals = [1.0] * 60 + [0.0] * 40
    a = interval(vals, Resamples(100, 500, seed=7))
    b = interval(vals, Resamples(100, 500, seed=7))
    assert a == b and a.point == pytest.approx(0.6) and a.lo < 0.6 < a.hi
    assert interval(vals, Resamples(100, 500, seed=8)) != a


def test_constant_values_give_a_zero_width_interval():
    iv = interval([1.0] * 50, Resamples(50, 200))
    assert (iv.lo, iv.point, iv.hi) == (1.0, 1.0, 1.0)


def test_coverage_is_close_to_nominal():
    rng = random.Random(1)
    rs = Resamples(200, 500, seed=3)  # resample indices are independent of the data
    hits = 0
    trials = 150
    for _ in range(trials):
        vals = [1.0 if rng.random() < 0.6 else 0.0 for _ in range(200)]
        iv = interval(vals, rs)
        hits += iv.lo <= 0.6 <= iv.hi
    assert 0.88 <= hits / trials <= 0.99


def test_paired_identical_models_are_never_separated_and_big_gaps_are():
    rs = Resamples(200, 1000, seed=0)
    a = [float(i % 3 == 0) for i in range(200)]
    iv, p = paired(a, a, rs)
    assert (iv.lo, iv.point, iv.hi) == (0.0, 0.0, 0.0) and p == 1.0
    strong = [1.0] * 180 + [0.0] * 20
    weak = [1.0] * 80 + [0.0] * 120
    iv2, p2 = paired(strong, weak, rs)
    assert iv2.lo > 0 and p2 < 0.01 and p2 > 0  # +1 correction: never exactly zero


def test_paired_uses_shared_cases():
    rs = Resamples(4, 100)
    with pytest.raises(ValueError, match="same cases"):
        paired([1.0, 0.0, 1.0, 1.0], [1.0, 0.0, 1.0], rs)
    with pytest.raises(ValueError, match="one per case"):
        interval([1.0, 0.0], rs)


def test_holm_step_down():
    ps = {"ab": 0.01, "ac": 0.04, "bc": 0.03}
    # sorted 0.01 <= 0.05/3, 0.03 > 0.05/2 -> stop: only ab rejected
    assert holm(ps) == {"ab": True, "ac": False, "bc": False}
    assert holm({"ab": 0.01, "ac": 0.02, "bc": 0.04}) == {"ab": True, "ac": True, "bc": True}
    assert holm({"x": 0.2}) == {"x": False} and holm({}) == {}


def test_holm_controls_family_wise_error_under_the_null():
    rng = random.Random(5)
    rs = Resamples(100, 300, seed=1)
    false_any = 0
    runs = 60
    for _ in range(runs):
        models = [[1.0 if rng.random() < 0.5 else 0.0 for _ in range(100)] for _ in range(4)]
        ps = {
            (i, j): paired(models[i], models[j], rs)[1] for i in range(4) for j in range(i + 1, 4)
        }
        false_any += any(holm(ps).values())
    assert false_any / runs <= 0.15  # nominal 5%; generous bound for 60 runs


def test_percentile_ms_nearest_rank():
    assert percentile_ms([], 0.5) == 0
    assert percentile_ms([100, 200, 300, 400], 0.5) == 200
    assert percentile_ms([100, 200, 300, 400], 0.95) == 400
    assert percentile_ms([100, 200, 300, 400, 500], 0.5) == 300  # nearest rank, not round()


def test_input_validation():
    with pytest.raises(ValueError):
        Resamples(0)
    with pytest.raises(ValueError):
        Resamples(10, 50)
    with pytest.raises(ValueError, match="finite"):
        interval([float("nan")] * 10, Resamples(10, 100))


def test_p_value_uses_the_plus_one_correction_exactly():
    rs = Resamples(50, 1000, seed=2)
    _, p = paired([1.0] * 50, [0.0] * 50, rs)
    assert p == pytest.approx(2 / 1001)
