"""The metrics-summary format `metrics-1` (B5.3, GAT-03, GAT-05, EFF-01; frozen 2026-11-17).

A small JSON file any project can write, with or without `evalkit run`, and gate a release on
with `evalkit gate` (specs/B5.3-gates/spec.md §1). Validation is strict: required fields, unit
domains, integer counts, [0, 1] rates, booleans, upper-case unit codes, k <= n, intervals with
their method, unique (name, labels), and unknown keys refused. Numbers are checked as parsed
JSON numbers: an integer stays an integer, so `1.0` is not a count.

`from_run_summary` converts EvalKit's own `summary.json` (and, when given, the run's records
for the efficiency block, spec §1b) to the same shape.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from evalkit.schemas import Interval, Record, RunSummary
    from evalkit.schemas.summary import Efficiency
    from evalkit.stats import Resamples

__all__ = [
    "EFFICIENCY_NAMES",
    "SCHEMA",
    "Metric",
    "MetricsError",
    "MetricsSummary",
    "Provenance",
    "efficiency_metrics",
    "from_run_summary",
    "load_metrics",
    "parse_metrics",
    "safe_text",
]

SCHEMA = "metrics-1"
# \Z, not $: "$" also matches before a trailing newline, so "x\n" would pass.
NAME_RE = re.compile(r"^[a-z][a-z0-9_.]*\Z")
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*\Z")  # USD, NTD, EUR, ...: an upper-case unit code
FIXED_UNITS = ("count", "rate", "bool", "ratio", "ms", "s")
MAX_SHOWN = 64  # free-text label values longer than this are never echoed (GAT-04)
MAX_INT_DIGITS = 4300  # Python's int/str conversion limit: a longer JSON integer is refused
# A rate's value must equal k / n: within 1e-6 relative, or 5e-7 absolute (half a unit in the
# sixth decimal, so a value rounded to 6 decimals, as assay-pipeline writes, always passes).
RATE_REL_TOL, RATE_ABS_TOL = 1e-6, 5e-7 + 1e-12  # + float slack: 3/128 as 0.023438 is 5e-7 off

# Spec §1b (EFF-01, D-67): standard efficiency names and the unit each must carry. "currency" is
# any upper-case code; latency is in ms (s accepted too, GAT-10).
EFFICIENCY_NAMES: dict[str, str] = {
    "tokens_in_per_case": "count",
    "tokens_out_per_case": "count",
    "tokens_reasoning_per_case": "count",
    "tokens_cached_per_case": "count",
    "cache_hit_share": "rate",
    "cost_per_case": "currency",
    "cost_per_correct": "currency",
    "latency_p50": "ms",
    "latency_p95": "ms",
    "latency_p99": "ms",
    "retry_rate": "rate",
    "error_rate": "rate",
    "eval_overhead_cost": "currency",
    "system_cost": "currency",
}
# The token names are means of counts per case (spec §1b: "unit count, means with n"), so they
# may be fractional; every other count is an integer.
COUNT_MEANS = frozenset(n for n, u in EFFICIENCY_NAMES.items() if n.startswith("tokens_"))
EFFICIENCY_MODES = ("batch", "live")

# "No case or member ids anywhere in the file" (spec §1): label keys that name an id are refused.
_ID_KEYS = frozenset({"id", "ids", "case", "cases", "member", "members", "msno", "item", "row"})

Number = StrictInt | StrictFloat
LabelValue = StrictStr | StrictInt | StrictBool


def safe_text(value: object) -> str:
    """A label value (or key) as gate output may show it: never more than 64 characters."""
    text = value if isinstance(value, str) else json.dumps(value)
    if len(text) > MAX_SHOWN:
        return f"<{len(text)} characters, not shown>"
    return text


def _is_num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _finite(v: int | float) -> bool:
    return isinstance(v, int) or math.isfinite(v)


def unit_kind(unit: str) -> str:
    return unit if unit in FIXED_UNITS else "currency"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class Provenance(_Strict):
    code_version: StrictStr | None = None
    snapshot: StrictStr | None = None
    config_hash: StrictStr | None = None
    evalkit: StrictStr | None = None


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)
_HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")
_B64_RE = re.compile(r"[A-Za-z0-9+/_-]{20,}={0,2}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def _id_shaped(value: str) -> bool:
    """A label value that looks like a case or member id (respectful-offers' D-53 case: a
    44-character base64 member id). Model ids, category codes and years are not id-shaped:
    a base64-looking token counts only when it mixes upper and lower case and digits."""
    t = value.strip()
    if _UUID_RE.fullmatch(t) or _HEX_RE.fullmatch(t) or _EMAIL_RE.fullmatch(t):
        return True
    if _B64_RE.fullmatch(t):
        return (
            any(c.isupper() for c in t)
            and any(c.islower() for c in t)
            and any(c.isdigit() for c in t)
        )
    return False


class Metric(_Strict):
    """One named figure. `value` keeps its JSON type: an int stays an int."""

    name: StrictStr
    unit: StrictStr
    value: StrictBool | StrictInt | StrictFloat
    n: StrictInt | None = None
    k: StrictInt | None = None
    ci_low: Number | None = None
    ci_high: Number | None = None
    ci_method: StrictStr | None = None
    labels: dict[str, LabelValue] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must match [a-z][a-z0-9_.]*")
        return v

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        if v not in FIXED_UNITS and not CODE_RE.match(v):
            raise ValueError(
                "unit must be count, rate, bool, ratio, ms, s, or an upper-case code such as USD"
            )
        return v

    @field_validator("labels")
    @classmethod
    def _labels(cls, v: dict[str, Any]) -> dict[str, Any]:
        for key in v:
            low = key.lower()
            if (
                low in _ID_KEYS
                or low.endswith(("_id", "_ids", "uuid", "uuids"))
                or re.search(r"[a-z0-9](Id|Ids|UUID|Uuid)$", key)  # camelCase: userId, caseUUID
            ):
                raise ValueError(
                    f"label {safe_text(key)!r} names an id; case and member ids are not allowed "
                    "in metrics-1 (aggregates only)"
                )
            if not key.strip():
                raise ValueError("label keys must not be blank")
            val = v[key]
            if isinstance(val, str) and _id_shaped(val):
                raise ValueError(
                    f"label {safe_text(key)!r} has a value shaped like an id (a uuid, a long hex "
                    "or base64 token, or an email); case and member ids are not allowed in "
                    "metrics-1 (aggregates only)"
                )
        return v

    @model_validator(mode="after")
    def _domain(self) -> Metric:
        kind, v = unit_kind(self.unit), self.value
        if kind == "bool":
            if not isinstance(v, bool):
                raise ValueError("a bool metric's value must be true or false")
        elif isinstance(v, bool):
            raise ValueError(f"a {self.unit} metric's value must be a number, not true/false")
        elif not _finite(v):
            raise ValueError("value must be finite")
        elif kind == "count":
            if self.name in COUNT_MEANS:
                if v < 0:
                    raise ValueError("a count mean must be >= 0")
            elif not isinstance(v, int) or v < 0:
                raise ValueError("a count must be a JSON integer >= 0 (1.0 is not a count)")
        elif kind == "rate" and not 0 <= v <= 1:
            raise ValueError("a rate must be in [0, 1]")
        if self.n is not None and self.n < 0:
            raise ValueError("n must be an integer >= 0")
        if self.k is not None:
            if kind != "rate":
                raise ValueError("k is only allowed on a rate")
            if self.n is None:
                raise ValueError("k needs n")
            if not 0 <= self.k <= self.n:
                raise ValueError("k must satisfy 0 <= k <= n")
            # the absolute slack never reaches half of one count, so a value of 0 with k=1 is
            # refused however large n is (B5.3 verifier pass 2)
            abs_tol = min(RATE_ABS_TOL, 0.25 / self.n) if self.n else RATE_ABS_TOL
            if self.n and not math.isclose(
                v, self.k / self.n, rel_tol=RATE_REL_TOL, abs_tol=abs_tol
            ):
                raise ValueError("a rate with k and n must have value k / n (within 1e-6)")
        lo, hi = self.ci_low, self.ci_high
        if (lo is None) != (hi is None):
            raise ValueError("ci_low and ci_high are given together or not at all")
        if lo is not None and hi is not None:
            if kind == "bool":
                raise ValueError("a bool metric has no interval")
            if not (_finite(lo) and _finite(hi)):
                raise ValueError("interval bounds must be finite")
            if not lo <= v <= hi:  # type: ignore[operator]
                raise ValueError("the interval must satisfy ci_low <= value <= ci_high")
            if kind == "rate" and not 0 <= lo <= hi <= 1:
                raise ValueError("a rate's interval must lie in [0, 1]")
            if kind == "count" and lo < 0:
                raise ValueError("a count's ci_low must be >= 0")
            if self.ci_method is None or not self.ci_method.strip():
                raise ValueError("ci_method is required when an interval is given")
        want = EFFICIENCY_NAMES.get(self.name)
        if want is not None:
            ok = kind == want or (want == "ms" and kind == "s")
            if not ok:
                raise ValueError(f"{self.name} is a standard efficiency name with unit {want}")
        mode = self.labels.get("mode")
        if want is not None and mode is not None and mode not in EFFICIENCY_MODES:
            raise ValueError("the efficiency label mode must be batch or live")
        return self

    def label_key(self) -> tuple[tuple[str, str, Any], ...]:
        # typed, so the label value 1 and "1" (and true) stay distinct
        return tuple(sorted((k, type(v).__name__, v) for k, v in self.labels.items()))


def _timestamp(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError("created_at must be an ISO 8601 string with a zone")
    try:
        parsed = datetime.fromisoformat(v)
    except ValueError:
        raise ValueError("created_at must be an ISO 8601 timestamp with a zone") from None
    if parsed.tzinfo is None:
        raise ValueError("created_at must carry a zone (e.g. Z or +05:30)")
    return v


class MetricsSummary(_Strict):
    """A `metrics-1` file. Unknown top-level keys are refused (a typo must not pass)."""

    schema_version: StrictStr
    project: Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]
    run_id: Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]
    created_at: StrictStr
    simulated: StrictBool = False
    provenance: Provenance = Field(default_factory=Provenance)
    metrics: list[Metric] = Field(default_factory=list)  # strict JSON: an array

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: str) -> str:
        if v != SCHEMA:
            raise ValueError(f"schema_version must be exactly {SCHEMA!r}")
        return v

    @field_validator("created_at")
    @classmethod
    def _created(cls, v: str) -> str:
        return _timestamp(v)

    @field_validator("provenance", mode="before")
    @classmethod
    def _null_provenance(cls, v: Any) -> Any:
        return {} if v is None else v  # `"provenance": null` means none given

    @model_validator(mode="after")
    def _unique(self) -> MetricsSummary:
        seen: dict[tuple[Any, ...], int] = {}
        for i, m in enumerate(self.metrics):
            key = (m.name, m.label_key())
            if key in seen:
                raise ValueError(
                    f"metrics[{i}] repeats metrics[{seen[key]}]: (name, labels) must be unique"
                )
            seen[key] = i
        return self


class MetricsError(ValueError):
    """A metrics-1 file that cannot be read. `problems` never echo file values."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


def describe_validation(exc: ValidationError, prefix: str = "") -> list[str]:
    """Pydantic errors as `where: what`, without the offending input (it may be held-out text
    or a long free-text value)."""
    out = []
    for e in exc.errors(include_input=False, include_url=False):
        where = ".".join(
            f"[{p}]" if isinstance(p, int) else safe_text(str(p)) for p in e["loc"]
        ).replace(".[", "[")
        msg = str(e["msg"]).removeprefix("Value error, ")
        out.append(f"{prefix}{where}: {msg}" if where else f"{prefix}{msg}")
    return out


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise MetricsError([f"duplicate key {safe_text(k)!r} in a JSON object"])
        out[k] = v
    return out


def _no_constants(name: str) -> Any:
    raise MetricsError([f"{name} is not a JSON number"])


def _bounded_int(text: str) -> int:
    digits = len(text.lstrip("-"))
    if digits > MAX_INT_DIGITS:
        raise MetricsError(
            [f"an integer of {digits} digits is too large (at most {MAX_INT_DIGITS})"]
        )
    return int(text)


def loads_json(text: str) -> Any:
    """Strict JSON: duplicate keys and NaN/Infinity refused; ints stay ints; integers over
    4300 digits and nesting deeper than Python's recursion limit are refused with a message."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constants,
            parse_int=_bounded_int,
        )
    except json.JSONDecodeError as exc:
        raise MetricsError([f"not valid JSON (line {exc.lineno}, column {exc.colno})"]) from None
    except RecursionError:
        raise MetricsError(["JSON nested too deeply"]) from None


def validate_metrics(data: Any) -> MetricsSummary:
    if not isinstance(data, dict):
        raise MetricsError(["a metrics-1 file is a JSON object"])
    try:
        return MetricsSummary.model_validate(data)
    except ValidationError as exc:
        raise MetricsError(describe_validation(exc)) from None


def parse_metrics(text: str) -> MetricsSummary:
    """Validate a metrics-1 JSON document; raises MetricsError (safe to print)."""
    return validate_metrics(loads_json(text))


def load_metrics(path: Any) -> MetricsSummary:
    from pathlib import Path

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MetricsError([f"cannot read {path}: {type(exc).__name__}"]) from None
    return parse_metrics(text)


# ---- EvalKit summary.json -> metrics-1 (spec §1, last rule) ------------------------------------

BOOT_METHOD = "percentile bootstrap over cases (seeded)"


def _iv_metric(
    name: str,
    unit: str,
    iv: Any,
    n: int,
    labels: Mapping[str, Any],
    *,
    with_k: bool = False,
) -> Metric:
    k = None
    if with_k:  # a mean of 0/1 per case: k is exact
        k = round(iv.point * n)
    return Metric(
        name=name,
        unit=unit,
        value=iv.point,
        n=n,
        k=k,
        ci_low=iv.lo,
        ci_high=iv.hi,
        ci_method=BOOT_METHOD,
        labels=dict(labels),
    )


def _row_metrics(row: Any, labels: Mapping[str, Any], *, model_level: bool) -> list[Metric]:
    """The figures one ModelSummary or SliceSummary carries, as metrics."""
    n = row.n
    out = [
        _iv_metric("accuracy", "rate", row.accuracy, n, labels, with_k=True),
        _iv_metric("citation_validity", "rate", row.citation_validity, n, labels),
        _iv_metric("coverage", "rate", row.coverage, n, labels),
    ]
    for judge, iv in row.headline.items():
        out.append(_iv_metric("headline", "rate", iv, n, {**labels, "judge": judge}, with_k=True))
    for judge, iv in row.groundedness_mean.items():
        out.append(_iv_metric("groundedness", "rate", iv, n, {**labels, "judge": judge}))
    for judge, iv in row.citation_precision.items():
        out.append(_iv_metric("citation_precision", "rate", iv, n, {**labels, "judge": judge}))
    for judge, iv in row.aim_citation_rate.items():
        out.append(_iv_metric("aim_citation_rate", "rate", iv, n, {**labels, "judge": judge}))
    for scorer, iv in row.task_scores.items():
        name = f"task_scores.{scorer}"
        if NAME_RE.match(name):
            out.append(_iv_metric(name, "ratio", iv, n, labels))
    errors = sum(row.n_errors.values())
    out.append(
        Metric(
            name="error_rate",
            unit="rate",
            value=errors / n if n else 0.0,
            n=n,
            k=errors,
            labels=dict(labels),  # the summary records no run mode: none is stated here
        )
    )
    out.append(
        Metric(
            name="cost_per_100_usd",
            unit="USD",
            value=row.cost_per_100_usd,
            n=n,
            labels=dict(labels),
        )
    )
    if row.latency_p50_ms is not None and row.latency_p95_ms is not None:
        out.append(
            Metric(name="latency_p50_ms", unit="ms", value=row.latency_p50_ms, labels=dict(labels))
        )
        out.append(
            Metric(name="latency_p95_ms", unit="ms", value=row.latency_p95_ms, labels=dict(labels))
        )
    if model_level:
        if row.cost_per_correct_usd is not None:
            out.append(
                Metric(
                    name="cost_per_correct_usd",
                    unit="USD",
                    value=row.cost_per_correct_usd,
                    labels=dict(labels),
                )
            )
        out.append(
            Metric(
                name="passes_threshold",
                unit="bool",
                value=row.passes_threshold,
                labels=dict(labels),
            )
        )
    return out


def _slice_label(field: str) -> str:
    return f"slice.{field}" if field in ("model", "judge", "mode") else field


def from_run_summary(
    summary: RunSummary, records: Sequence[Record] | None = None, *, n_resamples: int = 10_000
) -> MetricsSummary:
    """EvalKit's `summary.json` as metrics-1: per-model metrics labelled `model` (and `judge`
    where per judge), slices as extra labels, the v0.2 threshold verdict as the bool
    `passes_threshold`. The efficiency block (spec §1b) comes from each model's `efficiency`
    in the summary (0.3, D-70), so the gate sees exactly what summary.json says; a summary
    written before 0.3 has none, and then, when the run's records are given, it is computed
    from them with the same seeded resamples. Intervals from the summary are carried as
    computed."""
    metrics: list[Metric] = []
    for m in summary.models:
        metrics += _row_metrics(m, {"model": m.model}, model_level=True)
    for s in summary.slices:
        metrics += _row_metrics(
            s, {"model": s.model, _slice_label(s.field): s.value}, model_level=False
        )
    efficiency: list[Metric] = []
    for m in summary.models:
        if m.efficiency is not None:
            efficiency += _efficiency_as_metrics(m.model, m.efficiency)
    if not efficiency and records:
        efficiency = efficiency_metrics(records, n_resamples=n_resamples)
    if efficiency:
        # the efficiency block carries the model-level error rate (same k and n): keep one
        metrics = [
            x for x in metrics if not (x.name == "error_rate" and set(x.labels) == {"model"})
        ]
        metrics += efficiency
    created = summary.finished_at or summary.started_at
    return MetricsSummary(
        schema_version=SCHEMA,
        project=summary.task,
        run_id=summary.run_id,
        created_at=created.isoformat(),
        simulated=False,
        provenance=Provenance(config_hash=summary.config_hash),
        metrics=metrics,
    )


# ---- efficiency block (spec §1b, EFF-02, OWNER D-70) ------------------------------------------


def _latency_intervals(
    lat: Sequence[int | None], rows: Sequence[Sequence[int]]
) -> dict[float, Interval]:
    """Nearest-rank p50/p95/p99 over calls that reached the provider, each with a percentile
    bootstrap interval from the same case resamples."""
    from evalkit.stats import interval_from, percentile_ms

    ok = [x for x in lat if x is not None]
    qs = (0.50, 0.95, 0.99)
    boots: dict[float, list[float]] = {q: [] for q in qs}
    for row in rows:
        xs = sorted(x for i in row if (x := lat[i]) is not None)
        if not xs:
            continue
        for q in qs:
            boots[q].append(float(xs[max(1, math.ceil(q * len(xs))) - 1]))
    out = {}
    for q in qs:
        point = percentile_ms(ok, q)
        out[q] = interval_from(float(point), boots[q] or [float(point)])
    return out


def efficiency_block(
    rows: Sequence[Record],
    resamples: Resamples,
    *,
    price_date: str | None = None,
    eval_overhead_usd: float | None = None,
) -> Efficiency:
    """One model's efficiency block (EFF-02, D-70) from its records in resample order (cases
    sorted by id, as `aggregate` draws them): token means per case, cache-hit share, error
    rate with its count, generation (system) cost, cost per case and per correct answer and
    latency p50/p95/p99, the last five with seeded 95% bootstrap intervals over cases. Mode is
    `live` (generation calls are not batched). Retries are not recorded per call, so
    `retry_rate` is None; `eval_overhead_usd` (extractor and judges for these answers) and
    `price_date` are passed in by the caller when known."""
    from evalkit.schemas.summary import Efficiency
    from evalkit.stats import interval, ratio_interval

    n = len(rows)
    if n == 0 or n != resamples.n:
        raise ValueError("an efficiency block needs one record per resampled case")
    c = [r.completion for r in rows]
    total_in = sum(x.tokens_in for x in c)
    total_cached = sum(x.tokens_cached for x in c)
    costs = [x.cost_usd for x in c]
    correct = [1.0 if r.scores is not None and r.scores.label_correct else 0.0 for r in rows]
    n_correct = int(sum(correct))
    lat = [x.latency_ms if x.error != "transport" else None for x in c]
    n_lat = sum(1 for x in lat if x is not None)
    ivs = _latency_intervals(lat, resamples.rows) if n_lat else {}
    errors = sum(1 for x in c if x.error is not None)
    return Efficiency(
        mode="live",
        price_date=price_date,
        n=n,
        tokens_in_per_case=math.fsum(x.tokens_in for x in c) / n,
        tokens_out_per_case=math.fsum(x.tokens_out for x in c) / n,
        tokens_reasoning_per_case=math.fsum(x.tokens_reasoning for x in c) / n,
        tokens_cached_per_case=math.fsum(x.tokens_cached for x in c) / n,
        cache_hit_share=total_cached / total_in if total_in and total_cached <= total_in else None,
        cost_per_case_usd=interval(costs, resamples),
        cost_per_correct_usd=ratio_interval(costs, correct, resamples) if n_correct else None,
        n_correct=n_correct,
        latency_p50_ms=ivs.get(0.50),
        latency_p95_ms=ivs.get(0.95),
        latency_p99_ms=ivs.get(0.99),
        n_latency=n_lat,
        error_rate=errors / n,
        n_errored=errors,
        retry_rate=None,
        n_retried=None,
        eval_overhead_cost_usd=eval_overhead_usd,
        system_cost_usd=math.fsum(costs),
    )


def _efficiency_as_metrics(model: str, eff: Efficiency) -> list[Metric]:
    """The efficiency block as metrics-1 metrics with the standard names (spec §1b), labelled
    `model` and `mode` (and `price_date` when known). Figures that are None are left out."""
    labels: dict[str, Any] = {"model": model, "mode": eff.mode}
    if eff.price_date is not None:
        labels["price_date"] = eff.price_date
    n = eff.n
    out = [
        Metric(name=name, unit="count", value=value, n=n, labels=labels)
        for name, value in (
            ("tokens_in_per_case", eff.tokens_in_per_case),
            ("tokens_out_per_case", eff.tokens_out_per_case),
            ("tokens_reasoning_per_case", eff.tokens_reasoning_per_case),
            ("tokens_cached_per_case", eff.tokens_cached_per_case),
        )
    ]
    if eff.cache_hit_share is not None:
        out.append(
            Metric(
                name="cache_hit_share", unit="rate", value=eff.cache_hit_share, n=n, labels=labels
            )
        )
    out.append(
        Metric(
            name="error_rate",
            unit="rate",
            value=eff.error_rate,
            n=n,
            k=eff.n_errored,
            labels=labels,
        )
    )
    if eff.retry_rate is not None and eff.n_retried is not None:
        out.append(
            Metric(
                name="retry_rate",
                unit="rate",
                value=eff.retry_rate,
                n=n,
                k=eff.n_retried,
                labels=labels,
            )
        )
    out.append(_iv_metric("cost_per_case", "USD", eff.cost_per_case_usd, n, labels))
    if eff.cost_per_correct_usd is not None:
        out.append(
            _iv_metric("cost_per_correct", "USD", eff.cost_per_correct_usd, eff.n_correct, labels)
        )
    for name, iv in (
        ("latency_p50", eff.latency_p50_ms),
        ("latency_p95", eff.latency_p95_ms),
        ("latency_p99", eff.latency_p99_ms),
    ):
        if iv is not None:
            out.append(
                Metric(
                    name=name,
                    unit="ms",
                    value=int(iv.point),
                    n=eff.n_latency,
                    ci_low=iv.lo,
                    ci_high=iv.hi,
                    ci_method=BOOT_METHOD,
                    labels=labels,
                )
            )
    out.append(
        Metric(name="system_cost", unit="USD", value=eff.system_cost_usd, n=n, labels=labels)
    )
    if eff.eval_overhead_cost_usd is not None:
        out.append(
            Metric(
                name="eval_overhead_cost",
                unit="USD",
                value=eff.eval_overhead_cost_usd,
                n=n,
                labels=labels,
            )
        )
    return out


def efficiency_metrics(records: Sequence[Record], *, n_resamples: int = 10_000) -> list[Metric]:
    """The efficiency block per model from a run's records (spec §1b, D-67, D-70), as metrics:
    the same figures `aggregate` writes to `summary.json` under `efficiency` (the run's seed,
    cases in id order), labelled `model` and `mode: live`. The eval overhead and price date
    are not in the records, so they are left out here; `from_run_summary` carries them."""
    from evalkit.stats import Resamples

    by_model: dict[str, list[Record]] = {}
    for r in records:
        by_model.setdefault(r.completion.model, []).append(r)
    out: list[Metric] = []
    for model in sorted(by_model):
        rows = sorted(by_model[model], key=lambda r: r.case_id)
        rs = Resamples(len(rows), n_resamples, seed=rows[0].seed)
        out += _efficiency_as_metrics(model, efficiency_block(rows, rs))
    return out
