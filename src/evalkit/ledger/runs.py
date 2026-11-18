"""Run history and alarm bands (B5.7 LED-01, LED-02, EFF-03; spec §1, §2, §6).

A `run` entry stores one run's metrics-1 rows inline (held-out rows folded to split level), so
the history stands on its own when run folders are not committed. A new run is judged against
its own comparable history per (metric, labels): centre the median, spread
max(1.4826·MAD, median SE of the history, SE of the new run); warn beyond 2 spreads, alarm
beyond 3 (exit 1), on the failing side only for a gated metric. Fewer than MIN_HISTORY runs is
`learning` and never flags.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evalkit.gates import GatesFile, canonical, load_gates
from evalkit.ledger import _io
from evalkit.ledger._io import (
    EXIT_ALARM,
    EXIT_COST_CAP,
    EXIT_PASS,
    EXIT_PENDING,
    LEDGER_SCHEMA,
    LedgerError,
    append,
    canonical_json,
    check_project,
    guard_ids,
    held_splits,
    load_manifest,
    project_of,
    read_entries,
    require_ledger,
    sha256_text,
    text_field,
    utc_stamp,
    worst_exit,
)
from evalkit.metrics import EFFICIENCY_NAMES, MetricsError, MetricsSummary, safe_text

MIN_HISTORY = 5
WINDOW = 20
MAD_SCALE = 1.4826
Z95_WIDTH = 3.92  # a 95% interval is about 3.92 standard errors wide
KEPT_HELDOUT = frozenset({"split", "model", "judge"})
# settings that never make a run a different system for the trend: a re-seed, an upgrade
SETTINGS_IGNORED = frozenset({"seed", "evalkit", "results_format"})
COST_NAMES = frozenset({"cost_per_case", "cost_per_correct", "system_cost", "eval_overhead_cost"})

BandStatus = Literal["learning", "ok", "warn", "alarm", "improved", "degenerate", "stopped"]
FLAGGED = ("warn", "alarm")

LabelKey = tuple[tuple[str, str, Any], ...]


def label_key(labels: Mapping[str, Any]) -> LabelKey:
    """Typed, as metrics-1: the label value 1 and "1" (and true) stay distinct."""
    return tuple(sorted((k, type(v).__name__, v) for k, v in labels.items()))


def _shown(labels: Mapping[str, Any]) -> dict[str, str]:
    return {safe_text(k): safe_text(canonical(v)) for k, v in sorted(labels.items())}


# ---- loading a run ------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedRun:
    path: Path
    summary_sha256: str
    source: str  # metrics-1 | evalkit-summary
    metrics: MetricsSummary
    settings: Mapping[str, str | None]
    status: Literal["complete", "pending", "stopped"]


def load_run(run: str | Path) -> LoadedRun:
    """Anything `evalkit gate` reads: a metrics-1 file, an EvalKit run folder or its summary.json
    (with its efficiency block), or a folder holding metrics.json."""
    from evalkit.compare import load_input

    try:
        ri = load_input(run)
    except MetricsError as exc:
        raise LedgerError("; ".join(exc.problems)) from None
    path = Path(ri.path)
    status: Literal["complete", "pending", "stopped"] = (
        "complete"
        if ri.status == "complete"
        else "pending"
        if ri.status == "judging pending"
        else "stopped"
    )
    return LoadedRun(
        path=path,
        summary_sha256=summary_digest(path),
        source="metrics-1" if ri.kind == "metrics-1" else "evalkit-summary",
        metrics=ri.metrics,
        settings=dict(ri.settings),
        status=status,
    )


def summary_digest(path: Path) -> str:
    """sha256 of the summary's canonical JSON (sorted keys, compact, ASCII), so the same run
    pretty-printed, minified or with its keys reordered is the same run (recorded once)."""
    import json

    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise LedgerError(f"{path.name} is unreadable or not JSON") from None
    return sha256_text(canonical_json(data))


def _gates(gates: str | Path | GatesFile | None) -> GatesFile | None:
    if gates is None or isinstance(gates, GatesFile):
        return gates
    try:
        return load_gates(gates)
    except MetricsError as exc:
        raise LedgerError("; ".join(exc.problems)) from None


def fold_rows(
    metrics: MetricsSummary, held: frozenset[str]
) -> tuple[list[dict[str, Any]], int, dict[str, str | None]]:
    """The run's rows as stored: held-out rows only at split level (labels within split, model,
    judge; any other held-out row is dropped and counted), and each efficiency row's
    `price_date` label moved to the entry's `price_dates` per model, so a price-table change
    keeps the row comparable and is reported as a hint (EFF-03)."""
    rows: list[dict[str, Any]] = []
    folded = 0
    price_dates: dict[str, str | None] = {}
    for m in metrics.metrics:
        labels = dict(m.labels)
        is_held = "split" in labels and canonical(labels["split"]) in held
        if is_held and not set(labels) <= KEPT_HELDOUT:
            folded += 1
            continue
        row = m.model_dump(exclude_none=True)
        row["labels"] = labels
        if m.name in EFFICIENCY_NAMES and "model" in labels:
            model = canonical(labels["model"])
            date = labels.get("price_date")
            if date is not None or model not in price_dates:
                price_dates[model] = None if date is None else canonical(date)
        rows.append(row)
    stripped = []
    for row in rows:
        if row["name"] in EFFICIENCY_NAMES and "price_date" in row["labels"]:
            labels = {k: v for k, v in row["labels"].items() if k != "price_date"}
            row = {**row, "labels": labels}
        stripped.append(row)
    keys = [(r["name"], label_key(r["labels"])) for r in stripped]
    if len(set(keys)) == len(keys):  # never merge two rows by dropping the label
        rows = stripped
    return rows, folded, dict(sorted(price_dates.items()))


def settings_hash(settings: Mapping[str, str | None]) -> str:
    keep = {
        k: v
        for k, v in settings.items()
        if k not in SETTINGS_IGNORED and k not in ("project", "simulated") and v is not None
    }
    return sha256_text(canonical_json(keep))


def run_entry(
    run: LoadedRun,
    *,
    held: frozenset[str],
    note: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    rows, folded, price_dates = fold_rows(run.metrics, held)
    s = run.settings
    pv = run.metrics.provenance
    entry: dict[str, Any] = {
        "kind": "run",
        "schema": LEDGER_SCHEMA,
        "at": utc_stamp(at),
        "project": run.metrics.project,
        "run_id": run.metrics.run_id,
        "source": run.source,
        "summary_sha256": run.summary_sha256,
        "config_hash": s.get("config_hash") or pv.config_hash,
        "code_version": s.get("code_version") or pv.code_version,
        "snapshot": s.get("snapshot") or pv.snapshot,
        "evalkit": s.get("evalkit") or pv.evalkit,
        "recorded_with": _io.evalkit_version(),
        "settings_sha256": settings_hash(s),
        "simulated": bool(run.metrics.simulated),
        "status": "complete" if run.status == "complete" else "stopped",
        "price_dates": price_dates,
        "metrics": rows,
        "heldout_rows_folded": folded,
    }
    if note is not None:
        entry["note"] = text_field("note", note)
    return entry


# ---- bands (LED-02) -----------------------------------------------------------------------------


def row_se(row: Mapping[str, Any]) -> float:
    """A run's standard error for a row: interval width / 3.92; for a rate with n and no
    interval, sqrt(p(1-p)/n) with p clamped to [0.5/n, 1 - 0.5/n]; else 0."""
    lo, hi = row.get("ci_low"), row.get("ci_high")
    if lo is not None and hi is not None:
        return (float(hi) - float(lo)) / Z95_WIDTH
    n = row.get("n")
    if row.get("unit") == "rate" and isinstance(n, int) and n > 0:
        p = min(max(float(row["value"]), 0.5 / n), 1 - 0.5 / n)
        return math.sqrt(p * (1 - p) / n)
    return 0.0


@dataclass(frozen=True)
class Band:
    """One (metric, labels) of a run judged against its comparable history."""

    metric: str
    labels: Mapping[str, Any]
    unit: str
    value: bool | int | float
    status: BandStatus
    n_history: int
    ci_low: float | None = None
    ci_high: float | None = None
    centre: float | None = None
    spread: float | None = None
    mad_spread: float | None = None
    history_se: float | None = None
    run_se: float | None = None
    side: Literal["both", "low", "high"] = "both"
    z: float | None = None
    mode: Any = None  # bool metrics: the history's most common value
    hints: tuple[str, ...] = ()
    heldout: bool = False

    @property
    def flagged(self) -> bool:
        return self.status in FLAGGED

    def limits(self, k: float) -> tuple[float, float] | None:
        if self.centre is None or self.spread is None:
            return None
        return self.centre - k * self.spread, self.centre + k * self.spread

    def as_dict(self) -> dict[str, Any]:
        warn, alarm = self.limits(2), self.limits(3)
        out: dict[str, Any] = {
            "metric": self.metric,
            "labels": _shown(self.labels),
            "unit": self.unit,
            "value": self.value,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "status": self.status,
            "n_history": self.n_history,
            "centre": self.centre,
            "spread": self.spread,
            "mad_spread": self.mad_spread,
            "history_se": self.history_se,
            "run_se": self.run_se,
            "warn_band": list(warn) if warn else None,
            "alarm_band": list(alarm) if alarm else None,
            "side": self.side,
            "z": self.z,
            "hints": list(self.hints),
        }
        if self.mode is not None:
            out["mode"] = self.mode
        if self.heldout:
            out["heldout"] = True
        return out


def gate_side(gates: GatesFile | None, name: str, labels: Mapping[str, Any]) -> str:
    """Which side of the band may alarm: a gated metric only on the side that moves toward
    failing its gate (`>=`: low; `<=`: high; `==` or both kinds: both); ungated: both."""
    if gates is None:
        return "both"
    from evalkit.gates import _matches

    ops = {g.op for g in gates.gates if g.metric == name and _matches(labels, g.labels)}
    if ops == {">="}:
        return "low"
    if ops == {"<="}:
        return "high"
    return "both"


def band(
    history: Sequence[Mapping[str, Any]],
    row: Mapping[str, Any],
    *,
    side: str = "both",
    min_history: int = MIN_HISTORY,
) -> Band:
    """Judge `row` (a metrics-1 row) against `history` (the same row in earlier comparable runs,
    oldest first, at most WINDOW of them). Pure: the same inputs give the same Band."""
    value = row["value"]
    labels = dict(row.get("labels", {}))
    lo, hi = row.get("ci_low"), row.get("ci_high")
    where: Literal["both", "low", "high"] = (
        "low" if side == "low" else "high" if side == "high" else "both"
    )

    def make(status: BandStatus, **kw: Any) -> Band:
        return Band(
            metric=row["name"],
            labels=labels,
            unit=row["unit"],
            value=value,
            status=status,
            n_history=len(history),
            ci_low=lo,
            ci_high=hi,
            side=where,
            **kw,
        )

    if len(history) < min_history:
        return make("learning")
    if isinstance(value, bool) or row["unit"] == "bool":
        counts = Counter(h["value"] for h in history)
        top = max(counts.values())
        modes = sorted((v for v, c in counts.items() if c == top), key=repr)
        return make("ok" if value in modes else "warn", mode=modes[0])
    xs = [float(h["value"]) for h in history]
    c = statistics.median(xs)
    mad = MAD_SCALE * statistics.median(abs(x - c) for x in xs)
    h_se = statistics.median(row_se(h) for h in history)
    r_se = row_se(row)
    s = max(mad, h_se, r_se)
    d = float(value) - c
    bad = where == "both" or (where == "low" and d < 0) or (where == "high" and d > 0)
    status: BandStatus
    z: float | None = None
    if s == 0:
        status = "degenerate" if d == 0 else ("warn" if bad else "improved")
    else:
        z = d / s
        if abs(d) > 3 * s:
            status = "alarm" if bad else "improved"
        elif abs(d) > 2 * s:
            status = "warn" if bad else "improved"
        else:
            status = "ok"
    return make(status, centre=c, spread=s, mad_spread=mad, history_se=h_se, run_se=r_se, z=z)


def _hints(b: Band, price_now: str | None, price_before: str | None, changed: bool) -> list[str]:
    """Cause hints on a moved efficiency row (EFF-03). Labels only: they never clear a flag."""
    if b.status in ("ok", "learning", "degenerate", "stopped"):
        return []
    out = []
    if b.metric in COST_NAMES and changed and price_now != price_before:
        out.append("price_date changed")
    if b.metric == "cache_hit_share" and b.centre is not None and float(b.value) < b.centre:
        out.append("cache regression")
    if b.metric.startswith("latency_p"):
        out.append(f"{b.metric.removeprefix('latency_')} latency")
    return out


def _reset_matches(reset: Mapping[str, Any], name: str, labels: Mapping[str, Any]) -> bool:
    if reset.get("metric") != name:
        return False
    want = reset.get("labels")
    if not want:
        return True
    have = {k: canonical(v) for k, v in labels.items()}
    return {k: canonical(v) for k, v in want.items()} == have


def comparable(
    rows: Sequence[Mapping[str, Any]],
    upto: int,
    *,
    project: str,
    simulated: bool,
    name: str,
    labels: Mapping[str, Any],
    unit: str,
    window: int = WINDOW,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """The comparable history of a row before position `upto`: the last `window` complete run
    entries after the latest matching band_reset, with the same project, simulated flag, metric
    name, typed labels and unit. Returns (entry, row) pairs, oldest first."""
    key = label_key(labels)
    out: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for r in rows[:upto]:
        kind = r.get("kind")
        if kind == "band_reset" and r.get("project") == project:
            if _reset_matches(r, name, labels):
                out = []
            continue
        if kind != "run" or r.get("status") != "complete" or r.get("project") != project:
            continue
        if bool(r.get("simulated")) != simulated:
            continue
        for m in r.get("metrics", []):
            if (
                m.get("name") == name
                and m.get("unit") == unit
                and label_key(m.get("labels", {})) == key
            ):
                out.append((r, m))
                break
    return out[-window:]


def judge_entry(
    rows: Sequence[Mapping[str, Any]],
    upto: int,
    entry: Mapping[str, Any],
    *,
    gates: GatesFile | None = None,
    held: frozenset[str] = frozenset(),
) -> list[Band]:
    """Every row of `entry` judged against the comparable history before position `upto`."""
    out = []
    project, simulated = str(entry.get("project")), bool(entry.get("simulated"))
    for row in entry.get("metrics", []):
        labels = row.get("labels", {})
        hist = comparable(
            rows,
            upto,
            project=project,
            simulated=simulated,
            name=row["name"],
            labels=labels,
            unit=row["unit"],
        )
        b = band([m for _, m in hist], row, side=gate_side(gates, row["name"], labels))
        model = canonical(labels["model"]) if "model" in labels else None
        hints: list[str] = []
        if model is not None and hist:
            prev_dates = hist[-1][0].get("price_dates") or {}
            now_dates = entry.get("price_dates") or {}
            changed = model in prev_dates or model in now_dates
            hints = _hints(b, now_dates.get(model), prev_dates.get(model), changed)
        else:
            hints = _hints(b, None, None, False)
        is_held = "split" in labels and canonical(labels["split"]) in held
        if entry.get("status") == "stopped":
            b = _replace(b, status="stopped")
        if hints or is_held:
            b = _replace(b, hints=tuple(hints), heldout=is_held)
        out.append(b)
    return out


def _replace(b: Band, **kw: Any) -> Band:
    from dataclasses import replace

    return replace(b, **kw)


# ---- record and check a run (LED-01) ------------------------------------------------------------


@dataclass
class RunCheck:
    """A run judged against its history (and, for `record_run`, the entry appended)."""

    project: str
    run_id: str
    status: str  # complete | stopped
    bands: list[Band]
    exit_code: int
    entry: Mapping[str, Any] | None = None
    heldout_rows_folded: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        counts = Counter(b.status for b in self.bands)
        return {
            "schema": "history-check-1",
            "project": safe_text(self.project),
            "run_id": safe_text(self.run_id),
            "status": self.status,
            "recorded": self.entry is not None,
            "summary_sha256": self.entry.get("summary_sha256") if self.entry else None,
            "exit_code": self.exit_code,
            "alarms": counts.get("alarm", 0),
            "warns": counts.get("warn", 0),
            "learning": counts.get("learning", 0),
            "heldout_rows_folded": self.heldout_rows_folded,
            "bands": [b.as_dict() for b in self.bands],
            "errors": list(self.errors),
        }


def _exit_for(status: str, bands: Sequence[Band]) -> int:
    codes = [EXIT_PASS]
    if status == "stopped":
        codes.append(EXIT_COST_CAP)
    if any(b.status == "alarm" for b in bands):
        codes.append(EXIT_ALARM)
    return worst_exit(codes)


def check_run(
    ledger: str | Path,
    run: str | Path,
    *,
    gates: str | Path | GatesFile | None = None,
    heldout_splits: Sequence[Any] = (),
) -> RunCheck:
    """Judge a run against the ledger's history without recording it. Raises LedgerError
    (code 2 bad input, a missing ledger included; 4 judging pending)."""
    require_ledger(ledger)
    g = _gates(gates)
    held = held_splits(g, heldout_splits)
    loaded = load_run(run)
    if loaded.status == "pending":
        raise LedgerError("judging is pending: rerun when it completes", EXIT_PENDING)
    rows = read_entries(ledger)
    check_project(rows, loaded.metrics.project, ledger)
    entry = run_entry(loaded, held=held)
    bands = judge_entry(rows, len(rows), entry, gates=g, held=held)
    return RunCheck(
        project=loaded.metrics.project,
        run_id=loaded.metrics.run_id,
        status=entry["status"],
        bands=bands,
        exit_code=_exit_for(entry["status"], bands),
        heldout_rows_folded=entry["heldout_rows_folded"],
    )


def record_run(
    ledger: str | Path,
    run: str | Path,
    *,
    gates: str | Path | GatesFile | None = None,
    heldout_splits: Sequence[Any] = (),
    manifest: str | Path | Mapping[str, Any] | None = None,
    cases: Mapping[str, str | Path] | None = None,
    note: str | None = None,
    at: str | None = None,
) -> RunCheck:
    """Append one hash-chained `run` entry and judge the run against the history before it.
    A run is recorded once (a repeated `summary_sha256` raises, code 2); a run with judging
    pending is refused unrecorded (code 4); a run stopped at its cost cap is recorded with
    status `stopped`, kept out of every band, and the result's exit code is 3."""
    g = _gates(gates)
    held = held_splits(g, heldout_splits)
    m = load_manifest(manifest)
    loaded = load_run(run)
    if loaded.status == "pending":
        raise LedgerError("judging is pending: the run is not recorded", EXIT_PENDING)
    entry = run_entry(loaded, held=held, note=note, at=at)
    guard_ids(entry, m, held, cases)
    seen: list[list[Band]] = []

    def check(rows: list[dict[str, Any]], e: dict[str, Any]) -> None:
        check_project(rows, e["project"], ledger)
        for r in rows:
            if r.get("kind") == "run" and r.get("summary_sha256") == e["summary_sha256"]:
                raise LedgerError(
                    f"run {safe_text(e['run_id'])} is already recorded (same summary_sha256)"
                )
        seen.append(judge_entry(rows, len(rows), e, gates=g, held=held))

    written = append(ledger, entry, check=check)
    bands = seen[-1]
    return RunCheck(
        project=loaded.metrics.project,
        run_id=loaded.metrics.run_id,
        status=entry["status"],
        bands=bands,
        exit_code=_exit_for(entry["status"], bands),
        entry=written,
        heldout_rows_folded=entry["heldout_rows_folded"],
    )


def reset_band(
    ledger: str | Path,
    *,
    metric: str,
    reason: str,
    labels: Mapping[str, Any] | None = None,
    project: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Append a `band_reset`: history before it is not used for this metric's band (every label
    set of it when `labels` is empty), after a deliberate change such as a new model."""
    from evalkit.metrics import NAME_RE

    if not isinstance(metric, str) or not NAME_RE.match(metric):
        raise LedgerError("metric must match [a-z][a-z0-9_.]*")
    entry: dict[str, Any] = {
        "kind": "band_reset",
        "schema": LEDGER_SCHEMA,
        "at": utc_stamp(at),
        "metric": metric,
        "reason": text_field("reason", reason, required=True),
    }
    if labels:
        entry["labels"] = {str(k): canonical(v) for k, v in sorted(labels.items())}

    def check(rows: list[dict[str, Any]], e: dict[str, Any]) -> None:
        p = project or project_of(rows)
        if p is None:
            raise LedgerError("the ledger is empty: name the project")
        check_project(rows, p, ledger)
        e["project"] = p

    return append(ledger, entry, check=check)


# ---- the history view (history show, EFF-03 spread table) ---------------------------------------


@dataclass(frozen=True)
class TrendPoint:
    index: int  # run order in the ledger, from 1
    run_id: str
    at: str
    value: bool | int | float
    ci_low: float | None
    ci_high: float | None
    status: str
    centre: float | None
    spread: float | None
    settings_changed: bool
    reset_before: bool
    hints: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "run_id": safe_text(self.run_id),
            "at": self.at,
            "value": self.value,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "status": self.status,
            "centre": self.centre,
            "spread": self.spread,
            "settings_changed": self.settings_changed,
            "reset_before": self.reset_before,
            "hints": list(self.hints),
        }


@dataclass
class Series:
    metric: str
    labels: Mapping[str, Any]
    unit: str
    points: list[TrendPoint]
    latest: Band
    heldout: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "labels": _shown(self.labels),
            "unit": self.unit,
            "runs": len(self.points),
            "latest": self.latest.as_dict(),
            "points": [p.as_dict() for p in self.points],
        }


@dataclass(frozen=True)
class SpreadRow:
    """EFF-03: run-to-run variation of one efficiency figure for one model."""

    model: str
    mode: str | None
    name: str
    unit: str
    runs: int
    median: float
    mad_spread: float
    cv: float | None
    min: float
    max: float
    latest: float
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": safe_text(self.model),
            "mode": self.mode,
            "name": self.name,
            "unit": self.unit,
            "runs": self.runs,
            "median": self.median,
            "mad_spread": self.mad_spread,
            "cv": self.cv,
            "min": self.min,
            "max": self.max,
            "latest": self.latest,
            "status": self.status,
        }


@dataclass(frozen=True)
class PsiSeries:
    metric: str
    feature: str
    points: tuple[tuple[int, str, float], ...]  # (run index, run id, psi)


@dataclass
class History:
    project: str | None
    runs: int
    series: list[Series]
    spread: list[SpreadRow]
    psi: list[PsiSeries]
    resets: list[Mapping[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "history-1",
            "project": None if self.project is None else safe_text(self.project),
            "runs": self.runs,
            "series": [s.as_dict() for s in self.series],
            "spread": [r.as_dict() for r in self.spread],
            "psi": [
                {
                    "metric": p.metric,
                    "feature": safe_text(p.feature),
                    "points": [
                        {"index": i, "run_id": safe_text(r), "psi": v} for i, r, v in p.points
                    ],
                }
                for p in self.psi
            ],
            "resets": [
                {
                    "metric": r.get("metric"),
                    "labels": r.get("labels") or {},
                    "reason": r.get("reason"),
                    "at": r.get("at"),
                }
                for r in self.resets
            ],
        }


def spread_row(series: Series) -> SpreadRow | None:
    """The EFF-03 spread over the latest band's window: the last WINDOW complete runs after the
    latest reset, the latest run included."""
    if series.metric not in EFFICIENCY_NAMES or "model" not in series.labels:
        return None
    window: list[TrendPoint] = []
    for p in series.points:
        if p.reset_before:
            window = []
        if p.status != "stopped":
            window.append(p)
    window = window[-WINDOW:]
    if not window:
        return None
    xs = [float(p.value) for p in window]
    med = statistics.median(xs)
    mad = MAD_SCALE * statistics.median(abs(x - med) for x in xs)
    return SpreadRow(
        model=canonical(series.labels["model"]),
        mode=canonical(series.labels["mode"]) if "mode" in series.labels else None,
        name=series.metric,
        unit=series.unit,
        runs=len(xs),
        median=med,
        mad_spread=mad,
        cv=(mad / abs(med)) if med else None,
        min=min(xs),
        max=max(xs),
        latest=float(series.points[-1].value),
        status=series.latest.status,
    )


def _is_psi(name: str) -> bool:
    return name == "psi" or name.endswith(".psi")


def history(
    ledger: str | Path,
    *,
    gates: str | Path | GatesFile | None = None,
    metric: str | None = None,
    heldout_splits: Sequence[Any] = (),
) -> History:
    """Every (metric, labels) of the ledger with each run judged against the history before it,
    the latest band, the EFF-03 spread table and the PSI series of drift checks."""
    g = _gates(gates)
    held = held_splits(g, heldout_splits)
    rows = [r for r in read_entries(require_ledger(ledger)) if r.get("schema") == LEDGER_SCHEMA]
    project = project_of(rows)
    points: dict[tuple[str, LabelKey], list[TrendPoint]] = {}
    meta: dict[tuple[str, LabelKey], tuple[Mapping[str, Any], str, Band]] = {}
    order: list[tuple[str, LabelKey]] = []
    psi: dict[tuple[str, str], list[tuple[int, str, float]]] = {}
    prev_settings: str | None = None
    pending_resets: list[Mapping[str, Any]] = []
    resets: list[Mapping[str, Any]] = []
    n_run = 0
    for i, r in enumerate(rows):
        if r.get("kind") == "band_reset":
            pending_resets.append(r)
            resets.append(r)
            continue
        if r.get("kind") != "run":
            continue
        n_run += 1
        changed = prev_settings is not None and r.get("settings_sha256") != prev_settings
        prev_settings = r.get("settings_sha256")
        bands = judge_entry(rows, i, r, gates=g, held=held)
        for row, b in zip(r.get("metrics", []), bands, strict=True):
            if _is_psi(row["name"]) and "feature" in row.get("labels", {}):
                psi.setdefault((row["name"], canonical(row["labels"]["feature"])), []).append(
                    (n_run, str(r.get("run_id")), float(row["value"]))
                )
            if metric is not None and row["name"] != metric:
                continue
            key = (row["name"], label_key(row.get("labels", {})))
            if key not in points:
                points[key] = []
                order.append(key)
            reset_here = any(
                _reset_matches(x, row["name"], row.get("labels", {})) for x in pending_resets
            )
            points[key].append(
                TrendPoint(
                    index=n_run,
                    run_id=str(r.get("run_id")),
                    at=str(r.get("at")),
                    value=row["value"],
                    ci_low=row.get("ci_low"),
                    ci_high=row.get("ci_high"),
                    status=b.status,
                    centre=b.centre,
                    spread=b.spread,
                    settings_changed=changed,
                    reset_before=reset_here,
                    hints=b.hints,
                )
            )
            meta[key] = (row.get("labels", {}), row["unit"], b)
        pending_resets = []
    series = []
    for key in sorted(order, key=lambda k: (k[0], repr(k[1]))):
        labels, unit, latest = meta[key]
        series.append(
            Series(
                metric=key[0],
                labels=labels,
                unit=unit,
                points=points[key],
                latest=latest,
                heldout=latest.heldout,
            )
        )
    spread = [s for s in (spread_row(x) for x in series) if s is not None]
    spread.sort(key=lambda s: (s.model, s.mode or "", s.name))
    return History(
        project=project,
        runs=n_run,
        series=series,
        spread=spread,
        psi=[PsiSeries(m, f, tuple(v)) for (m, f), v in sorted(psi.items())],
        resets=resets,
    )


def bands(
    ledger: str | Path,
    *,
    gates: str | Path | GatesFile | None = None,
    heldout_splits: Sequence[Any] = (),
) -> list[Band]:
    """The latest band of every (metric, labels) in the ledger, each judged against the
    history before its run."""
    return [s.latest for s in history(ledger, gates=gates, heldout_splits=heldout_splits).series]
