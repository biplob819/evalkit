"""Prices, per-call cost, pre-run estimates and post-run reconciliation (ADP-04, ADP-05)."""

from __future__ import annotations

import contextvars
import math
import warnings
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field

from evalkit.schemas import RunConfig
from evalkit.schemas._base import Model, Name, NonNegFloat, NonNegInt

__all__ = ["Price", "call_cost", "estimate_tokens", "load_prices"]  # public since 0.3.0

PRICES_YAML = Path(__file__).with_name("prices.yaml")
PER_M = 1_000_000


class Price(Model):
    input: NonNegFloat
    output: NonNegFloat
    cache_read: NonNegFloat
    cache_write: NonNegFloat
    batch_discount: float = Field(default=0.5, gt=0, le=1, allow_inf_nan=False)
    checked_on: date
    source: str
    verified: bool = True
    # Long-context tier: prompts above `long_threshold` tokens bill at these prices instead.
    long_threshold: NonNegInt | None = None
    long_input: NonNegFloat | None = None
    long_output: NonNegFloat | None = None
    long_cache_read: NonNegFloat | None = None


FREE = Price(
    input=0,
    output=0,
    cache_read=0,
    cache_write=0,
    checked_on=date(2026, 11, 14),
    source="local or self-hosted model",
    verified=False,
)


class UnknownPrice(KeyError):
    pass


def load_prices(path: Path = PRICES_YAML) -> dict[str, Price]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {model: Price.model_validate(entry) for model, entry in data.items()}


def price_for(prices: dict[str, Price], model_id: str, *, free_if_unknown: bool = False) -> Price:
    """The price for a model id. Unknown models are an error, never a silent $0, unless the
    caller opts in (local OpenAI-compatible servers), which warns."""
    if model_id in prices:
        return prices[model_id]
    if free_if_unknown:
        warnings.warn(f"no price for {model_id!r}; costing it at $0", stacklevel=2)
        return FREE
    raise UnknownPrice(f"no price for model {model_id!r} in prices.yaml")


# When set (the live demo, D-54), every token estimate is a strict upper bound: one token per
# UTF-8 byte for all text, since dense digits or symbols can approach one token per character.
# Context-local, so it covers exactly the calls made inside `compare()` and nothing else.
STRICT_TOKENS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "evalkit_strict_tokens", default=False
)


def estimate_tokens(*texts: str) -> int:
    """A conservative prompt-token count for budgeting: ASCII at about 3 characters per token,
    and every other character at one token per UTF-8 byte, a true upper bound (byte-level
    tokenizers never emit more tokens than bytes). English estimates are unchanged, while CJK,
    emoji or rare scripts can no longer be priced at a third of their real cost (D3.1). Under
    `STRICT_TOKENS` all text counts by bytes (an upper bound for any input)."""
    if STRICT_TOKENS.get():
        return sum(len(t.encode("utf-8")) for t in texts) + 1
    ascii_chars = other_bytes = 0
    for t in texts:
        for ch in t:
            if ch < "\x80":
                ascii_chars += 1
            else:
                other_bytes += len(ch.encode("utf-8"))
    return ascii_chars // 3 + other_bytes + 1


def call_cost(
    price: Price,
    *,
    tokens_in: int,
    tokens_out: int,
    tokens_reasoning: int = 0,
    tokens_cached: int = 0,
    tokens_cache_write: int = 0,
    batch: bool = False,
) -> float:
    """Cost of one call. `tokens_in` is the whole prompt; cached and cache-write tokens are
    the parts of it billed at cache prices. Reasoning tokens are billed as output."""
    uncached = tokens_in - tokens_cached - tokens_cache_write
    if min(uncached, tokens_out, tokens_reasoning, tokens_cached, tokens_cache_write) < 0:
        raise ValueError("token counts are inconsistent (cached + cache writes exceed the prompt)")
    inp, out, cread = price.input, price.output, price.cache_read
    if price.long_threshold is not None and tokens_in > price.long_threshold:
        inp = price.long_input if price.long_input is not None else inp
        out = price.long_output if price.long_output is not None else out
        cread = price.long_cache_read if price.long_cache_read is not None else cread
    usd = (
        uncached * inp
        + tokens_cached * cread
        + tokens_cache_write * price.cache_write
        + (tokens_out + tokens_reasoning) * out
    ) / PER_M
    return usd * (price.batch_discount if batch else 1.0)


class TokenAssumptions(Model):
    """Planning averages per call (Tech Spec §9). Replace with measured values after a smoke run."""

    # Measured on the 12-case real run of 2026-11-14 (D-46): 48 answers, 428 judge calls.
    t_in: NonNegInt = 750
    t_out: NonNegInt = 150
    t_reason: NonNegInt = 50  # generation at the model's configured effort
    t_ext_in: NonNegInt = 500
    t_ext_out: NonNegInt = 200
    t_ext_reason: NonNegInt = 300  # extractor at medium effort (D-34)
    t_j_in: NonNegInt = 450
    t_j_out: NonNegInt = 100
    t_j_reason: NonNegInt = 40  # judges at medium effort (OWNER D-34)
    claims_per_answer: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 4.5  # measured 4.46


# B5.12: a citation reply lists every cited sentence (about 25 tokens each, ~3 per claim)
# instead of one verdict, so its planned output is larger than a claim verdict's.
CITATION_T_OUT = 120


class CostLine(Model):
    node: Name
    model: Name
    calls: NonNegInt
    usd: NonNegFloat


class CostEstimate(Model):
    lines: tuple[CostLine, ...]
    total_usd: NonNegFloat

    def over(self, budget: float | None) -> bool:
        return budget is not None and self.total_usd > budget


def estimate_run_cost(
    prices: dict[str, Price],
    *,
    models: list[str] | tuple[str, ...],
    extractor: str,
    judges: list[str] | tuple[str, ...],
    n_cases: int,
    assumptions: TokenAssumptions | None = None,
    batch_judges: bool = True,
    free: frozenset[str] = frozenset(),
    citation_judges: bool = False,
) -> CostEstimate:
    """generation + extraction + judging (batch unless `batch_judges=False`), per node and
    model (Tech Spec §9). Models in `free` (local endpoints) cost 0 unless priced.
    `citation_judges` (B5.12, tasks listing `citation_support`) adds one citation-judge call
    per claim per judge."""
    a = assumptions or TokenAssumptions()

    def pf(model: str) -> Price:
        return price_for(prices, model, free_if_unknown=model in free)

    lines: list[CostLine] = []
    for m in models:
        per = call_cost(pf(m), tokens_in=a.t_in, tokens_out=a.t_out, tokens_reasoning=a.t_reason)
        lines.append(CostLine(node="generate", model=m, calls=n_cases, usd=per * n_cases))
    answers = len(models) * n_cases
    per = call_cost(
        pf(extractor),
        tokens_in=a.t_ext_in,
        tokens_out=a.t_ext_out,
        tokens_reasoning=a.t_ext_reason,
    )
    lines.append(CostLine(node="extract_claims", model=extractor, calls=answers, usd=per * answers))
    for j in judges:
        per = call_cost(
            pf(j),
            tokens_in=a.t_j_in,
            tokens_out=a.t_j_out,
            tokens_reasoning=a.t_j_reason,
            batch=batch_judges,
        )
        claims = math.ceil(answers * a.claims_per_answer)  # judges score each claim
        lines.append(CostLine(node="judge", model=j, calls=claims, usd=per * claims))
    if citation_judges:
        for j in judges:
            per = call_cost(
                pf(j),
                tokens_in=a.t_j_in,
                tokens_out=CITATION_T_OUT,
                tokens_reasoning=a.t_j_reason,
                batch=batch_judges,
            )
            claims = math.ceil(answers * a.claims_per_answer)  # an upper bound: B3 skips some
            lines.append(CostLine(node="citation_judge", model=j, calls=claims, usd=per * claims))
    return CostEstimate(lines=tuple(lines), total_usd=sum(line.usd for line in lines))


def reconcile(
    recorded_usd: float, provider_usd: float, tolerance: float = 0.10
) -> tuple[bool, float]:
    """Compare EvalKit's summed costs with the provider's usage figure. Returns (ok, gap_ratio)."""
    if provider_usd <= 0:
        return recorded_usd == 0, 0.0 if recorded_usd == 0 else float("inf")
    gap = abs(recorded_usd - provider_usd) / provider_usd
    return gap <= tolerance, gap


def reconcile_records(
    records: Iterable[Any], provider_usd: float, tolerance: float = 0.10
) -> tuple[bool, float]:
    """`reconcile` over anything carrying `cost_usd` directly or on `.completion`."""
    total = 0.0
    for r in records:
        cost = getattr(r, "cost_usd", None)
        if cost is None:
            cost = r.completion.cost_usd
        total += float(cost)
    return reconcile(total, provider_usd, tolerance)


def estimate_for_config(
    prices: dict[str, Price],
    config: RunConfig,
    n_cases: int,
    assumptions: TokenAssumptions | None = None,
    batch_judges: bool = True,
    citation_judges: bool = False,
) -> CostEstimate:
    """The ADP-05 entry point: estimate a run straight from its RunConfig (model ids)."""
    return estimate_run_cost(
        prices,
        models=config.models,
        extractor=config.extractor,
        judges=config.judges,
        n_cases=n_cases,
        assumptions=assumptions,
        batch_judges=batch_judges,
        free=frozenset(config.endpoints) | frozenset(config.systems),
        citation_judges=citation_judges,
    )
