"""Prices, per-call cost, pre-run estimates and post-run reconciliation (ADP-04, ADP-05)."""

from __future__ import annotations

import contextvars
import math
import re
import unicodedata
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


# The most claims per item an override or default may name (an ~80,000-word text at the
# planning ratio). Larger figures are typos, such as 1e308, not plans.
MAX_CLAIMS_PER_ITEM = 10_000.0


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
    claims_per_answer: Annotated[
        float, Field(gt=0, le=MAX_CLAIMS_PER_ITEM, allow_inf_nan=False)
    ] = 4.5  # measured 4.46


# B5.12: a citation reply lists every cited sentence (about 25 tokens each, ~3 per claim)
# instead of one verdict, so its planned output is larger than a claim verdict's.
CITATION_T_OUT = 120


# Claims per item. Judges score every claim, so judge cost is planned per claim (D-46). Before
# any text exists the planning default applies (4.5, measured on PubMedQA benchmark answers).
# When the texts to be judged already exist (faithfulness on given explanations, re-judging
# saved answers) their content says more: fraud-review-bedrock's explanations hold ~12 claims
# each, so 4.5 planned its judging at well under half the real cost (D-61 rule 3).
#
# Each text is planned at the largest of several cheap lower bounds, none of which calls a
# model, because a quote that is too low defeats the cost cap while one that is too high only
# asks for more headroom (D-76):
#   - words ÷ WORDS_PER_CLAIM;
#   - sentence-like units: pieces between sentence ends, line breaks, list markers and
#     semicolons (and, in scripts written without spaces between words, such as Thai, the
#     spaces, which separate phrases there);
#   - key=value fields (`amount=420,country=NG`, or `"amount": 420`);
#   - distinct numbers;
#   - non-space characters (after NFKC) ÷ CHARS_PER_CLAIM, for scripts without spaces;
#   - the planning default.
#
# Both ratios are measured on EvalKit's own committed extraction output
# (scripts/measure_words_per_claim.py, docs/measurements/words-per-claim-2026-11-17.json):
# the published 200-case run's failure explorer (docs/results/report.html, run r-e8dac378d2d3,
# 553 answers with claims) has 11.9 words and 69.6 characters per claim. Planning uses the
# 10th percentile of its per-answer ratios, rounded down (8.67 → 8 words, 50.15 → 50
# characters). Under the whole rule 4.2% of those answers are planned below their true count
# (18.6% under the first rule, words ÷ 10), at 1.53 times the true total, and the
# fraud-review-shaped fixture (8.8 words per claim) is planned at 106 claims against 96.
WORDS_PER_CLAIM = 8.0
CHARS_PER_CLAIM = 50.0
PLANNING_CLAIMS_SOURCE = "planning default 4.5, PubMedQA answers (D-46, measured 4.46)"
NOT_AN_UPPER_BOUND = "an expected count, not an upper bound"

# A run of non-space characters is one word; every CJK ideograph or kana is a word of its own,
# since those scripts do not separate words with spaces.
_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿"
_WORD_RE = re.compile(f"[{_CJK}]|[^\\s{_CJK}]+")
# Scripts that write words without spaces and use a space between phrases or sentences:
# Thai, Lao, Tibetan, Myanmar, Khmer (and their extension blocks).
_NO_SPACE = "".join(
    f"{chr(lo)}-{chr(hi)}"
    for lo, hi in (
        (0x0E00, 0x0EFF),  # Thai, Lao
        (0x0F00, 0x0FFF),  # Tibetan
        (0x1000, 0x109F),  # Myanmar
        (0x1780, 0x17FF),  # Khmer
        (0x19E0, 0x19FF),  # Khmer symbols
        (0xA9E0, 0xA9FF),  # Myanmar extended-B
        (0xAA60, 0xAA7F),  # Myanmar extended-A
    )
)
_UNIT_BREAK = re.compile(
    r"[.!?。！？]+[\"'”’)\]]*(?=\s|$)"  # sentence ends
    r"|[;；\n•▪]"  # semicolons, line breaks, bullets
    r"|^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]"  # list markers at the start of a line
    rf"|(?<=[{_NO_SPACE}])\s+(?=[{_NO_SPACE}])",  # phrase breaks in scripts without spaces
    re.M,
)
_HAS_WORD = re.compile(r"\w")
_FIELD_RE = re.compile(r"[^\W\d_][\w.-]*[\"']?[ \t]*[=:][ \t]*[^\s,;=:/]")
_NUMBER_RE = re.compile(r"(?<![^\W\d_])\d+(?:[.,:]\d+)*")  # not inside a name: Q3, HbA1c


def count_words(text: str) -> int:
    """Whitespace-separated words, with each CJK ideograph or kana counted as one word."""
    return len(_WORD_RE.findall(text))


def count_chars(text: str) -> int:
    """Non-space characters after NFKC normalization."""
    return sum(1 for ch in unicodedata.normalize("NFKC", text) if not ch.isspace())


def claim_bounds(
    text: str,
    *,
    words_per_claim: float = WORDS_PER_CLAIM,
    chars_per_claim: float = CHARS_PER_CLAIM,
) -> dict[str, float]:
    """The cheap lower bounds on the claims in one text (no model call), by name: `words`
    (words ÷ `words_per_claim`), `units` (sentence-like units), `fields` (key=value pairs),
    `numbers` (distinct numbers) and `chars` (non-space characters ÷ `chars_per_claim`)."""
    norm = unicodedata.normalize("NFKC", text)
    return {
        "words": count_words(text) / words_per_claim,
        "units": float(sum(1 for s in _UNIT_BREAK.split(norm) if _HAS_WORD.search(s))),
        "fields": float(len(_FIELD_RE.findall(norm))),
        "numbers": float(len(set(_NUMBER_RE.findall(norm)))),
        "chars": count_chars(text) / chars_per_claim,
    }


def _check_per_item(value: float, what: str) -> float:
    """A claims-per-item figure must be a finite number above 0 and at most
    `MAX_CLAIMS_PER_ITEM`; anything else is a mistake, not a plan."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{what} must be a number, not {value!r}")
    if not (math.isfinite(value) and 0 < value <= MAX_CLAIMS_PER_ITEM):
        raise ValueError(
            f"{what} must be a finite number above 0 and at most {MAX_CLAIMS_PER_ITEM:,g} "
            f"claims per item, not {value!r}"
        )
    return float(value)


def _check_ratio(value: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{what} must be a number, not {value!r}")
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{what} must be a finite number above 0, not {value!r}")
    return float(value)


class ClaimsAssumption(Model):
    """How many claims per item an estimate plans to judge, and where that number comes from.

    `basis` is "default" (the planning default), "override" (`TokenAssumptions` set by the
    caller), "measured" (an explicit figure from a sample the caller ran) or "texts" (counted
    from the texts: each item at the largest of its lower bounds, `claim_bounds`, and the
    planning default). Every basis is an expected count, never an upper bound."""

    basis: Literal["default", "override", "measured", "texts"]
    per_item: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    source: str
    # basis "texts" only: the summed per-item counts (judge calls are ceil(total)), the texts
    # and words counted, the ratios used, and how many texts were planned above their words ÷
    # words_per_claim (by a sentence, field, number or character count, or the default).
    total: NonNegFloat | None = None
    items: NonNegInt | None = None
    words: NonNegInt | None = None
    words_per_claim: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    chars_per_claim: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    raised: NonNegInt | None = None

    _omit_if_default: ClassVar[tuple[str, ...]] = (
        "total",
        "items",
        "words",
        "words_per_claim",
        "chars_per_claim",
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
    as given, for every item, and must be finite, above 0 and at most `MAX_CLAIMS_PER_ITEM`."""
    per_item = _check_per_item(per_item, "a measured claims per item")
    return ClaimsAssumption(
        basis="measured",
        per_item=per_item,
        source=f"measured override {per_item:g} claims per item ({source})",
    )


def claims_in_text(
    text: str,
    *,
    default: float | None = None,
    words_per_claim: float = WORDS_PER_CLAIM,
    chars_per_claim: float = CHARS_PER_CLAIM,
) -> float:
    """Planned claims for one text: the largest of its `claim_bounds` and `default` (the
    planning default 4.5 when not given)."""
    floor = PLANNING_CLAIMS.per_item if default is None else _check_per_item(default, "default")
    bounds = claim_bounds(
        text,
        words_per_claim=_check_ratio(words_per_claim, "words_per_claim"),
        chars_per_claim=_check_ratio(chars_per_claim, "chars_per_claim"),
    )
    return max(*bounds.values(), floor)


def claims_from_texts(
    texts: Sequence[str],
    *,
    default: float | None = None,
    words_per_claim: float = WORDS_PER_CLAIM,
    chars_per_claim: float = CHARS_PER_CLAIM,
) -> ClaimsAssumption:
    """Claims counted from texts that already exist, with no model call: per text, the largest
    of its lower bounds (`claim_bounds`: words ÷ `words_per_claim`, sentence-like units,
    key=value fields, distinct numbers, characters ÷ `chars_per_claim`) and the planning
    default (`default`), summed over the texts."""
    if not texts:
        raise ValueError("no texts to count claims in")
    floor = PLANNING_CLAIMS.per_item if default is None else _check_per_item(default, "default")
    wpc = _check_ratio(words_per_claim, "words_per_claim")
    cpc = _check_ratio(chars_per_claim, "chars_per_claim")
    words = [count_words(t) for t in texts]
    planned: list[float] = []
    by: dict[str, int] = {}
    for t in texts:
        bounds = claim_bounds(t, words_per_claim=wpc, chars_per_claim=cpc)
        bounds["default"] = floor
        top = max(bounds.values())
        planned.append(top)
        if top > bounds["words"]:
            # the first bound that reached the plan, in a fixed order
            name = next(k for k in _BOUND_ORDER if bounds[k] == top)
            by[name] = by.get(name, 0) + 1
    total = math.fsum(planned)
    raised = sum(by.values())
    detail = ", ".join(f"{n} by {_BOUND_ORDER[k]}" for k in _BOUND_ORDER if (n := by.get(k)))
    return ClaimsAssumption(
        basis="texts",
        per_item=total / len(texts),
        total=total,
        items=len(texts),
        words=sum(words),
        words_per_claim=wpc,
        chars_per_claim=cpc,
        raised=raised,
        source=(
            f"from the texts: {total:.1f} claims in {len(texts)} items ({sum(words):,} words), "
            f"each item at the largest of words ÷ {wpc:g}, sentence-like units, key=value "
            f"fields, distinct numbers, characters ÷ {cpc:g} and {floor:g}; "
            f"{raised} raised above words ÷ {wpc:g}" + (f" ({detail})" if detail else "")
        ),
    )


# The bounds that can raise a text above its words ÷ ratio, in the order a tie is credited.
_BOUND_ORDER = {
    "default": "the default",
    "units": "sentence-like units",
    "fields": "key=value fields",
    "numbers": "distinct numbers",
    "chars": "characters",
}


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
