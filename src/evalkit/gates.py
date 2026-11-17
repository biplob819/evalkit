"""Gates on any metric (B5.3, GAT-01..10): `evalkit gate <summary.json> [--config gates.yaml]`.

Reads a `metrics-1` file (evalkit.metrics) or EvalKit's own `summary.json`, checks every gate
of a gates file, and returns a report with an exit code on EvalKit's scheme, precedence
2 > 3 > 4 > 1 > 0 (GAT-09, OWNER D-44). No model call, no network, no keys. Held-out splits
are printed only as split-level aggregates and free-text label values longer than 64
characters are never echoed (GAT-04). Every input the verdict reads is listed in `INPUTS`
(and in docs/gates.md, the input-classes table).
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

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

from evalkit.metrics import (
    NAME_RE,
    SCHEMA,
    MetricsError,
    MetricsSummary,
    describe_validation,
    from_run_summary,
    loads_json,
    safe_text,
    unit_kind,
    validate_metrics,
)

__all__ = [
    "EXIT_COST_CAP",
    "EXIT_FAIL",
    "EXIT_INPUT",
    "EXIT_PASS",
    "EXIT_PENDING",
    "INPUTS",
    "Gate",
    "GateReport",
    "GateRow",
    "GatesFile",
    "RunStatus",
    "disparity_gap",
    "evaluate",
    "load_gates",
    "parse_gates",
    "render_text",
    "run_gate",
]

EXIT_PASS, EXIT_FAIL, EXIT_INPUT, EXIT_COST_CAP, EXIT_PENDING = 0, 1, 2, 3, 4
DISPARITY_DRAWS = 10_000
GATE_SCHEMA = "gate-1"

INPUT_ONLY = "input-only"
COMPLETION = "completion-dependent"

# The input-classes table (GAT-09). Input-only checks always exit 2; completion-dependent ones
# exit 2 on a finished input, 3 on an EvalKit run stopped at its cost cap, 4 while judging is
# pending. tests/test_gates.py fails if the verdict reads anything not listed here, and if
# docs/gates.md lists anything else.
INPUTS: dict[str, str] = {
    # files
    "file:summary": INPUT_ONLY,  # readable UTF-8, strict JSON (no duplicate keys, no NaN)
    "file:gates": INPUT_ONLY,  # readable UTF-8, YAML mapping, no duplicate keys
    "file:run.json": INPUT_ONLY,  # beside an EvalKit summary, optional; same run_id
    "file:records.jsonl": INPUT_ONLY,  # beside an EvalKit summary, optional; same run_id
    # metrics-1 (validated in full; these are the fields the verdict then reads)
    "metrics.schema_version": INPUT_ONLY,
    "metrics.project": INPUT_ONLY,
    "metrics.run_id": INPUT_ONLY,
    "metrics.created_at": INPUT_ONLY,
    "metrics.simulated": INPUT_ONLY,
    "metrics.provenance": INPUT_ONLY,
    "metrics.metrics": INPUT_ONLY,
    "metrics.metrics[].name": INPUT_ONLY,  # a gate naming no metric: unknown metric
    "metrics.metrics[].labels": INPUT_ONLY,  # a gate whose labels match nothing
    "metrics.metrics[].unit": INPUT_ONLY,
    "metrics.metrics[].value": INPUT_ONLY,
    "metrics.metrics[].n": INPUT_ONLY,
    "metrics.metrics[].k": INPUT_ONLY,
    "metrics.metrics[].ci_low": INPUT_ONLY,
    "metrics.metrics[].ci_high": INPUT_ONLY,
    "metrics.metrics[].ci_method": INPUT_ONLY,
    # gates file
    "gates.gates": INPUT_ONLY,
    "gates.gates[].metric": INPUT_ONLY,
    "gates.gates[].labels": INPUT_ONLY,
    "gates.gates[].op": INPUT_ONLY,
    "gates.gates[].value": INPUT_ONLY,
    "gates.gates[].bound": INPUT_ONLY,
    "gates.gates[].severity": INPUT_ONLY,
    "gates.gates[].disparity": INPUT_ONLY,
    "gates.heldout_splits": INPUT_ONLY,
    # completion state of an EvalKit run
    "status.kind": INPUT_ONLY,
    "status.judging": COMPLETION,  # summary.json judging_status: pending -> 4
    "status.stopped_at_cost_cap": COMPLETION,  # run.json stopped_at_cost_cap -> 3
    "summary.schema_version": INPUT_ONLY,
    "summary.judging_status": COMPLETION,
    "run.run_id": INPUT_ONLY,
    "run.stopped_at_cost_cap": COMPLETION,
    # EvalKit summary.json fields converted to metrics-1 (evalkit.metrics.from_run_summary)
    "summary.run_id": INPUT_ONLY,
    "summary.task": INPUT_ONLY,
    "summary.config_hash": INPUT_ONLY,
    "summary.started_at": INPUT_ONLY,
    "summary.finished_at": INPUT_ONLY,
    "summary.models": INPUT_ONLY,
    "summary.slices": INPUT_ONLY,
    **{
        f"summary.{row}.{f}": INPUT_ONLY
        for row in ("models[]", "slices[]")
        for f in (
            "n",
            "n_errors",
            "cost_per_100_usd",
            "latency_p50_ms",
            "latency_p95_ms",
            "model",
            "task_scores",
            "accuracy",
            "citation_validity",
            "coverage",
        )
    },
    # per-judge figures are empty until every judge finishes: a gate on `headline`,
    # `groundedness`, `citation_precision` or `aim_citation_rate` that matches nothing in an
    # unfinished run exits 3 or 4 (any other unknown metric is input-only, exit 2)
    **{
        f"summary.{row}.{f}": COMPLETION
        for row in ("models[]", "slices[]")
        for f in ("headline", "groundedness_mean", "citation_precision", "aim_citation_rate")
    },
    **{
        f"summary.{row}.{f}.{b}": INPUT_ONLY
        for row in ("models[]", "slices[]")
        for f in ("accuracy", "citation_validity", "coverage")
        for b in ("point", "lo", "hi")
    },
    **{
        f"summary.{row}.{f}{{}}.{b}": INPUT_ONLY
        for row in ("models[]", "slices[]")
        for f in (
            "headline",
            "groundedness_mean",
            "citation_precision",
            "aim_citation_rate",
            "task_scores",
        )
        for b in ("point", "lo", "hi")
    },
    "summary.models[].cost_per_correct_usd": INPUT_ONLY,
    "summary.models[].efficiency": INPUT_ONLY,  # optional (0.3, D-70): the efficiency block
    **{
        f"summary.models[].efficiency.{f}": INPUT_ONLY
        for f in (
            "mode",
            "price_date",
            "n",
            "tokens_in_per_case",
            "tokens_out_per_case",
            "tokens_reasoning_per_case",
            "tokens_cached_per_case",
            "cache_hit_share",
            "cost_per_case_usd",
            "cost_per_correct_usd",
            "n_correct",
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_p99_ms",
            "n_latency",
            "error_rate",
            "n_errored",
            "retry_rate",
            "n_retried",
            "eval_overhead_cost_usd",
            "system_cost_usd",
        )
    },
    **{
        f"summary.models[].efficiency.{f}.{b}": INPUT_ONLY
        for f in (
            "cost_per_case_usd",
            "cost_per_correct_usd",
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_p99_ms",
        )
        for b in ("point", "lo", "hi")
    },
    "summary.models[].passes_threshold": INPUT_ONLY,  # the v0.2 thresholds verdict
    "summary.slices[].field": INPUT_ONLY,
    "summary.slices[].value": INPUT_ONLY,
    # records.jsonl, for the efficiency block (spec §1b); aggregated, never printed
    "records[].run_id": INPUT_ONLY,
    "records[].case_id": INPUT_ONLY,  # ordering for the seeded resamples only
    "records[].seed": INPUT_ONLY,
    "records[].scores": INPUT_ONLY,
    "records[].scores.label_correct": INPUT_ONLY,
    "records[].completion": INPUT_ONLY,
    **{
        f"records[].completion.{f}": INPUT_ONLY
        for f in (
            "model",
            "tokens_in",
            "tokens_out",
            "tokens_reasoning",
            "tokens_cached",
            "cost_usd",
            "latency_ms",
            "error",
        )
    },
}


# ---- the gates file ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


GateValue = StrictBool | StrictInt | StrictFloat


class Gate(_Strict):
    metric: StrictStr
    labels: dict[str, StrictStr | StrictInt | StrictBool] | None = None  # "*" = any value
    op: Literal[">=", "<=", "=="]
    value: GateValue
    bound: Literal["point", "lower", "upper"] = "point"
    severity: Literal["fail", "warn"] = "fail"
    disparity: Annotated[StrictStr, Field(min_length=1)] | None = None

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("metric must match [a-z][a-z0-9_.]*")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Gate:
        if isinstance(self.value, bool):
            if self.op != "==":
                raise ValueError("a true/false gate uses op ==")
            if self.bound != "point" or self.disparity:
                raise ValueError("a true/false gate has no bound and no disparity")
        elif isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("value must be finite")
        if self.disparity and self.labels and self.labels.get(self.disparity) not in (None, "*"):
            raise ValueError("a disparity gate cannot fix the value of its own label")
        return self


class GatesFile(_Strict):
    gates: Annotated[list[Gate], Field(min_length=1)]
    # Compared as canonical strings (GAT-04): `heldout_splits: [2024]` or `["2024"]` hides a
    # `split` label stored as 2024 or "2024" alike, so a number never slips past as public.
    heldout_splits: list[StrictStr | StrictInt | StrictBool] = Field(default_factory=list)


def canonical(value: object) -> str:
    """A label value as one string for held-out matching: strings as is, integers in decimal,
    booleans as JSON (`true`/`false`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


class _NoDupLoader:  # built lazily so yaml stays an import-time detail
    loader: Any = None


def _yaml_loader() -> Any:
    if _NoDupLoader.loader is None:
        import yaml

        class Loader(yaml.SafeLoader):
            pass

        def mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
            seen: set[str] = set()
            for k, _ in node.value:
                key = loader.construct_object(k, deep=deep)
                text = repr(key)
                if text in seen:
                    raise ValueError(
                        f"duplicate key {safe_text(str(key))!r} (line {k.start_mark.line + 1}); "
                        "each key may appear once in a mapping"
                    )
                seen.add(text)
            return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

        Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        _NoDupLoader.loader = Loader
    return _NoDupLoader.loader


def parse_gates(text: str) -> GatesFile:
    """Validate a gates YAML document; raises MetricsError (safe to print)."""
    import yaml

    try:
        data = yaml.load(text, Loader=_yaml_loader())
    except RecursionError:
        raise MetricsError(["gates file: nested too deeply"]) from None
    except (yaml.YAMLError, ValueError) as exc:
        where = getattr(exc, "problem_mark", None)
        at = f" (line {where.line + 1})" if where is not None else ""
        if isinstance(exc, yaml.YAMLError):
            detail = "not valid YAML"
        elif "integer string conversion" in str(exc):  # Python's 4300-digit limit
            detail = "an integer is too large (at most 4300 digits)"
        else:
            detail = str(exc)
        raise MetricsError([f"gates file: {detail}{at}"]) from None
    if not isinstance(data, dict):
        raise MetricsError(["gates file: expected a mapping with a `gates` list"])
    try:
        return GatesFile.model_validate(data)
    except ValidationError as exc:
        raise MetricsError(describe_validation(exc, "gates file: ")) from None


def load_gates(path: Path | str) -> GatesFile:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MetricsError([f"cannot read gates file {path}: {type(exc).__name__}"]) from None
    return parse_gates(text)


DEFAULT_GATES = GatesFile(
    gates=[Gate(metric="passes_threshold", labels={"model": "*"}, op="==", value=True)]
)


# ---- results ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStatus:
    """Completion of the input: metrics-1 files are always complete."""

    kind: Literal["metrics-1", "evalkit-summary"] = "metrics-1"
    judging: Literal["complete", "pending"] = "complete"
    stopped_at_cost_cap: bool = False


Result = Literal["pass", "warn", "fail", "error"]


@dataclass
class GateRow:
    gate: int  # 1-based position in the gates file
    metric: str
    labels: dict[str, str]  # as shown: long values hidden
    bound: str
    op: str
    threshold: bool | int | float
    severity: str
    result: Result
    observed: bool | int | float | None = None
    unit: str | None = None
    ci_low: float | int | None = None
    ci_high: float | int | None = None
    ci_method: str | None = None
    n: int | None = None
    k: int | None = None
    note: str = ""
    heldout_rows: int = 0  # > 0: a held-out split-level aggregate of this many rows

    def as_dict(self) -> dict[str, Any]:
        out = {
            "gate": self.gate,
            "metric": self.metric,
            "labels": self.labels,
            "bound": self.bound,
            "op": self.op,
            "threshold": self.threshold,
            "observed": self.observed,
            "unit": self.unit,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "ci_method": self.ci_method,
            "n": self.n,
            "k": self.k,
            "severity": self.severity,
            "result": self.result,
            "note": self.note,
        }
        if self.heldout_rows:
            out["heldout_rows"] = self.heldout_rows
        return out


@dataclass
class GateReport:
    exit_code: int
    verdict: str
    simulated: bool | None
    rows: list[GateRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GATE_SCHEMA,
            "exit_code": self.exit_code,
            "verdict": self.verdict,
            "simulated": self.simulated,
            "source": self.source,
            "gates": [r.as_dict() for r in self.rows],
            "failed": sum(1 for r in self.rows if r.result == "fail"),
            "warned": sum(1 for r in self.rows if r.result == "warn"),
            "errors": list(self.errors),
        }


VERDICTS = {
    EXIT_PASS: "pass",
    EXIT_FAIL: "fail",
    EXIT_INPUT: "error",
    EXIT_COST_CAP: "stopped at the cost cap",
    EXIT_PENDING: "judging pending",
}


def _shown_labels(labels: Mapping[str, Any]) -> dict[str, str]:
    return {safe_text(k): safe_text(v) for k, v in sorted(labels.items())}


def _matches(labels: Mapping[str, Any], want: Mapping[str, Any] | None) -> bool:
    if not want:
        return True
    for key, value in want.items():
        if key not in labels:
            return False
        if value == "*":
            continue
        have = labels[key]
        if type(have) is not type(value) or have != value:
            return False
    return True


def _compare(observed: bool | int | float, op: str, threshold: bool | int | float) -> bool:
    # Python compares int and float exactly, so no integer is converted before comparing.
    if op == "==":
        return observed == threshold
    if op == ">=":
        return observed >= threshold
    return observed <= threshold


CP_METHOD = "clopper-pearson"


def _exact(m: Any) -> bool:
    """A rate with k and n and no supplied interval: EvalKit computes it exactly."""
    return (
        m.ci_low is None and m.unit == "rate" and m.k is not None and m.n is not None and m.n >= 1
    )


def _interval(m: Any) -> tuple[Any, Any, str] | None:
    """The metric's displayed 95% interval: supplied (carried exactly) or, for a rate with k
    and n, the standard two-sided exact Clopper-Pearson interval (GAT-07, D-70)."""
    from evalkit.stats import clopper_pearson

    if m.ci_low is not None and m.ci_high is not None:
        return m.ci_low, m.ci_high, m.ci_method
    if _exact(m):
        iv = clopper_pearson(m.k, m.n)
        return iv.lo, iv.hi, CP_METHOD
    return None


def _bound(m: Any, side: str, iv: tuple[Any, Any, str]) -> Any:
    """What `bound: lower|upper` gates: a supplied interval's own bound, or for a rate with k
    and n the exact one-sided 95% Clopper-Pearson bound (D-70: n = 150, k = 0 -> 0.0198,
    k = 1 -> 0.0312), which gives the stated 95% for every count."""
    from evalkit.stats import clopper_pearson

    if _exact(m):
        one = clopper_pearson(m.k, m.n, sided="upper" if side == "upper" else "lower")
        return one.hi if side == "upper" else one.lo
    return iv[1] if side == "upper" else iv[0]


def _cp_draw(rng: random.Random, k: int, n: int) -> float:
    """One draw from a group's Clopper-Pearson confidence distribution: an even mix of the
    Beta(k, n-k+1) and Beta(k+1, n-k) laws whose quantiles are the exact bounds."""
    if rng.random() < 0.5:
        a, b = k, n - k + 1
    else:
        a, b = k + 1, n - k
    if a == 0:
        return 0.0
    if b == 0:
        return 1.0
    return rng.betavariate(a, b)


def disparity_gap(
    groups: Mapping[str, tuple[float, int | None, int | None]],
    *,
    seed: str = "",
    draws: int = DISPARITY_DRAWS,
) -> tuple[float, tuple[float, float] | None]:
    """Max - min of the group values, and a seeded 95% percentile interval when every group is
    a rate with n >= 1 (k given, or derived as round(value * n)): each draw takes one value per
    group from its Clopper-Pearson confidence distribution (GAT-06). `groups` maps a group to
    (value, k, n); k and n may be None (no interval then)."""
    from evalkit.stats import interval_from

    values = [v for v, _, _ in groups.values()]
    gap = max(values) - min(values)
    counts: list[tuple[int, int]] = []
    for v, k, n in groups.values():
        if n is None or n < 1:
            return gap, None
        counts.append((k if k is not None else round(v * n), n))
    rng = random.Random(f"evalkit-disparity:{seed}")
    boot = []
    for _ in range(draws):
        ps = [_cp_draw(rng, k, n) for k, n in counts]
        boot.append(max(ps) - min(ps))
    iv = interval_from(float(gap), boot)
    return gap, (iv.lo, iv.hi)


def _check_type(metric: Any, gate: Any) -> str | None:
    is_bool = unit_kind(metric.unit) == "bool"
    if is_bool != isinstance(gate.value, bool):
        if is_bool:
            return f"metric {metric.name} is true/false but the gate's value is a number"
        return f"metric {metric.name} is numeric but the gate's value is true/false"
    if is_bool and (gate.bound != "point" or gate.disparity):
        return f"metric {metric.name} is true/false: no bound or disparity"
    return None


def _hidden_heldout(labels: Mapping[str, Any], heldout: frozenset[str]) -> bool:
    """A row from a held-out split: its `split` label, as a canonical string, is listed."""
    return "split" in labels and canonical(labels["split"]) in heldout


PER_JUDGE = frozenset({"headline", "groundedness", "citation_precision", "aim_citation_rate"})


def evaluate(metrics: Any, gates: Any, status: Any = None) -> GateReport:
    """Check every gate against a validated MetricsSummary. Never raises for bad gates: their
    problems are errors in the report. Exit precedence 2 > 3 > 4 > 1 > 0."""
    status = status if status is not None else RunStatus()
    heldout = frozenset(canonical(v) for v in gates.heldout_splits)
    unfinished = status.judging == "pending" or status.stopped_at_cost_cap
    rows: list[GateRow] = []
    input_errors: list[str] = []
    missing: list[str] = []  # per-judge metrics an unfinished EvalKit run has not reported yet
    all_metrics = tuple(metrics.metrics)
    for gi, gate in enumerate(gates.gates, start=1):
        g_metric, g_labels, g_op, g_value = gate.metric, gate.labels, gate.op, gate.value
        g_bound, g_severity, g_disp = gate.bound, gate.severity, gate.disparity
        where = f"gate {gi} ({safe_text(g_metric)})"
        base = {
            "gate": gi,
            "metric": safe_text(g_metric),
            "bound": g_bound,
            "op": g_op,
            "threshold": g_value,
            "severity": g_severity,
        }
        # Only per-judge figures can still appear once an EvalKit run finishes; any other
        # name or label that matches nothing is bad input (exit 2) even while judging is
        # pending or the run stopped at its cost cap.
        later = (
            missing
            if unfinished and status.kind == "evalkit-summary" and g_metric in PER_JUDGE
            else input_errors
        )
        named = [m for m in all_metrics if m.name == g_metric]
        if not named:
            later.append(f"{where}: no metric named {safe_text(g_metric)}")
            rows.append(
                GateRow(
                    labels=_shown_labels(g_labels or {}),
                    result="error",
                    note="unknown metric",
                    **base,
                )
            )
            continue
        matched = [m for m in named if _matches(m.labels, g_labels)]
        if g_disp:
            matched = [m for m in matched if g_disp in m.labels]
        if not matched:
            later.append(f"{where}: matches no metric")
            rows.append(
                GateRow(
                    labels=_shown_labels(g_labels or {}),
                    result="error",
                    note="matches nothing",
                    **base,
                )
            )
            continue
        problems = [p for m in matched if (p := _check_type(m, gate))]
        if problems:
            input_errors.append(f"{where}: {problems[0]}")
            rows.append(
                GateRow(
                    labels=_shown_labels(g_labels or {}), result="error", note=problems[0], **base
                )
            )
            continue
        fail_as: Result = "fail" if g_severity == "fail" else "warn"
        if g_disp:
            for row, combo in _disparity_rows(matched, g_disp, base, where, fail_as, input_errors):
                if _hidden_heldout(combo, heldout):
                    row.heldout_rows = 1  # a held-out combination is folded like any row
                rows.append(row)
            continue
        for m in matched:
            unit, value, labels = m.unit, m.value, m.labels
            iv = _interval(m)
            if g_bound != "point" and iv is None:
                msg = f"{where}: bound {g_bound} on a metric without an interval"
                input_errors.append(msg)
                row = GateRow(
                    labels=_shown_labels(labels),
                    result="error",
                    unit=unit,
                    observed=value,
                    note="no interval",
                    **base,
                )
            else:
                observed = value if g_bound == "point" or iv is None else _bound(m, g_bound, iv)
                ok = _compare(observed, g_op, g_value)
                n, k = m.n, m.k
                note = ""
                if unit == "rate" and k is not None and n:
                    from evalkit.stats import rate_wording

                    note = rate_wording(k, n)
                    if g_bound != "point" and _exact(m):
                        note += f"; gated on the one-sided 95% {g_bound} bound {observed:.4g}"
                row = GateRow(
                    labels=_shown_labels(labels),
                    result="pass" if ok else fail_as,
                    observed=observed,
                    unit=unit,
                    ci_low=None if iv is None else iv[0],
                    ci_high=None if iv is None else iv[1],
                    ci_method=None if iv is None else iv[2],
                    n=n,
                    k=k,
                    note=note,
                    **base,
                )
            if _hidden_heldout(labels, heldout):
                row.heldout_rows = 1  # folded below: never printed with its other labels
            rows.append(row)
    rows = _fold_heldout(rows)
    if input_errors:
        code = EXIT_INPUT
    elif status.stopped_at_cost_cap:
        code = EXIT_COST_CAP
    elif status.judging == "pending":
        code = EXIT_PENDING
    elif any(r.result == "fail" for r in rows):
        code = EXIT_FAIL
    else:
        code = EXIT_PASS
    errors = input_errors + missing
    if missing:
        errors.append(
            "the metrics above may appear once the run completes"
            + (" (stopped at the cost cap)" if status.stopped_at_cost_cap else " (judging pending)")
        )
    return GateReport(
        exit_code=code,
        verdict=VERDICTS[code],
        simulated=bool(metrics.simulated),
        rows=rows,
        errors=errors,
        source={
            "kind": status.kind,
            "project": safe_text(metrics.project),
            "run_id": safe_text(metrics.run_id),
        },
    )


def _group_name(value: Any, mixed: bool) -> str:
    """A disparity group as shown: when one disparity mixes label types, strings are quoted so
    the group 1 and the group "1" stay told apart."""
    if mixed and isinstance(value, str):
        return safe_text(json.dumps(value))
    return safe_text(value)


def _disparity_rows(
    matched: Sequence[Any],
    label: str,
    base: dict[str, Any],
    where: str,
    fail_as: Result,
    input_errors: list[str],
) -> list[tuple[GateRow, dict[str, Any]]]:
    """One row per combination of the other labels, with that combination's raw labels (for
    the held-out check). Groups are keyed by the label's type and value, so 1 and "1" (and
    true) are different groups, as everywhere else in metrics-1."""
    combos: dict[tuple[Any, ...], list[Any]] = {}
    for m in matched:
        rest = tuple(sorted((k, type(v).__name__, v) for k, v in m.labels.items() if k != label))
        combos.setdefault(rest, []).append(m)
    out: list[tuple[GateRow, dict[str, Any]]] = []
    for rest, ms in sorted(combos.items(), key=lambda kv: repr(kv[0])):
        labels = {k: v for k, _, v in rest}
        shown = {**_shown_labels(labels), safe_text(label): "max - min"}
        units = {m.unit for m in ms}
        if len(ms) < 2 or len(units) != 1:
            why = "needs at least two groups" if len(ms) < 2 else "groups carry different units"
            input_errors.append(f"{where}: disparity over {safe_text(label)} {why}")
            out.append((GateRow(labels=shown, result="error", note=why, **base), labels))
            continue
        mixed = len({type(m.labels[label]) for m in ms}) > 1
        keyed = {
            (type(m.labels[label]).__name__, m.labels[label]): (
                m.value,
                m.k,
                m.n if m.unit == "rate" else None,
            )
            for m in ms
        }
        names = {key: _group_name(key[1], mixed) for key in keyed}
        groups = {repr(key): v for key, v in keyed.items()}
        gap, iv = disparity_gap(groups, seed=f"{base['metric']}|{label}|{rest!r}")
        if base["bound"] != "point" and iv is None:
            msg = f"{where}: bound {base['bound']} on a disparity without group counts (n)"
            input_errors.append(msg)
            out.append(
                (
                    GateRow(labels=shown, result="error", observed=gap, note="no interval", **base),
                    labels,
                )
            )
            continue
        observed = gap if base["bound"] == "point" else iv[0] if base["bound"] == "lower" else iv[1]  # type: ignore[index]
        hi_g = max(keyed, key=lambda g: keyed[g][0])
        lo_g = min(keyed, key=lambda g: keyed[g][0])
        row = GateRow(
            labels=shown,
            result="pass" if _compare(observed, base["op"], base["threshold"]) else fail_as,
            observed=observed,
            unit=units.pop(),
            ci_low=None if iv is None else iv[0],
            ci_high=None if iv is None else iv[1],
            ci_method=None if iv is None else "bootstrap of Clopper-Pearson draws (seeded)",
            note=f"{len(keyed)} groups; highest {names[hi_g]}, lowest {names[lo_g]}",
            **base,
        )
        out.append((row, labels))
    return out


_RANK: dict[str, int] = {"pass": 0, "warn": 1, "fail": 2, "error": 3}


def _fold_heldout(rows: list[GateRow]) -> list[GateRow]:
    """Held-out rows become one split-level row per gate, split, model and judge: the number of
    rows, the worst result and the worst observed value; no other label is shown (GAT-04)."""
    out: list[GateRow] = []
    folded: dict[tuple[Any, ...], GateRow] = {}
    for r in rows:
        if not r.heldout_rows:
            out.append(r)
            continue
        keep = {k: v for k, v in r.labels.items() if k in ("split", "model", "judge")}
        key = (r.gate, tuple(sorted(keep.items())))
        agg = folded.get(key)
        if agg is None:
            agg = GateRow(
                gate=r.gate,
                metric=r.metric,
                labels=keep,
                bound=r.bound,
                op=r.op,
                threshold=r.threshold,
                severity=r.severity,
                result=r.result,
                observed=r.observed,
                unit=r.unit,
                heldout_rows=0,
            )
            folded[key] = agg
            out.append(agg)
        agg.heldout_rows += 1
        if _RANK[r.result] > _RANK[agg.result]:
            agg.result = r.result
        worse = (r.op == "<=" and _gt(r.observed, agg.observed)) or (
            r.op == ">=" and _gt(agg.observed, r.observed)
        )
        if worse or (r.op == "==" and r.result != "pass"):
            agg.observed = r.observed
        agg.note = f"held-out split: {agg.heldout_rows} row(s), worst shown"
    return out


def _gt(a: Any, b: Any) -> bool:
    return a is not None and b is not None and a > b


# ---- loading and the command ------------------------------------------------------------------

_EVALKIT_VERSION = re.compile(r"^\d+\.\d+$")


def _load_summary(path: Path) -> tuple[MetricsSummary, RunStatus]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MetricsError([f"cannot read {path}: {type(exc).__name__}"]) from None
    data = loads_json(text)
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version == SCHEMA:
        return validate_metrics(data), RunStatus()
    if isinstance(version, str) and _EVALKIT_VERSION.match(version):
        return _load_evalkit(path, text)
    raise MetricsError(
        [
            "not a metrics-1 file or an EvalKit summary.json (schema_version must be "
            f"{SCHEMA!r} or EvalKit's 1.x)"
        ]
    )


def _load_evalkit(path: Path, text: str) -> tuple[MetricsSummary, RunStatus]:
    from evalkit.schemas import Record, RunSummary

    try:
        summary = RunSummary.model_validate_json(text)
    except ValidationError as exc:
        raise MetricsError(describe_validation(exc, "summary: ")) from None
    stopped = False
    run_json = path.parent / "run.json"
    if run_json.is_file():
        try:
            run = loads_json(run_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise MetricsError([f"cannot read {run_json}: {type(exc).__name__}"]) from None
        if not isinstance(run, dict) or run.get("run_id") != summary.run_id:
            raise MetricsError([f"{run_json} does not belong to this summary's run"])
        flag = run.get("stopped_at_cost_cap", False)
        if not isinstance(flag, bool):
            raise MetricsError([f"{run_json}: stopped_at_cost_cap must be true or false"])
        stopped = flag
    records = None
    rec_path = path.parent / "records.jsonl"
    if rec_path.is_file():
        try:
            lines = rec_path.read_text(encoding="utf-8").splitlines()
            records = [Record.model_validate_json(x) for x in lines if x.strip()]
        except (OSError, UnicodeDecodeError, ValidationError):
            raise MetricsError([f"{rec_path} is not a valid records file"]) from None
        if any(r.run_id != summary.run_id for r in records):
            raise MetricsError([f"{rec_path} does not belong to this summary's run"])
    try:
        metrics = from_run_summary(summary, records)
    except (ValueError, ValidationError) as exc:
        raise MetricsError([f"cannot convert the summary: {type(exc).__name__}"]) from None
    status = RunStatus(
        kind="evalkit-summary", judging=summary.judging_status, stopped_at_cost_cap=stopped
    )
    return metrics, status


def run_gate(summary_path: Path | str, config_path: Path | str | None = None) -> GateReport:
    """Load, check and report. Input problems give a report with exit 2, never an exception."""
    summary_path = Path(summary_path)
    source = {"path": str(summary_path)}
    try:
        metrics, status = _load_summary(summary_path)
    except MetricsError as exc:
        return GateReport(
            EXIT_INPUT, VERDICTS[EXIT_INPUT], None, errors=list(exc.problems), source=source
        )
    try:
        if config_path is not None:
            gates = load_gates(config_path)
        elif status.kind == "evalkit-summary":
            gates = DEFAULT_GATES  # v0.2 thresholds, with their meaning (GAT-01)
        else:
            raise MetricsError(["no gates: give a gates file with --config"])
    except MetricsError as exc:
        return GateReport(
            EXIT_INPUT,
            VERDICTS[EXIT_INPUT],
            bool(metrics.simulated),
            errors=list(exc.problems),
            source={
                **source,
                "kind": status.kind,
                "project": safe_text(metrics.project),
                "run_id": safe_text(metrics.run_id),
            },
        )
    report = evaluate(metrics, gates, status)
    report.source = {"path": str(summary_path), **report.source}
    if config_path is not None:
        report.source["config"] = str(config_path)
    return report


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def render_text(report: GateReport) -> str:
    """The table of every gate, then the verdict; `simulated` first (GAT-05)."""
    sim = report.simulated
    lines = [
        "SIMULATED: true (results from a simulator, not production)"
        if sim
        else f"simulated: {_fmt(sim) if sim is not None else 'unknown (input not read)'}"
    ]
    src = report.source
    if src.get("project"):
        lines.append(
            f"source: {src.get('path', '')} ({src.get('kind')}, project "
            f"{src['project']}, run {src.get('run_id')})"
        )
    header = ("metric", "labels", "bound", "op", "threshold", "observed", "severity", "result")
    table: list[tuple[str, ...]] = [header]
    for r in report.rows:
        # strings are quoted so a text label "1" and a number 1 read differently
        labels = (
            ", ".join(
                f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in r.labels.items()
            )
            or "(any)"
        )
        observed = _fmt(r.observed)
        if r.ci_low is not None and r.bound == "point":
            observed += f" [{_fmt(r.ci_low)}, {_fmt(r.ci_high)}]"
        if r.unit and r.unit not in ("bool", "rate", "count"):
            observed += f" {r.unit}"
        table.append(
            (
                r.metric,
                labels,
                r.bound,
                r.op,
                _fmt(r.threshold),
                observed,
                r.severity,
                r.result.upper(),
            )
        )
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
    notes = [f"  gate {r.gate} {r.metric}: {r.note}" for r in report.rows if r.note]
    if notes:
        lines.append("notes:")
        lines += notes
    for e in report.errors:
        lines.append(f"error: {e}")
    failed = sum(1 for r in report.rows if r.result == "fail")
    warned = sum(1 for r in report.rows if r.result == "warn")
    lines.append(
        f"verdict: {report.verdict.upper()} (exit {report.exit_code}) · {failed} failed · "
        f"{warned} warning(s) · {len(report.rows)} row(s)"
    )
    return "\n".join(lines)


def report_json(report: GateReport) -> str:
    return json.dumps(report.as_dict(), sort_keys=True, indent=2) + "\n"
