"""Run-vs-run compare (B5.6, RVR-01..03, D-62, D-65): `evalkit compare OLD NEW`.

Says, per metric and label, whether a new run is better or worse than an old one, with a 95%
interval for the change new - old, and without leaking held-out cases:

- **paired** when both runs carry per-case values for the same case ids, and the shared ids
  are at least half of each run's cases: a two-level bootstrap over the shared cases, then
  each run's trials within a case (D-41, D-51); a case-only resample when there is no trial
  level (pass^k is computed per case from all k trials, so it is resampled over cases only);
- otherwise **unpaired**: each run's own 95% interval (per-case bootstrap, the supplied
  interval, or exact Clopper-Pearson) combined by MOVER (Zou & Donner 2008), and labelled
  `unpaired` in every output (`partial_overlap` when both runs had case ids).

A change is claimed only from at least MIN_CASES (10) cases and a non-zero-width interval;
other rows are shown, marked `too_few_cases` or `degenerate`, and never flagged. Claimed
changes are flagged after Holm adjustment where the interval excludes 0.
Settings that differ (snapshot, code version, config and prompt hashes, models, EvalKit
version, and any recorded hash in `settings.json`) are listed first; metrics or labels on one
side only are listed, never skipped. Held-out rows appear only as split-level aggregates and
no output ever prints a case id. With a gates file, a baseline must name every gated metric
with at least 2 shared cases (RVR-03). No model call, no network, no keys. Seeded: the same
inputs and seed give the same bytes.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evalkit.gates import GatesFile, canonical, load_gates
from evalkit.metrics import (
    COUNT_MEANS,
    NAME_RE,
    RATE_ABS_TOL,
    RATE_REL_TOL,
    SCHEMA,
    Metric,
    MetricsError,
    MetricsSummary,
    describe_validation,
    loads_json,
    safe_text,
    unit_kind,
    validate_metrics,
)
from evalkit.schemas import Interval
from evalkit.stats import ALPHA, Resamples, clopper_pearson, holm, interval, interval_from, paired

__all__ = [
    "COMPARE_SCHEMA",
    "EXIT_INPUT",
    "EXIT_PASS",
    "EXIT_REGRESSION",
    "CaseValue",
    "Change",
    "CompareError",
    "Comparison",
    "Missing",
    "RunInput",
    "SettingDifference",
    "compare",
    "compare_runs",
    "load_input",
    "paired_bootstrap",
    "render_markdown",
    "report_json",
]

COMPARE_SCHEMA = "compare-1"
EXIT_PASS, EXIT_REGRESSION, EXIT_INPUT = 0, 1, 2
VERDICTS = {EXIT_PASS: "no flagged regression", EXIT_REGRESSION: "regression", EXIT_INPUT: "error"}
DEFAULT_SEED = 7
DEFAULT_RESAMPLES = 10_000
DEFAULT_HELDOUT = "heldout"
Z975 = 1.959963984540054  # the standard normal 97.5% quantile
TWO_LEVEL = "paired two-level bootstrap (cases, then trials)"
CASE_ONLY = "paired case bootstrap (no trial level)"
MOVER = "unpaired: MOVER from each run's 95% interval"
NO_INTERVAL = "unpaired: no interval"
KEPT_HELDOUT = ("split", "model", "judge")  # the only labels a held-out row shows (GAT-04)
_SIDECAR_KEYS = frozenset({"case_id", "trial", "metric", "value", "labels"})
_EVALKIT_VERSION = re.compile(r"^\d+\.\d+$")
_EFF_KEYS = frozenset({"model", "mode", "price_date"})  # a model-level efficiency row's labels
# A change is CLAIMED (enters Holm, can be flagged, can exit 1) only from at least MIN_CASES
# cases: the shared cases of a paired row, each side's cases for a per-case bootstrap. Below
# about ten a percentile bootstrap's 95% interval badly undercovers: it takes only a handful of
# distinct values (2 cases give 3 resample means, 5 give 126), so two identical drops read as
# certain. RVR-03's "at least 2 shared cases" for a gated baseline is a separate, looser rule
# about what a baseline must contain, not about what may be claimed.
MIN_CASES = 10
# A row is paired only when its shared cases are at least half of each side's per-case units.
# Below that the shared subset says little about either run: the row is compared unpaired over
# all its cases and labelled partial_overlap, with the cases left without a partner counted.
MIN_OVERLAP_SHARE = 0.5
OK, TOO_FEW, DEGENERATE = "ok", "too_few_cases", "degenerate"

LabelKey = tuple[tuple[str, str, Any], ...]
Key = tuple[str, LabelKey]


class CompareError(MetricsError):
    """An input that cannot be compared. `problems` never echo values or case ids."""


def _label_key(labels: Mapping[str, Any]) -> LabelKey:
    # typed, as metrics-1: the label value 1 and "1" (and true) stay distinct
    return tuple(sorted((k, type(v).__name__, v) for k, v in labels.items()))


def _shown(labels: Mapping[str, Any], quoted: frozenset[str] = frozenset()) -> dict[str, str]:
    """Labels as shown. For a key whose values mix types across the two runs, text values are
    quoted (as `evalkit gate` shows disparity groups), so `cohort=2024` and `cohort="2024"`
    read differently in JSON and markdown."""
    from evalkit.gates import _group_name

    return {safe_text(k): _group_name(v, k in quoted) for k, v in sorted(labels.items())}


def _mixed_keys(*runs: MetricsSummary) -> frozenset[str]:
    """Label keys whose values carry more than one type (text, integer, true/false)."""
    types: dict[str, set[str]] = {}
    for run in runs:
        for m in run.metrics:
            for k, v in m.labels.items():
                types.setdefault(k, set()).add(type(v).__name__)
    return frozenset(k for k, ts in types.items() if len(ts) > 1)


# ---- inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseValue:
    """One per-case value: from a sidecar line, or from an EvalKit record."""

    metric: str
    labels: Mapping[str, Any]
    value: float
    case_id: str | None = None  # None: a held-out row (never paired)
    trial: int | None = None  # None: no trial level (e.g. pass^k over all k trials)

    def key(self) -> Key:
        return (self.metric, _label_key(self.labels))


@dataclass(frozen=True)
class RunInput:
    """One side of a comparison, loaded and validated."""

    path: str
    kind: Literal["metrics-1", "evalkit-run"]
    metrics: MetricsSummary
    cases: tuple[CaseValue, ...] | None  # None: no per-case values
    settings: Mapping[str, str | None]
    status: str = "complete"

    def source(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "project": safe_text(self.metrics.project),
            "run_id": safe_text(self.metrics.run_id),
            "simulated": bool(self.metrics.simulated),
            "status": self.status,
            "per_case_values": self.cases is not None,
        }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CompareError([f"cannot read {path}: {type(exc).__name__}"]) from None


def load_input(path: str | Path) -> RunInput:
    """An EvalKit run folder (or its summary.json), a folder holding metrics.json, or a
    metrics-1 file, with its optional per-case sidecar `cases.jsonl` and `settings.json`."""
    p = Path(path)
    if p.is_dir():
        if (p / "summary.json").is_file():
            return _load_run(p / "summary.json")
        if (p / "metrics.json").is_file():
            p = p / "metrics.json"
        else:
            raise CompareError(
                [f"{p} holds neither an EvalKit run (summary.json) nor a metrics.json"]
            )
    elif not p.is_file():
        raise CompareError([f"cannot read {p}: no such file or folder"])
    data = loads_json(_read_text(p))
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version == SCHEMA:
        return _load_metrics(p, data)
    if isinstance(version, str) and _EVALKIT_VERSION.match(version):
        return _load_run(p)
    raise CompareError(
        [f"{p}: not a metrics-1 file or an EvalKit summary.json (schema_version must be "
         f"{SCHEMA!r} or EvalKit's 1.x)"]
    )  # fmt: skip


def _load_metrics(path: Path, data: Any) -> RunInput:
    metrics = validate_metrics(data)
    folder = path.parent
    cases = None
    # an EvalKit run folder's cases.jsonl holds case definitions, never per-case values
    if not (folder / "run.json").is_file() and (folder / "cases.jsonl").is_file():
        cases = _read_sidecar(folder / "cases.jsonl", metrics, path.name)
    settings = _metrics_settings(metrics)
    _merge_settings(settings, _read_settings(folder / "settings.json"))
    return RunInput(str(path), "metrics-1", metrics, cases, settings)


def _bad(where: str, what: str) -> CompareError:
    return CompareError([f"{where}: {what}"])


def _read_sidecar(path: Path, metrics: MetricsSummary, source: str) -> tuple[CaseValue, ...]:
    """The per-case sidecar, strictly: every line names a metric of the metrics file."""
    units = {(m.name, m.label_key()): m.unit for m in metrics.metrics}
    out: list[CaseValue] = []
    seen: set[tuple[Key, str, int | None]] = set()
    with_trial: dict[tuple[Key, str], bool] = {}
    first_line: dict[Key, int] = {}
    for i, line in enumerate(_read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name} line {i}"
        try:
            row = loads_json(line)
        except MetricsError as exc:
            raise _bad(where, exc.problems[0]) from None
        if not isinstance(row, dict):
            raise _bad(where, "each line is one JSON object")
        extra = sorted(safe_text(k) for k in set(row) - _SIDECAR_KEYS)
        if extra:
            raise _bad(where, f"unknown key(s) {', '.join(extra)}")
        metric, value = row.get("metric"), row.get("value")
        labels = row.get("labels", {})
        if not isinstance(metric, str) or not NAME_RE.match(metric):
            raise _bad(where, "metric must be a metrics-1 name ([a-z][a-z0-9_.]*)")
        if isinstance(value, bool):
            v = 1.0 if value else 0.0
        elif isinstance(value, int | float) and math.isfinite(value):
            v = float(value)
        else:
            raise _bad(where, "value must be a finite number or true/false")
        try:  # labels exactly as metrics-1 allows them (no id keys)
            Metric.model_validate({"name": metric, "unit": "ratio", "value": 0.0, "labels": labels})
        except ValidationError as exc:
            raise CompareError([f"{where}: {p}" for p in describe_validation(exc)]) from None
        case_id = row.get("case_id")
        if "case_id" in row and (not isinstance(case_id, str) or not case_id.strip()):
            raise _bad(where, "case_id must be a non-blank string (leave it out on held-out rows)")
        trial = row.get("trial")
        if "trial" in row and (isinstance(trial, bool) or not isinstance(trial, int) or trial < 0):
            raise _bad(where, "trial must be an integer >= 0")
        key = (metric, _label_key(labels))
        unit = units.get(key)
        if unit is None:
            raise _bad(where, f"no metric {safe_text(metric)} with these labels in {source}")
        if unit_kind(unit) == "bool":
            raise _bad(where, f"{safe_text(metric)} is true/false: it has no per-case values")
        if unit == "count" and metric not in COUNT_MEANS:
            raise _bad(
                where,
                f"{safe_text(metric)} is a count: per-case values are taken for rates and means "
                "only (a count's change is new count - old count, from the metrics file)",
            )
        first_line.setdefault(key, i)
        if case_id is not None:
            if (key, case_id, trial) in seen:
                raise _bad(where, "repeats an earlier line (same case, trial, metric and labels)")
            seen.add((key, case_id, trial))
            if with_trial.setdefault((key, case_id), trial is not None) != (trial is not None):
                raise _bad(where, "one case has rows with and without trial for this metric")
        out.append(CaseValue(metric, dict(labels), v, case_id, trial))
    _check_aggregates(out, metrics, path.name, source, first_line)
    return tuple(out)


def _agrees(a: float, b: float) -> bool:
    """Equal within the metrics-1 rate tolerance (1e-6 relative, 5e-7 absolute)."""
    return math.isclose(a, b, rel_tol=RATE_REL_TOL, abs_tol=RATE_ABS_TOL)


def _check_aggregates(
    rows: Sequence[CaseValue],
    metrics: MetricsSummary,
    name: str,
    source: str,
    first_line: Mapping[Key, int],
) -> None:
    """Every (metric, labels) the sidecar covers must average to its value in the metrics
    file: the mean of the per-case means (trials averaged within a case; an id-less row is
    one case), or the mean over every line (the two differ only with unequal trial counts).
    A sidecar that contradicts its summary is refused, never compared."""
    values = {(m.name, m.label_key()): m.value for m in metrics.metrics}
    per_case: dict[Key, dict[str, list[float]]] = {}
    anon: dict[Key, list[float]] = {}
    for c in rows:
        if c.case_id is None:
            anon.setdefault(c.key(), []).append(c.value)
        else:
            per_case.setdefault(c.key(), {}).setdefault(c.case_id, []).append(c.value)
    for key in sorted(per_case.keys() | anon.keys(), key=lambda k: first_line[k]):
        groups = [*per_case.get(key, {}).values(), *([v] for v in anon.get(key, []))]
        flat = [v for g in groups for v in g]
        target = float(values[key])
        case_mean = _mean([_mean(g) for g in groups])
        if not (_agrees(case_mean, target) or _agrees(_mean(flat), target)):
            raise _bad(
                f"{name} line {first_line[key]}",
                f"the per-case values of {safe_text(key[0])} (this line's labels) do not average "
                f"to its value in {source} (within 1e-6): the sidecar contradicts the summary",
            )


def _scalar_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, sort_keys=True)


def _read_settings(path: Path) -> dict[str, str | None]:
    """An optional flat `settings.json` of recorded settings (rules hash, bar, ...)."""
    if not path.is_file():
        return {}
    data = loads_json(_read_text(path))
    if not isinstance(data, dict):
        raise CompareError([f"{path.name}: a flat JSON object of recorded settings"])
    out: dict[str, str | None] = {}
    for k, v in data.items():
        if not k.strip():
            raise CompareError([f"{path.name}: setting names must not be blank"])
        flat = v is None or isinstance(v, str | int | float | bool)
        if isinstance(v, list):
            flat = all(x is None or isinstance(x, str | int | float | bool) for x in v)
        if not flat:
            raise CompareError([f"{path.name}: {safe_text(k)} must be a value or a list of values"])
        out[k] = _scalar_text(v)
    return out


def _merge_settings(base: dict[str, str | None], extra: Mapping[str, str | None]) -> None:
    for k, v in extra.items():
        if base.get(k) is None:
            base[k] = v
        elif v is not None and base[k] != v:
            raise CompareError(
                [f"settings.json: {safe_text(k)} differs from the value the results record"]
            )


def _metrics_settings(m: MetricsSummary) -> dict[str, str | None]:
    models = sorted({canonical(x.labels["model"]) for x in m.metrics if "model" in x.labels})
    pv = m.provenance
    return {
        "project": m.project,
        "simulated": "true" if m.simulated else "false",
        "snapshot": pv.snapshot,
        "code_version": pv.code_version,
        "config_hash": pv.config_hash,
        "evalkit": pv.evalkit,
        "models": ", ".join(models) or None,
    }


def _distinct(values: Iterable[str]) -> str | None:
    vals = sorted({v for v in values if v})
    return ", ".join(vals) if vals else None


def _load_run(summary_path: Path) -> RunInput:
    """An EvalKit run folder: metrics as `evalkit gate` reads them, per-case values from
    records.jsonl (one value per case and model: no trial level)."""
    from evalkit.gates import _load_summary
    from evalkit.schemas import Record

    metrics, status = _load_summary(summary_path)
    folder = summary_path.parent
    run: dict[str, Any] = {}
    if (folder / "run.json").is_file():
        data = loads_json(_read_text(folder / "run.json"))
        run = data if isinstance(data, dict) else {}
    records: list[Record] | None = None
    rec_path = folder / "records.jsonl"
    if rec_path.is_file():
        try:
            lines = _read_text(rec_path).splitlines()
            records = [Record.model_validate_json(x) for x in lines if x.strip()]
        except ValidationError:
            raise CompareError([f"{rec_path} is not a valid records file"]) from None
    settings = _metrics_settings(metrics)
    if isinstance(run.get("models"), list):
        settings["models"] = ", ".join(sorted(str(x) for x in run["models"]))
    for key in ("judge_mode", "seed"):
        settings[key] = _scalar_text(run.get(key))
    if isinstance(run.get("judges"), list):
        settings["judges"] = ", ".join(str(x) for x in run["judges"])
    summary = loads_json(_read_text(summary_path))
    settings["results_format"] = _scalar_text(summary.get("schema_version"))
    cases = None
    if records is not None:
        settings["code_version"] = _distinct(r.git_sha for r in records)
        settings["prompt_hash"] = _distinct(r.prompt_hash for r in records)
        # one setting per model, so each provider snapshot is shown (a joined list of every
        # model's snapshot would pass the 64-character limit and be hidden)
        snapshots: dict[str, set[str]] = {}
        for r in records:
            if r.completion.model_snapshot:
                snapshots.setdefault(r.completion.model, set()).add(r.completion.model_snapshot)
        for model, snaps in sorted(snapshots.items()):
            settings[f"model_ids.{model}"] = _distinct(snaps)
        settings["judge_prompt_hash"] = _distinct(
            v.judge_prompt_hash for r in records for v in r.verdicts
        )
        settings["extractor_version"] = _distinct(
            c.extractor_version for r in records for c in r.claims
        )
        slices = [str(s) for s in run.get("slices", []) if isinstance(s, str)]
        complete = status.judging == "complete"
        cases = _record_values(records, folder, slices, complete, metrics)
    _merge_settings(settings, _read_settings(folder / "settings.json"))
    state = {
        (False, "complete"): "complete",
        (False, "pending"): "judging pending",
    }.get((status.stopped_at_cost_cap, status.judging), "stopped at the cost cap")
    return RunInput(str(summary_path), "evalkit-run", metrics, cases, settings, state)


def _record_values(
    records: Sequence[Any],
    folder: Path,
    slices: Sequence[str],
    complete: bool,
    metrics: MetricsSummary,
) -> tuple[CaseValue, ...]:
    """Per-case values for the metrics `from_run_summary` writes as means over cases: accuracy,
    citation validity, coverage, task scores and (judging complete) headline and groundedness
    per judge, per model and per slice; and per model the efficiency block's cost per case
    and error rate (labelled as the summary labels them). Ratio figures (citation precision,
    aim-citation rate, cost per correct answer), latency percentiles and token means are
    compared from their intervals."""
    from evalkit.metrics import _slice_label

    # the efficiency rows' labels per model (model, mode and the price date when known)
    eff_labels: dict[tuple[str, str], dict[str, Any]] = {}
    for m in metrics.metrics:
        keys = set(m.labels)
        if m.name in ("cost_per_case", "error_rate") and {"model", "mode"} <= keys <= _EFF_KEYS:
            eff_labels[(m.name, str(m.labels["model"]))] = dict(m.labels)

    case_slices: dict[str, dict[str, str]] = {}
    if slices:
        from evalkit.pipeline.aggregate import slice_value
        from evalkit.schemas import Case

        try:
            lines = _read_text(folder / "cases.jsonl").splitlines()
            defs = [Case.model_validate_json(x) for x in lines if x.strip()]
            case_slices = {c.id: {f: slice_value(c, f) for f in slices} for c in defs}
        except (ValidationError, ValueError):
            raise CompareError([f"{folder / 'cases.jsonl'} cannot give the run's slices"]) from None
    out: list[CaseValue] = []
    seen: set[tuple[str, str]] = set()
    for r in records:
        model = r.completion.model
        if (model, r.case_id) in seen:
            raise CompareError(
                [f"{folder / 'records.jsonl'}: a case is recorded twice for a model"]
            )
        seen.add((model, r.case_id))
        sc = r.scores
        if sc is None:
            raise CompareError([f"{folder / 'records.jsonl'}: a record has not been scored"])
        values: list[tuple[str, dict[str, Any], float]] = [
            ("accuracy", {}, float(sc.label_correct)),
            ("citation_validity", {}, sc.citation_valid),
            ("coverage", {}, sc.coverage),
        ]
        if complete:
            for j, ok in sc.correct_and_grounded.items():
                values.append(("headline", {"judge": j}, float(ok)))
            for j, g in sc.groundedness.items():
                values.append(("groundedness", {"judge": j}, g))
        for name, ts in r.task_scores.items():
            if NAME_RE.match(f"task_scores.{name}"):
                values.append((f"task_scores.{name}", {}, ts.value))
        groups: list[dict[str, Any]] = [{"model": model}]
        if r.case_id in case_slices:
            for f, v in case_slices[r.case_id].items():
                groups.append({"model": model, _slice_label(f): v})
        for base in groups:
            for metric, extra, value in values:
                out.append(CaseValue(metric, {**base, **extra}, value, r.case_id))
        for name, value in (
            ("cost_per_case", r.completion.cost_usd),
            ("error_rate", 1.0 if r.completion.error is not None else 0.0),
        ):
            labels = eff_labels.get((name, model))
            if labels is not None:
                out.append(CaseValue(name, labels, value, r.case_id))
    return tuple(out)


# ---- statistics -------------------------------------------------------------------------------


class _Cache:
    def __init__(self, seed: int, resamples: int) -> None:
        self.seed, self.resamples = seed, resamples
        self._rs: dict[int, Resamples] = {}

    def rs(self, n: int) -> Resamples:
        if n not in self._rs:
            self._rs[n] = Resamples(n, self.resamples, seed=self.seed)
        return self._rs[n]


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _check_values(groups: Iterable[Sequence[float]]) -> None:
    for g in groups:
        if not g:
            raise ValueError("every case needs at least one value")
        if not all(math.isfinite(x) for x in g):
            raise ValueError("values must be finite")


def _paired(
    old: Mapping[str, Sequence[float]],
    new: Mapping[str, Sequence[float]],
    key: str,
    cache: _Cache,
) -> tuple[Interval, float, str]:
    ids = sorted(set(old) & set(new))
    if not ids:
        raise ValueError("a paired comparison needs shared case ids")
    o = [list(old[c]) for c in ids]
    w = [list(new[c]) for c in ids]
    _check_values(o + w)
    n = len(ids)
    rs = cache.rs(n)
    if all(len(x) == 1 for x in o + w):
        iv, p = paired([x[0] for x in w], [x[0] for x in o], rs)
        return iv, p, CASE_ONLY
    point = math.fsum(_mean(b) - _mean(a) for a, b in zip(o, w, strict=True)) / n
    draw = random.Random(f"evalkit-compare-trials:{cache.seed}:{key}").random
    boot = []
    for row in rs.rows:
        total = 0.0
        for i in row:
            a, b = o[i], w[i]
            ka, kb = len(a), len(b)
            sa = a[0] if ka == 1 else math.fsum([a[int(draw() * ka)] for _ in range(ka)]) / ka
            sb = b[0] if kb == 1 else math.fsum([b[int(draw() * kb)] for _ in range(kb)]) / kb
            total += sb - sa
        boot.append(total / n)
    big = len(boot) + 1
    le = (sum(d <= 0 for d in boot) + 1) / big
    ge = (sum(d >= 0 for d in boot) + 1) / big
    return interval_from(point, boot), min(1.0, 2 * min(le, ge)), TWO_LEVEL


def paired_bootstrap(
    old: Mapping[str, Sequence[float]],
    new: Mapping[str, Sequence[float]],
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    key: str = "",
) -> tuple[Interval, float, str]:
    """mean(new - old) over the case ids both map, each case's values being its trials. Returns
    the 95% percentile interval, a two-sided bootstrap p-value for no change (+1 correction)
    and the method. Two-level (resample cases, then each run's trials within a case,
    D-41/D-51) when any shared case has more than one value; otherwise a case-only resample,
    exactly `evalkit.stats.paired` over `Resamples(n, resamples, seed)`. `key` salts the
    trial draws (one comparison passes the row's metric and labels)."""
    return _paired(old, new, key, _Cache(seed, resamples))


def _one_side(groups: Sequence[Sequence[float]], key: str, cache: _Cache) -> Interval:
    """One run's mean over its cases with a bootstrap interval (two-level with trials)."""
    _check_values(groups)
    n = len(groups)
    rs = cache.rs(n)
    if all(len(g) == 1 for g in groups):
        return interval([g[0] for g in groups], rs)
    point = math.fsum(_mean(g) for g in groups) / n
    draw = random.Random(f"evalkit-compare-side:{cache.seed}:{key}").random
    boot = []
    for row in rs.rows:
        total = 0.0
        for i in row:
            g = groups[i]
            k = len(g)
            total += g[0] if k == 1 else math.fsum([g[int(draw() * k)] for _ in range(k)]) / k
        boot.append(total / n)
    return interval_from(point, boot)


def _mover(old: Interval, new: Interval) -> Interval:
    """MOVER (Zou & Donner 2008): an interval for new - old from two independent intervals."""
    d = new.point - old.point
    lo = d - math.sqrt((new.point - new.lo) ** 2 + (old.hi - old.point) ** 2)
    hi = d + math.sqrt((new.hi - new.point) ** 2 + (old.point - old.lo) ** 2)
    return Interval(point=d, lo=min(lo, d), hi=max(hi, d))


def _normal_p(iv: Interval) -> float:
    """Two-sided p-value for no change from the half-width on the side of zero."""
    d = iv.point
    if d == 0:
        return 1.0
    half = d - iv.lo if d > 0 else iv.hi - d
    if half <= 0:
        return 0.0
    return min(1.0, math.erfc(abs(d) / (half / Z975) / math.sqrt(2)))


def _holm_adjusted(ps: Mapping[int, float]) -> dict[int, float]:
    order = sorted(ps, key=lambda k: (ps[k], str(k)))
    m, running, out = len(order), 0.0, {}
    for rank, k in enumerate(order):
        running = max(running, min(1.0, (m - rank) * ps[k]))
        out[k] = running
    return out


# ---- results ----------------------------------------------------------------------------------


@dataclass
class Change:
    """One (metric, labels) present in both runs."""

    metric: str
    labels: dict[str, str]  # as shown: long values hidden
    unit: str
    old: bool | int | float | None
    new: bool | int | float | None
    diff: int | float | None  # new - old (paired: over the shared cases)
    ci_low: float | None
    ci_high: float | None
    pairing: Literal["paired", "unpaired"]
    method: str
    interval_sources: dict[str, str | None] | None = None  # unpaired: where each interval came from
    n_shared: int | None = None  # paired, or partial overlap: shared case ids
    not_paired: dict[str, int] | None = None  # per-case units outside the shared set
    # "ok": a claim may be made (Holm, flag, exit 1); "too_few_cases" or "degenerate" (a
    # zero-width interval): shown, never claimed; None: no interval
    interval_status: str | None = None
    partial_overlap: bool = False  # both runs carry case ids, too few shared: compared unpaired
    n_old: int | None = None  # cases (per-case units, or the metric's n)
    n_new: int | None = None
    p_value: float | None = None
    p_holm: float | None = None
    flagged: bool = False
    gate: Literal["fail", "warn"] | None = None
    regression: bool = False
    regression_severity: Literal["fail", "warn"] | None = None
    heldout_rows: int = 0
    note: str = ""

    @property
    def direction(self) -> str | None:
        if self.diff is None:
            return None
        return "up" if self.diff > 0 else "down" if self.diff < 0 else "none"

    def as_dict(self) -> dict[str, Any]:
        out = {
            "metric": self.metric,
            "labels": self.labels,
            "unit": self.unit,
            "old": self.old,
            "new": self.new,
            "diff": self.diff,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "direction": self.direction,
            "pairing": self.pairing,
            "method": self.method,
            "interval_sources": self.interval_sources,
            "n_shared": self.n_shared,
            "not_paired": self.not_paired,
            "interval_status": self.interval_status,
            "partial_overlap": self.partial_overlap,
            "n_old": self.n_old,
            "n_new": self.n_new,
            "p_value": self.p_value,
            "p_holm": self.p_holm,
            "flagged": self.flagged,
            "gate": self.gate,
            "regression": self.regression,
            "regression_severity": self.regression_severity,
            "note": self.note,
        }
        if self.heldout_rows:
            out["heldout_rows"] = self.heldout_rows
        return out


@dataclass(frozen=True)
class Missing:
    """A metric (or a label combination of it) in one run only."""

    side: Literal["old", "new"]  # the run it is missing from
    metric: str
    labels: dict[str, str]
    what: Literal["metric", "labels"]  # the name is absent, or only these labels
    heldout_rows: int = 0  # held-out: how many rows of this split (their labels hidden)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "side": self.side,
            "metric": self.metric,
            "labels": self.labels,
            "what": self.what,
        }
        if self.heldout_rows:
            out["heldout_rows"] = self.heldout_rows
        return out


@dataclass(frozen=True)
class SettingDifference:
    setting: str
    old: str | None
    new: str | None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"setting": self.setting, "old": self.old, "new": self.new, "note": self.note}


@dataclass
class Comparison:
    exit_code: int
    verdict: str
    old: dict[str, Any]
    new: dict[str, Any]
    seed: int = DEFAULT_SEED
    resamples: int = DEFAULT_RESAMPLES
    settings: list[SettingDifference] = field(default_factory=list)
    settings_status: str = "same"
    changes: list[Change] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    holm_family: int = 0
    gates: str | None = None

    @classmethod
    def error(
        cls, errors: Sequence[str], old: Mapping[str, Any], new: Mapping[str, Any], **kw: Any
    ) -> Comparison:
        return cls(
            EXIT_INPUT, VERDICTS[EXIT_INPUT], dict(old), dict(new), errors=list(errors), **kw
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COMPARE_SCHEMA,
            "exit_code": self.exit_code,
            "verdict": self.verdict,
            "seed": self.seed,
            "resamples": self.resamples,
            "holm": {"alpha": ALPHA, "family": self.holm_family},
            "gates": self.gates,
            "old": self.old,
            "new": self.new,
            "settings": {
                "status": self.settings_status,
                "differences": [d.as_dict() for d in self.settings],
            },
            "changes": [c.as_dict() for c in self.changes],
            "missing": [m.as_dict() for m in self.missing],
            "flagged": sum(1 for c in self.changes if c.flagged),
            "regressions": sum(1 for c in self.changes if c.regression),
            "errors": list(self.errors),
        }


# ---- the comparison ---------------------------------------------------------------------------


@dataclass
class _Cases:
    ids: dict[str, list[float]] = field(default_factory=dict)  # pairable case -> trial values
    held: dict[str, list[float]] = field(default_factory=dict)  # manifest held-out: never paired
    anon: list[float] = field(default_factory=list)  # rows without a case id: one unit each
    trials: dict[str, list[tuple[int, float]]] = field(default_factory=dict)

    def units(self) -> list[list[float]]:
        return [*self.ids.values(), *self.held.values(), *([v] for v in self.anon)]


def _group(cases: Sequence[CaseValue] | None, held_hashes: frozenset[str]) -> dict[Key, _Cases]:
    from evalkit.heldout import case_id_hash

    out: dict[Key, _Cases] = {}
    for c in cases or ():
        g = out.setdefault(c.key(), _Cases())
        if c.case_id is None:
            g.anon.append(c.value)
            continue
        target = g.held if held_hashes and case_id_hash(c.case_id) in held_hashes else g.ids
        target.setdefault(c.case_id, []).append(c.value)
        if c.trial is not None:  # keep trial order, whatever the line order
            g.trials.setdefault(c.case_id, []).append((c.trial, c.value))
    for g in out.values():
        for cid, pairs in g.trials.items():
            vals = [v for _, v in sorted(pairs, key=lambda tv: tv[0])]
            (g.held if cid in g.held else g.ids)[cid] = vals
    return out


def _manifest_hashes(manifest: Mapping[str, Any], held: frozenset[str]) -> frozenset[str]:
    splits = manifest.get("splits")
    if not isinstance(splits, Mapping):
        raise CompareError(["manifest: no splits (a B5.14 held-out manifest is expected)"])
    out: set[str] = set()
    found = False
    for name, entries in splits.items():
        if canonical(name) not in held:
            continue
        found = True
        if not isinstance(entries, list):
            raise CompareError([f"manifest: split {safe_text(name)} is not a list of entries"])
        for e in entries:
            h = e.get("id_sha256") if isinstance(e, Mapping) else None
            if not isinstance(h, str):
                raise CompareError(
                    [f"manifest: split {safe_text(name)} has an entry without id_sha256"]
                )
            out.add(h)
    if not found:
        raise CompareError([f"manifest: no held-out split ({', '.join(sorted(held))})"])
    return frozenset(out)


def _binary_exact(units: Sequence[Sequence[float]], m: Metric) -> Interval | None:
    """Clopper-Pearson from per-case 0/1 values of a rate (one value per case): exact at any n,
    where a bootstrap of identical values, or of a few, would claim certainty."""
    if m.unit != "rate" or not units:
        return None
    if not all(len(u) == 1 and u[0] in (0.0, 1.0) for u in units):
        return None
    k, n = round(math.fsum(u[0] for u in units)), len(units)
    cp = clopper_pearson(k, n)
    point = k / n
    return Interval(point=point, lo=min(cp.lo, point), hi=max(cp.hi, point))


def _side_interval(
    m: Metric, cs: _Cases | None, key: str, cache: _Cache
) -> tuple[Interval | None, str | None, str]:
    """One run's 95% interval for a row, with its source and status. With per-case values from
    at least MIN_CASES cases, their bootstrap (exact Clopper-Pearson over the cases instead when
    the bootstrap has zero width and the values are 0/1). Otherwise the supplied interval, else
    exact Clopper-Pearson from k (or a whole-number value x n) and n, else Clopper-Pearson over
    0/1 per-case values; failing all of those, a bootstrap of too few cases (`too_few_cases`)."""
    if unit_kind(m.unit) == "bool" or isinstance(m.value, bool):
        return None, None, OK
    units = cs.units() if cs is not None else []
    if len(units) >= MIN_CASES:
        iv = _one_side(units, key, cache)
        if iv.lo < iv.hi:
            return iv, "bootstrap", OK
        exact = _binary_exact(units, m)
        if exact is not None:
            return exact, "clopper-pearson (cases)", OK
        return iv, "bootstrap", DEGENERATE
    summary = _summary_interval(m)
    if summary is not None:
        return summary
    exact = _binary_exact(units, m)
    if exact is not None:
        return exact, "clopper-pearson (cases)", OK
    if units:
        return _one_side(units, key, cache), "bootstrap", TOO_FEW
    return None, None, OK


def _summary_interval(m: Metric) -> tuple[Interval, str, str] | None:
    """The supplied interval, else exact Clopper-Pearson from k (or value x n) and n."""
    value = float(m.value)
    if m.ci_low is not None and m.ci_high is not None:
        iv = Interval(point=value, lo=min(m.ci_low, value), hi=max(m.ci_high, value))
        return iv, "supplied", OK
    if m.unit == "rate" and m.n is not None and m.n >= 1:
        k = m.k
        if k is None and abs(value * m.n - round(value * m.n)) < 1e-6:
            k = round(value * m.n)
        if k is not None and 0 <= k <= m.n:
            cp = clopper_pearson(k, m.n)
            iv = Interval(point=value, lo=min(cp.lo, value), hi=max(cp.hi, value))
            return iv, "clopper-pearson", OK
    return None


def _is_heldout(labels: Mapping[str, Any], held: frozenset[str]) -> bool:
    return "split" in labels and canonical(labels["split"]) in held


def _gate_matches(gate: Any, name: str, labels: Mapping[str, Any]) -> bool:
    from evalkit.gates import _matches

    if gate.metric != name or not _matches(labels, gate.labels):
        return False
    return not gate.disparity or gate.disparity in labels


def _bad_direction(op: str, diff: float) -> bool:
    if op == ">=":
        return diff < 0
    if op == "<=":
        return diff > 0
    return diff != 0


def compare(
    old: RunInput,
    new: RunInput,
    *,
    gates: GatesFile | None = None,
    manifest: Mapping[str, Any] | None = None,
    heldout_splits: Sequence[str | int | bool] = (),
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> Comparison:
    """Compare two loaded runs (see the module docstring). Input problems are errors in the
    result (exit 2), never exceptions; held-out rows are folded to split level."""
    held = frozenset(
        {DEFAULT_HELDOUT}
        | {canonical(v) for v in heldout_splits}
        | {canonical(v) for v in (gates.heldout_splits if gates else ())}
    )
    base: dict[str, Any] = {"seed": seed, "resamples": resamples}
    if resamples < 100:
        return Comparison.error(["use at least 100 resamples"], old.source(), new.source(), **base)
    try:
        held_hashes = _manifest_hashes(manifest, held) if manifest is not None else frozenset()
    except CompareError as exc:
        return Comparison.error(list(exc.problems), old.source(), new.source(), **base)
    cache = _Cache(seed, resamples)
    o_rows = {(m.name, m.label_key()): m for m in old.metrics.metrics}
    n_rows = {(m.name, m.label_key()): m for m in new.metrics.metrics}
    o_cases = _group(old.cases, held_hashes) if old.cases is not None else None
    n_cases = _group(new.cases, held_hashes) if new.cases is not None else None
    o_names = {k[0] for k in o_rows}
    n_names = {k[0] for k in n_rows}
    quoted = _mixed_keys(old.metrics, new.metrics)
    errors: list[str] = []
    changes: dict[Key, Change] = {}
    shared_of: dict[Key, int | None] = {}  # paired-eligible rows: shared ids (even < 2)
    missing: list[tuple[Key, Missing]] = []
    is_held: dict[Key, bool] = {}
    for key in sorted(o_rows.keys() | n_rows.keys(), key=lambda k: (k[0], repr(k[1]))):
        mo, mn = o_rows.get(key), n_rows.get(key)
        any_m = mo or mn
        assert any_m is not None
        is_held[key] = _is_heldout(any_m.labels, held)
        if mo is None or mn is None:
            side: Literal["old", "new"] = "old" if mo is None else "new"
            names = o_names if side == "old" else n_names
            what: Literal["metric", "labels"] = "labels" if key[0] in names else "metric"
            shown = _shown(any_m.labels, quoted)
            missing.append((key, Missing(side, safe_text(key[0]), shown, what)))
            continue
        if mo.unit != mn.unit:
            errors.append(
                f"{safe_text(key[0])} "
                f"{_labels_text(_shown_held(mo.labels, is_held[key], quoted))}: "
                f"the unit differs between the runs ({mo.unit} vs {mn.unit})"
            )
            continue
        changes[key] = _change(
            key, mo, mn, o_cases, n_cases, is_held[key], cache, shared_of, quoted
        )
    family = _flag(changes)
    if gates is not None:
        errors += _baseline_errors(gates, o_rows, n_rows, changes, shared_of, is_held, quoted)
        _mark_gated(gates, changes, o_rows)
    out_changes = _fold([(k, c) for k, c in changes.items()], is_held)
    out_missing = _fold_missing(missing, is_held)
    # a run folder is written by EvalKit, so its version is a setting even when unrecorded
    always = ("evalkit",) if "evalkit-run" in (old.kind, new.kind) else ()
    settings, status = _settings_diff(old.settings, new.settings, always)
    if errors:
        code = EXIT_INPUT
    elif any(c.regression_severity == "fail" for c in changes.values()):
        code = EXIT_REGRESSION
    else:
        code = EXIT_PASS
    return Comparison(
        exit_code=code,
        verdict=VERDICTS[code],
        old=old.source(),
        new=new.source(),
        seed=seed,
        resamples=resamples,
        settings=settings,
        settings_status=status,
        changes=out_changes,
        missing=out_missing,
        errors=errors,
        holm_family=family,
    )


def _units_n(cs: _Cases | None, m: Metric) -> int | None:
    if cs is not None:
        return len(cs.units())
    return m.n


def _change(
    key: Key,
    mo: Metric,
    mn: Metric,
    o_cases: dict[Key, _Cases] | None,
    n_cases: dict[Key, _Cases] | None,
    heldout: bool,
    cache: _Cache,
    shared_of: dict[Key, int | None],
    quoted: frozenset[str] = frozenset(),
) -> Change:
    co = o_cases.get(key) if o_cases is not None else None
    cn = n_cases.get(key) if n_cases is not None else None
    salt = f"{key[0]}|{key[1]!r}"
    row = Change(
        metric=safe_text(key[0]),
        labels=_shown(mo.labels, quoted),
        unit=mo.unit,
        old=mo.value,
        new=mn.value,
        diff=None,
        ci_low=None,
        ci_high=None,
        pairing="unpaired",
        method=NO_INTERVAL,
        n_old=_units_n(co, mo),
        n_new=_units_n(cn, mn),
    )
    is_bool = unit_kind(mo.unit) == "bool"
    if is_bool:
        row.note = "true/false: changed" if mo.value != mn.value else "true/false: unchanged"
        return row
    overlap = ""
    if not heldout and co is not None and cn is not None and co.ids and cn.ids:
        shared = sorted(set(co.ids) & set(cn.ids))
        k, uo, un = len(shared), len(co.units()), len(cn.units())
        shared_of[key] = k
        row.n_shared, row.not_paired = k, {"old": uo - k, "new": un - k}
        if k >= 2 and k >= MIN_OVERLAP_SHARE * uo and k >= MIN_OVERLAP_SHARE * un:
            iv, p, method = _paired(
                {c: co.ids[c] for c in shared}, {c: cn.ids[c] for c in shared}, salt, cache
            )
            row.pairing, row.method = "paired", method
            row.diff, row.ci_low, row.ci_high, row.p_value = iv.point, iv.lo, iv.hi, p
            if k < MIN_CASES:
                row.interval_status = TOO_FEW
                row.note = f"too few cases: {k} shared (a claim needs {MIN_CASES})"
            else:
                # with enough shared cases a zero-width interval means every case moved by the
                # same amount: the strongest evidence, not a degenerate one (e.g. every case
                # going from right to wrong must be flagged)
                row.interval_status = OK
                if iv.lo == iv.hi:
                    row.note = (
                        f"unchanged in all {k} shared cases"
                        if iv.point == 0
                        else f"every one of the {k} shared cases moved by the same amount"
                    )
            return row
        row.partial_overlap = True
        overlap = (
            f"partial overlap: {k} shared case(s) of {uo} old and {un} new (pairing needs at "
            f"least half of each); compared unpaired; {uo - k} old and {un - k} new case(s) "
            "without a partner"
        )
    so, src_o, st_o = _side_interval(mo, co, f"{salt}|old", cache)
    sn, src_n, st_n = _side_interval(mn, cn, f"{salt}|new", cache)
    row.interval_sources = {"old": src_o, "new": src_n}
    notes = [overlap] if overlap else []
    if so is None or sn is None:
        row.diff = mn.value - mo.value  # type: ignore[operator]
        notes.append(
            "no interval on "
            + (
                "either side"
                if so is None and sn is None
                else "the old side"
                if so is None
                else "the new side"
            )
        )
        row.note = "; ".join(notes)
        return row
    iv = _mover(so, sn)
    row.method = MOVER
    row.diff, row.ci_low, row.ci_high, row.p_value = iv.point, iv.lo, iv.hi, _normal_p(iv)
    if heldout:
        notes.append("held-out: never paired")
    if TOO_FEW in (st_o, st_n):
        row.interval_status = TOO_FEW
        notes.append(f"too few cases: a run's per-case interval needs {MIN_CASES} cases")
    elif DEGENERATE in (st_o, st_n) or iv.lo == iv.hi:
        row.interval_status = DEGENERATE
        notes.append("degenerate: a zero-width interval (identical values) is never a claim")
    else:
        row.interval_status = OK
    row.note = "; ".join(notes)
    return row


def _flag(changes: Mapping[Key, Change]) -> int:
    """Holm across every row whose interval may carry a claim (`interval_status` ok: enough
    cases, not of zero width); flagged = rejected and the interval excludes 0. Rows that are
    too small or degenerate keep their p-value but take no part in Holm and are never
    flagged."""
    keys = list(changes)
    clean: dict[int, float] = {}
    for i, k in enumerate(keys):
        p = changes[k].p_value
        if p is not None and changes[k].interval_status == OK:
            clean[i] = p
    rejected = holm(clean)
    adjusted = _holm_adjusted(clean)
    for i in clean:
        c = changes[keys[i]]
        c.p_holm = adjusted[i]
        lo, hi = c.ci_low, c.ci_high
        excludes = lo is not None and hi is not None and (lo > 0 or hi < 0)
        c.flagged = bool(rejected[i] and excludes)
    return len(clean)


def _labels_text(labels: Mapping[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items()) or "(no labels)"


def _shown_held(
    labels: Mapping[str, Any], heldout: bool, quoted: frozenset[str] = frozenset()
) -> dict[str, str]:
    shown = _shown(labels, quoted)
    return {k: v for k, v in shown.items() if k in KEPT_HELDOUT} if heldout else shown


def _baseline_errors(
    gates: GatesFile,
    o_rows: Mapping[Key, Metric],
    n_rows: Mapping[Key, Metric],
    changes: Mapping[Key, Change],
    shared_of: Mapping[Key, int | None],
    is_held: Mapping[Key, bool],
    quoted: frozenset[str] = frozenset(),
) -> list[str]:
    """RVR-03: a baseline names every gated metric, with at least 2 shared cases."""
    out: list[str] = []
    for gi, g in enumerate(gates.gates, start=1):
        where = f"gate {gi} ({safe_text(g.metric)})"
        om = {k for k, m in o_rows.items() if _gate_matches(g, m.name, m.labels)}
        nm = {k for k, m in n_rows.items() if _gate_matches(g, m.name, m.labels)}
        if not om:
            out.append(f"{where}: the baseline (old run) has no metric matching this gate")
        if not nm:
            out.append(f"{where}: the new run has no metric matching this gate")
        seen: set[str] = set()
        for k in sorted(om ^ nm, key=lambda k: (k[0], repr(k[1]))):
            m = o_rows.get(k) or n_rows[k]
            side = "baseline (old run)" if k in nm else "new run"
            shown = _labels_text(_shown_held(m.labels, is_held[k], quoted))
            msg = f"{where}: {shown} is missing from the {side}"
            if msg not in seen:
                seen.add(msg)
                out.append(msg)
        for k in sorted(om & nm, key=lambda k: (k[0], repr(k[1]))):
            c = changes.get(k)
            if c is None:  # a unit mismatch: already an error
                continue
            shown = _labels_text(_shown_held(o_rows[k].labels, is_held[k], quoted))
            shared = shared_of.get(k)
            if shared is not None and shared < 2:
                out.append(
                    f"{where}: {shown}: only {shared} shared case(s) "
                    "(a baseline needs at least 2 for every gated metric)"
                )
            elif shared is None:
                for side, n in (("old", c.n_old), ("new", c.n_new)):
                    if n is not None and n < 2:
                        msg = (
                            f"{where}: {shown}: n = {n} on the {side} side "
                            "(a baseline needs at least 2)"
                        )
                        if msg not in seen:
                            seen.add(msg)
                            out.append(msg)
    return out


def _mark_gated(
    gates: GatesFile, changes: Mapping[Key, Change], rows: Mapping[Key, Metric]
) -> None:
    rank = {"warn": 1, "fail": 2}
    for key, c in changes.items():
        m = rows[key]
        for g in gates.gates:
            if not _gate_matches(g, m.name, m.labels):
                continue
            if c.gate is None or rank[g.severity] > rank[c.gate]:
                c.gate = g.severity
            if g.disparity or not c.flagged or c.diff is None or isinstance(g.value, bool):
                continue  # a disparity gap's direction is not one row's change
            if _bad_direction(g.op, float(c.diff)):
                c.regression = True
                if c.regression_severity is None or rank[g.severity] > rank[c.regression_severity]:
                    c.regression_severity = g.severity


def _fold(items: Sequence[tuple[Key, Change]], is_held: Mapping[Key, bool]) -> list[Change]:
    """Held-out rows become one split-level row per metric, split, model and judge; a group of
    one keeps its figures (it is already a split-level aggregate), a larger group shows only
    its row count, how many were flagged and whether any regressed."""
    out: list[Change] = []
    groups: dict[tuple[str, tuple[tuple[str, str], ...]], list[Change]] = {}
    order: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for key, c in items:
        if not is_held[key]:
            out.append(c)
            continue
        kept = tuple((k, v) for k, v in c.labels.items() if k in KEPT_HELDOUT)
        gk = (c.metric, kept)
        if gk not in groups:
            groups[gk] = []
            order.append(gk)
            out.append(c)  # placeholder, replaced below
        groups[gk].append(c)
    placed = {id(groups[gk][0]): gk for gk in order}
    final: list[Change] = []
    for c in out:
        gk = placed.get(id(c))
        if gk is None:
            final.append(c)
            continue
        members = groups[gk]
        if len(members) == 1 and set(c.labels) <= set(KEPT_HELDOUT):
            c.heldout_rows = 1  # already a split-level aggregate: it keeps its figures
            final.append(c)
            continue
        # a row with any other label (a category, a topic) is never shown under the split's
        # name, even alone: its figures are one slice of the split, not the split
        flagged = sum(1 for m in members if m.flagged)
        sev = [m.regression_severity for m in members if m.regression_severity]
        gate = [m.gate for m in members if m.gate]
        final.append(
            Change(
                metric=c.metric,
                labels=dict(gk[1]),
                unit=c.unit,
                old=None,
                new=None,
                diff=None,
                ci_low=None,
                ci_high=None,
                pairing="unpaired",
                method="held-out split: rows folded",
                flagged=flagged > 0,
                gate="fail" if "fail" in gate else "warn" if gate else None,
                regression=any(m.regression for m in members),
                regression_severity="fail" if "fail" in sev else "warn" if sev else None,
                heldout_rows=len(members),
                note=f"held-out split: {len(members)} row(s) compared, {flagged} flagged",
            )
        )
    return final


def _fold_missing(
    items: Sequence[tuple[Key, Missing]], is_held: Mapping[Key, bool]
) -> list[Missing]:
    """Held-out rows in one run only are listed at split level with how many there are, so a
    split compared in part and missing in part reads as such, never as a contradiction."""
    out: list[Missing] = []
    held_at: dict[tuple[str, str, tuple[tuple[str, str], ...], str], int] = {}
    for key, m in items:
        if not is_held[key]:
            if m not in out:
                out.append(m)
            continue
        kept = {k: v for k, v in m.labels.items() if k in KEPT_HELDOUT}
        gk = (m.side, m.metric, tuple(kept.items()), m.what)
        if gk in held_at:
            i = held_at[gk]
            out[i] = Missing(m.side, m.metric, kept, m.what, out[i].heldout_rows + 1)
        else:
            held_at[gk] = len(out)
            out.append(Missing(m.side, m.metric, kept, m.what, 1))
    return out


def _maybe_text(v: str | None) -> str | None:
    return None if v is None else safe_text(v)


def _settings_diff(
    old: Mapping[str, str | None],
    new: Mapping[str, str | None],
    always: Sequence[str] = (),
) -> tuple[list[SettingDifference], str]:
    """Settings recorded differently or on one side only; a setting in `always` is listed as
    unrecorded when neither run records it (a run folder's EvalKit version)."""
    out: list[SettingDifference] = []
    differ = one_sided = False
    for k in sorted(old.keys() | new.keys() | set(always)):
        a, b = old.get(k), new.get(k)
        if a is None and b is None and k in always:
            one_sided = True
            out.append(SettingDifference(safe_text(k), None, None, "unrecorded in both runs"))
            continue
        if a == b:
            continue
        if a is None or b is None:
            one_sided = True
            note = "recorded on one side only"
        else:
            differ = True
            note = ""
        out.append(SettingDifference(safe_text(k), _maybe_text(a), _maybe_text(b), note))
    status = "different" if differ else "partly unrecorded" if one_sided else "same"
    return out, status


# ---- loading everything, and the outputs ------------------------------------------------------


def compare_runs(
    old: str | Path,
    new: str | Path,
    *,
    gates: str | Path | GatesFile | None = None,
    manifest: str | Path | Mapping[str, Any] | None = None,
    heldout_splits: Sequence[str | int | bool] = (),
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> Comparison:
    """Load both inputs (and the gates file and B5.14 manifest when given) and compare them.
    Bad input gives a Comparison with exit code 2 and its errors, never an exception."""
    errors: list[str] = []
    loaded: dict[str, RunInput] = {}
    for side, path in (("old", old), ("new", new)):
        try:
            loaded[side] = load_input(path)
        except MetricsError as exc:
            errors += [f"{side}: {p}" for p in exc.problems]
    gates_file: GatesFile | None = None
    if isinstance(gates, GatesFile):
        gates_file = gates
    elif gates is not None:
        try:
            gates_file = load_gates(gates)
        except MetricsError as exc:
            errors += list(exc.problems)
    manifest_data: Mapping[str, Any] | None = None
    if isinstance(manifest, Mapping):
        manifest_data = manifest
    elif manifest is not None:
        try:
            data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            errors.append(f"manifest {Path(manifest).name} is unreadable or not JSON")
        else:
            if isinstance(data, dict):
                manifest_data = data
            else:
                errors.append(f"manifest {Path(manifest).name} is not a JSON object")
    sources = {
        side: loaded[side].source() if side in loaded else {"path": str(path)}
        for side, path in (("old", old), ("new", new))
    }
    if errors:
        return Comparison.error(
            errors, sources["old"], sources["new"], seed=seed, resamples=resamples
        )
    c = compare(
        loaded["old"],
        loaded["new"],
        gates=gates_file,
        manifest=manifest_data,
        heldout_splits=heldout_splits,
        seed=seed,
        resamples=resamples,
    )
    if gates is not None:
        c.gates = "(given as an object)" if isinstance(gates, GatesFile) else str(gates)
    return c


def report_json(c: Comparison) -> str:
    """compare.json: sorted keys, indent 2, LF, trailing newline; no timestamps."""
    return json.dumps(c.as_dict(), sort_keys=True, indent=2) + "\n"


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _change_text(c: Change) -> str:
    if c.diff is None:
        return c.note or "–"
    sign = "+" if c.diff > 0 else ""
    text = f"{sign}{_fmt(c.diff)}"
    if c.ci_low is not None and c.ci_high is not None:
        text += f" [{_fmt(c.ci_low)}, {_fmt(c.ci_high)}]"
    else:
        text += " (no interval)"
    if c.interval_status == TOO_FEW:
        text += " (too few cases: not a claim)"
    elif c.interval_status == DEGENERATE:
        text += " (degenerate: zero width, not a claim)"
    return text


def _pairing_text(c: Change) -> str:
    if c.pairing == "paired":
        trials = ", trials" if c.method == TWO_LEVEL else ""
        return f"paired ({c.n_shared} cases{trials})"
    if c.heldout_rows and c.method.startswith("held-out"):
        return "unpaired (held-out)"
    text = "unpaired" if c.ci_low is not None else "unpaired, no interval"
    if c.partial_overlap and c.not_paired is not None:
        text += (
            f" (partial overlap: {c.n_shared} shared; {c.not_paired['old']} old and "
            f"{c.not_paired['new']} new without a partner)"
        )
    return text


def _worth_a_row(c: Change) -> bool:
    """The markdown table shows rows that moved: a non-zero change, a flag, a changed
    true/false value (a folded held-out group has no figures: shown when flagged)."""
    if c.flagged or c.regression:
        return True
    return c.old != c.new or (c.diff is not None and c.diff != 0)


def _flag_text(c: Change) -> str:
    if c.regression:
        return f"REGRESSION ({c.regression_severity} gate)"
    if c.flagged:
        return "flagged"
    return "–"


def render_markdown(c: Comparison) -> str:
    """A short report: sources, settings differences first, one table row per change,
    missing rows, errors and the verdict."""
    lines = ["# Run comparison", ""]
    for side in ("old", "new"):
        src = getattr(c, side)
        desc = f"{side}: `{_cell(str(src.get('path', '')))}`"
        if src.get("run_id"):
            project, run_id = _cell(str(src["project"])), _cell(str(src["run_id"]))
            desc += f" ({src.get('kind')}, project {project}, run {run_id})"
        if src.get("status") not in (None, "complete"):
            desc += f", {src['status']}"
        lines.append(desc + "  ")
    sims = [side for side in ("old", "new") if getattr(c, side).get("simulated")]
    if sims:
        who = "both runs'" if len(sims) == 2 else f"the {sims[0]} run's"
        lines.append(f"SIMULATED: {who} results come from a simulator  ")
    lines.append("")
    if c.settings:
        title = {
            "different": "**Settings differ**: this comparison crosses different settings.",
            "partly unrecorded": (
                "**Settings partly unrecorded**: some are unrecorded in one run or both."
            ),
        }.get(c.settings_status, "**Settings**")
        lines.append(title)
        for d in c.settings:
            note = f" ({d.note})" if d.note else ""
            old = "unrecorded" if d.old is None else _fmt(d.old)
            new = "unrecorded" if d.new is None else _fmt(d.new)
            lines.append(f"- `{_cell(d.setting)}`: {_cell(old)} → {_cell(new)}{note}")
    elif c.old.get("run_id") and c.new.get("run_id"):
        lines.append("Settings: the same, as recorded.")
    lines.append("")
    shown = [ch for ch in c.changes if _worth_a_row(ch)]
    if c.changes and not shown:
        lines.append(f"No change in any of the {len(c.changes)} compared row(s).")
        lines.append("")
    if shown:
        lines.append("| metric | labels | old | new | change (95% CI) | pairing | flag |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for ch in shown:
            labels = _labels_text(ch.labels)
            if ch.heldout_rows:
                labels += f" (held-out split, {ch.heldout_rows} row(s))"
            unit = f" {ch.unit}" if ch.unit not in ("rate", "count", "bool", "ratio") else ""
            cells = (
                ch.metric,
                labels,
                _fmt(ch.old) + (unit if ch.old is not None else ""),
                _fmt(ch.new) + (unit if ch.new is not None else ""),
                _change_text(ch),
                _pairing_text(ch),
                _flag_text(ch),
            )
            lines.append("| " + " | ".join(_cell(x) for x in cells) + " |")
        hidden = len(c.changes) - len(shown)
        if hidden:
            lines.append("")
            lines.append(f"{hidden} unchanged row(s) not shown (every row is in compare.json).")
        lines.append("")
    if c.missing:
        lines.append("Missing (listed, never skipped):")
        for m in c.missing:
            what = "" if m.what == "labels" else " (no metric of this name)"
            if m.heldout_rows:
                what += f" (held-out split, {m.heldout_rows} row(s), other labels not shown)"
            lines.append(
                f"- not in the {m.side} run: `{_cell(m.metric)}` "
                f"{_cell(_labels_text(m.labels))}{what}"
            )
        lines.append("")
    for e in c.errors:
        lines.append(f"error: {_cell(e)}")
    if c.errors:
        lines.append("")
    flagged = sum(1 for ch in c.changes if ch.flagged)
    regressions = sum(1 for ch in c.changes if ch.regression)
    unclaimed = sum(1 for ch in c.changes if ch.interval_status in (TOO_FEW, DEGENERATE))
    lines.append(
        f"Holm across {c.holm_family} compared row(s) at alpha {ALPHA}; seed {c.seed}, "
        f"{c.resamples:,} resamples. Paired: shared case ids (cases, then trials), when they "
        f"are at least half of each run's cases; unpaired: each run's interval combined "
        f"(MOVER). A change is claimed only from at least {MIN_CASES} cases and a non-zero-"
        f"width interval ({unclaimed} row(s) shown but not claimed). Held-out rows: "
        "split-level aggregates only."
    )
    lines.append("")
    lines.append(
        f"**Verdict: {c.verdict.upper()} (exit {c.exit_code})** · {flagged} flagged · "
        f"{regressions} gated regression(s) · {len(c.changes)} row(s) · {len(c.missing)} missing"
    )
    return "\n".join(lines) + "\n"
