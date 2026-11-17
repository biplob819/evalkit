"""Prices, per-call cost, pre-run estimates and post-run reconciliation (ADP-04, ADP-05)."""

from __future__ import annotations

import contextvars
import math
import re
import warnings
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

import yaml
from pydantic import Field

from evalkit.schemas import RunConfig
from evalkit.schemas._base import Model, Name, NonNegFloat, NonNegInt

# Public since 0.3.0: Price, call_cost, estimate_tokens, load_prices. Added for the claims per
# item behind an estimate (fraud-review-bedrock, D-61 rule 3): ClaimsAssumption,
# claims_from_texts, measured_claims.
__all__ = [
    "ClaimsAssumption",
    "Price",
    "call_cost",
    "claims_from_texts",
    "estimate_tokens",
    "load_prices",
    "measured_claims",
]

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


# Claims per item. Judges score every claim, so judge cost is planned per claim (D-46). Before
# any text exists the planning default applies (4.5, measured on PubMedQA benchmark answers).
# When the texts to be judged already exist (faithfulness on given explanations, re-judging
# saved answers) their length says more: fraud-review-bedrock's explanations hold ~12 claims
# each, so 4.5 planned its judging at well under half the real cost (D-61 rule 3).
#
# WORDS_PER_CLAIM is measured with `count_words` on EvalKit's own committed extraction output
# (scripts/measure_words_per_claim.py, docs/measurements/words-per-claim-2026-11-17.json):
# the published 200-case run's failure explorer (docs/results/report.html, run r-e8dac378d2d3,
# 553 answers with claims) has 11.9 words per claim (per-answer quartiles 10.2 / 12.0 / 14.5),
# and the demo replay (demo/replay.json, 24 answers) 12.2. Planning uses 10, below the lower
# quartile, so a text plans for more claims than a typical answer of its length holds.
WORDS_PER_CLAIM = 10.0
PLANNING_CLAIMS_SOURCE = "planning default 4.5, PubMedQA answers (D-46, measured 4.46)"
NOT_AN_UPPER_BOUND = "an expected count, not an upper bound"

# A run of non-space characters is one word; every CJK ideograph or kana is a word of its own,
# since those scripts do not separate words with spaces.
_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿"
_WORD_RE = re.compile(f"[{_CJK}]|[^\\s{_CJK}]+")


def count_words(text: str) -> int:
    """Whitespace-separated words, with each CJK ideograph or kana counted as one word."""
    return len(_WORD_RE.findall(text))


class ClaimsAssumption(Model):
    """How many claims per item an estimate plans to judge, and where that number comes from.

    `basis` is "default" (the planning default), "override" (`TokenAssumptions` set by the
    caller), "measured" (an explicit figure from a sample the caller ran) or "texts" (counted
    from the texts at `words_per_claim`, each item at least the planning default). Every basis
    is an expected count, never an upper bound: a dense text can hold more claims."""

    basis: Literal["default", "override", "measured", "texts"]
    per_item: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    source: str
    # basis "texts" only: the summed per-item counts (judge calls are ceil(total)), the texts
    # and words counted, the ratio used, and how many short texts were raised to the default.
    total: NonNegFloat | None = None
    items: NonNegInt | None = None
    words: NonNegInt | None = None
    words_per_claim: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    raised: NonNegInt | None = None

    _omit_if_default: ClassVar[tuple[str, ...]] = (
        "total",
        "items",
        "words",
        "words_per_claim",
        "raised",
    )

    def calls(self, n_items: int) -> int:
        """Claims to judge over `n_items` items (one judge call each, per judge)."""
        if self.total is None:
            return math.ceil(n_items * self.per_item)
        if n_items != self.items:
            raise ValueError(f"claims were counted from {self.items} texts, not {n_items}")
        return math.ceil(round(self.total, 9))

    def describe(self) -> str:
        return f"claims per item: {self.source}; {NOT_AN_UPPER_BOUND}"


PLANNING_CLAIMS = ClaimsAssumption(
    basis="default",
    per_item=TokenAssumptions().claims_per_answer,
    source=PLANNING_CLAIMS_SOURCE,
)


def planning_claims(assumptions: TokenAssumptions | None = None) -> ClaimsAssumption:
    """The claims assumption before any text exists: the planning default, or the caller's
    `TokenAssumptions.claims_per_answer` when it differs."""
    per = (assumptions or TokenAssumptions()).claims_per_answer
    if per == PLANNING_CLAIMS.per_item:
        return PLANNING_CLAIMS
    return ClaimsAssumption(
        basis="override",
        per_item=per,
        source=f"TokenAssumptions.claims_per_answer {per:g}, set by the caller",
    )


def measured_claims(per_item: float, source: str = "a measured sample") -> ClaimsAssumption:
    """An explicit claims-per-item figure measured on a small paid sample of the same kind of
    text (e.g. the mean `FaithfulnessResult.n_claims` over 20 items; docs/cost.md). It is used
    as given, for every item."""
    return ClaimsAssumption(
        basis="measured",
        per_item=per_item,
        source=f"measured override {per_item:g} claims per item ({source})",
    )


def claims_in_text(
    text: str, *, default: float | None = None, words_per_claim: float = WORDS_PER_CLAIM
) -> float:
    """Planned claims for one text: its words at `words_per_claim`, at least `default`."""
    floor = PLANNING_CLAIMS.per_item if default is None else default
    return max(count_words(text) / words_per_claim, floor)


def claims_from_texts(
    texts: Sequence[str],
    *,
    default: float | None = None,
    words_per_claim: float = WORDS_PER_CLAIM,
) -> ClaimsAssumption:
    """Claims counted from texts that already exist, with no model call: per text, the larger
    of its words / `words_per_claim` and the planning default (`default`), summed."""
    if not texts:
        raise ValueError("no texts to count claims in")
    if not words_per_claim > 0:
        raise ValueError("words_per_claim must be positive")
    floor = PLANNING_CLAIMS.per_item if default is None else default
    words = [count_words(t) for t in texts]
    total = math.fsum(max(w / words_per_claim, floor) for w in words)
    raised = sum(1 for w in words if w / words_per_claim < floor)
    return ClaimsAssumption(
        basis="texts",
        per_item=total / len(texts),
        total=total,
        items=len(texts),
        words=sum(words),
        words_per_claim=words_per_claim,
        raised=raised,
        source=(
            f"from the texts: {sum(words):,} words → {total:.1f} claims in {len(texts)} items "
            f"at {words_per_claim:g} words/claim (each item at least {floor:g}; "
            f"{raised} raised to it)"
        ),
    )


class CostLine(Model):
    node: Name
    model: Name
    calls: NonNegInt
    usd: NonNegFloat


class CostEstimate(Model):
    lines: tuple[CostLine, ...]
    total_usd: NonNegFloat
    # The claims-per-item assumption behind the judge lines. Left out of the JSON while it is
    # the planning default, so benchmark estimates keep their exact bytes.
    claims: ClaimsAssumption = PLANNING_CLAIMS

    _omit_if_default: ClassVar[tuple[str, ...]] = ("claims",)

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
    claims: ClaimsAssumption | None = None,
) -> CostEstimate:
    """generation + extraction + judging (batch unless `batch_judges=False`), per node and
    model (Tech Spec §9). Models in `free` (local endpoints) cost 0 unless priced.
    `citation_judges` (B5.12, tasks listing `citation_support`) adds one citation-judge call
    per claim per judge. `claims` sets the claims per answer (`measured_claims`, or
    `claims_from_texts` over the saved answers when re-judging them); without it, the planning
    default (`assumptions.claims_per_answer`). The estimate records which in `.claims`."""
    a = assumptions or TokenAssumptions()
    c = claims or planning_claims(a)

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
    n_claims = c.calls(answers)  # judges score each claim
    for j in judges:
        per = call_cost(
            pf(j),
            tokens_in=a.t_j_in,
            tokens_out=a.t_j_out,
            tokens_reasoning=a.t_j_reason,
            batch=batch_judges,
        )
        lines.append(CostLine(node="judge", model=j, calls=n_claims, usd=per * n_claims))
    if citation_judges:
        for j in judges:
            per = call_cost(
                pf(j),
                tokens_in=a.t_j_in,
                tokens_out=CITATION_T_OUT,
                tokens_reasoning=a.t_j_reason,
                batch=batch_judges,
            )
            # at most one per claim: B3 skips some
            lines.append(
                CostLine(node="citation_judge", model=j, calls=n_claims, usd=per * n_claims)
            )
    return CostEstimate(lines=tuple(lines), total_usd=sum(line.usd for line in lines), claims=c)


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
    claims: ClaimsAssumption | None = None,
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
        claims=claims,
    )
