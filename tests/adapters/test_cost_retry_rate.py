import asyncio
import random

import pytest

from evalkit.cost import (
    TokenAssumptions,
    UnknownPrice,
    call_cost,
    estimate_run_cost,
    load_prices,
    price_for,
    reconcile,
)
from evalkit.ratelimit import TokenBucket
from evalkit.retry import backoff_delays, is_retryable, with_retries
from tests.adapters.fakes import StatusError

PRICES = load_prices()


# ---- ADP-04 prices and per-call cost ---------------------------------------------------


@pytest.mark.contract
def test_prices_cover_every_model_named_in_decisions():
    for m in (
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "gpt-6.1-sol",
        "gpt-6-luna",
        "gpt-6.1-terra",
    ):
        assert m in PRICES, m
    assert PRICES["gpt-6.1-sol"].input == PRICES["claude-sonnet-5-5"].input  # matched pair
    assert PRICES["gpt-6-luna"].output == PRICES["claude-haiku-5-5"].output
    assert PRICES["claude-haiku-5-5"].verified is False


def test_call_cost_formula_and_batch_discount():
    p = PRICES["claude-opus-5-5"]
    usd = call_cost(
        p,
        tokens_in=1000,
        tokens_out=100,
        tokens_reasoning=50,
        tokens_cached=400,
        tokens_cache_write=100,
    )
    assert usd == pytest.approx((500 * 4 + 400 * 0.2 + 100 * 5 + 150 * 20) / 1e6)
    batch = call_cost(
        p,
        tokens_in=1000,
        tokens_out=100,
        tokens_reasoning=50,
        tokens_cached=400,
        tokens_cache_write=100,
        batch=True,
    )
    assert batch == pytest.approx(usd / 2)


def test_inconsistent_tokens_and_unknown_models_are_errors():
    with pytest.raises(ValueError, match="inconsistent"):
        call_cost(PRICES["gpt-6-luna"], tokens_in=10, tokens_out=1, tokens_cached=20)
    with pytest.raises(UnknownPrice):
        price_for(PRICES, "mystery-model")


# ---- ADP-05 estimate and reconcile ------------------------------------------------------


def test_estimate_breaks_down_by_node_and_model():
    est = estimate_run_cost(
        PRICES,
        models=["claude-sonnet-5-5", "claude-haiku-5-5", "gpt-6.1-sol", "gpt-6-luna"],
        extractor="claude-haiku-5-5",
        judges=["claude-opus-5-5", "gpt-6.1-sol"],
        n_cases=200,
    )
    nodes = [(line.node, line.model, line.calls) for line in est.lines]
    assert nodes[0] == ("generate", "claude-sonnet-5-5", 200)
    assert ("extract_claims", "claude-haiku-5-5", 800) in nodes
    # Judges score every claim: 800 answers x 4.5 measured claims per answer (D-46).
    assert ("judge", "claude-opus-5-5", 3600) in nodes and ("judge", "gpt-6.1-sol", 3600) in nodes
    assert est.total_usd == pytest.approx(sum(line.usd for line in est.lines))
    # Measured assumptions (12-case real run, D-46), judges and extractor at medium effort.
    assert est.total_usd == pytest.approx(14.13, abs=0.01)
    assert not est.over(20.0)


def test_estimate_scales_with_assumptions():
    a = estimate_run_cost(
        PRICES,
        models=["gpt-6.1-sol"],
        extractor="claude-haiku-5-5",
        judges=["claude-opus-5-5"],
        n_cases=10,
    )
    b = estimate_run_cost(
        PRICES,
        models=["gpt-6.1-sol"],
        extractor="claude-haiku-5-5",
        judges=["claude-opus-5-5"],
        n_cases=10,
        assumptions=TokenAssumptions(t_reason=4000),
    )
    assert b.total_usd > a.total_usd


def test_reconcile_flags_gaps_over_ten_percent():
    assert reconcile(1.05, 1.00) == (True, pytest.approx(0.05))
    ok, gap = reconcile(1.20, 1.00)
    assert not ok and gap == pytest.approx(0.20)
    assert reconcile(0.0, 0.0) == (True, 0.0)


# ---- ADP-06 retry policy ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "retry"),
    [
        (StatusError(429), True),
        (StatusError(500), True),
        (StatusError(529), True),
        (StatusError(408), True),
        (StatusError(409), True),
        (StatusError(400), False),
        (StatusError(401), False),
        (StatusError(404), False),
        (TimeoutError(), True),
        (ConnectionError(), True),
        (ValueError("schema"), False),
    ],
)
def test_retry_classification(exc, retry):
    assert is_retryable(exc) is retry


def test_retries_stop_at_six_attempts_with_seeded_backoff():
    calls, slept = [], []

    async def always_429():
        calls.append(1)
        raise StatusError(429)

    async def sleep(s):
        slept.append(s)

    with pytest.raises(StatusError):
        asyncio.run(with_retries(always_429, sleep=sleep, rng=random.Random(7)))
    assert len(calls) == 6 and len(slept) == 5
    assert slept == backoff_delays(6, 0.5, 30.0, random.Random(7))
    assert all(0 <= s <= min(30.0, 0.5 * 2**i) for i, s in enumerate(slept))


def test_non_retryable_errors_raise_immediately():
    calls = []

    async def bad():
        calls.append(1)
        raise StatusError(400)

    with pytest.raises(StatusError):
        asyncio.run(with_retries(bad, sleep=lambda s: asyncio.sleep(0)))
    assert calls == [1]


# ---- ADP-07 token bucket ----------------------------------------------------------------


def test_token_bucket_paces_bursts():
    now = [0.0]
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)
        now[0] += s

    async def go():
        bucket = TokenBucket(rate_per_s=1.0, capacity=2, clock=lambda: now[0], sleep=sleep)
        for _ in range(5):
            await bucket.acquire()

    asyncio.run(go())
    assert sum(slept) == pytest.approx(3.0)


def test_token_bucket_validates_arguments():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_s=0, capacity=1)
    with pytest.raises(ValueError):
        asyncio.run(TokenBucket(rate_per_s=1, capacity=2).acquire(3))


# ---- N1.2 verifier regressions ----------------------------------------------------------


def test_long_context_prompts_bill_at_the_long_tier():
    haiku = PRICES["claude-haiku-5-5"]
    short = call_cost(haiku, tokens_in=100_000, tokens_out=0)
    long = call_cost(haiku, tokens_in=100_001, tokens_out=1000)
    assert short == pytest.approx(100_000 * 0.10 / 1e6)
    assert long == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)
    sol = PRICES["gpt-6.1-sol"]
    assert call_cost(sol, tokens_in=300_000, tokens_out=0) == pytest.approx(300_000 * 4.0 / 1e6)


def test_estimate_and_reconcile_from_config_and_records():
    from types import SimpleNamespace as NS

    from evalkit.cost import estimate_for_config, reconcile_records
    from evalkit.schemas import RunConfig

    cfg = RunConfig(
        models=("claude-sonnet-5-5", "gpt-6.1-sol"),
        judges=("claude-opus-5-5",),
        extractor="claude-haiku-5-5",
    )
    est = estimate_for_config(PRICES, cfg, n_cases=10)
    assert {line.node for line in est.lines} == {"generate", "extract_claims", "judge"}
    records = [NS(cost_usd=0.5), NS(cost_usd=None, completion=NS(cost_usd=0.5))]
    assert reconcile_records(records, 1.0) == (True, 0.0)


def test_negative_token_requests_are_rejected():
    with pytest.raises(ValueError):
        asyncio.run(TokenBucket(rate_per_s=1, capacity=2).acquire(-1))
