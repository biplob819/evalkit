"""Spend ledger with a cap alarm (B5.7 LED-06; spec §7; OWNER D-73).

`spend-1` lines are chained like every ledger line. EvalKit's own ledger starts with 22 legacy
lines written before chaining; they are kept byte for byte and sealed by the first chained line
(its `prev` is the last legacy line's sha256 and its `sealed` records their count and sha256).
`check_spend` sums the current UTC window (total, day or calendar month) of any project's
ledger, read through a field map, against a cap: `alarm` at a set fraction (exit 0: it warns
about money, it does not fail quality), `cap` when reached or when an estimate would exceed it
(exit 3, so a script can refuse to start a run).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evalkit.ledger._io import (
    EXIT_COST_CAP,
    EXIT_PASS,
    LEDGER_SCHEMA,
    LedgerError,
    append_spend_line,
    chain_state,
    check_project,
    guard_ids,
    held_splits,
    load_manifest,
    text_field,
    utc_stamp,
)

ALARM_FRACTION = 0.8  # default; course-assistant uses 0.9 (its D-64)
Window = Literal["total", "day", "month"]
WINDOWS = ("total", "day", "month")
DEFAULT_MAP: dict[str, str] = {"spent_usd": "spent_usd", "at": "at"}
_DETAIL_KEYS = ("run_id", "model", "n_cases", "note")


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, str | int | float | bool)


def record_spend(
    ledger: str | Path,
    *,
    spend_kind: str,
    spent_usd: float,
    project: str,
    run_id: str | None = None,
    model: str | None = None,
    n_cases: int | None = None,
    note: str | None = None,
    detail: Mapping[str, Any] | None = None,
    at: str | None = None,
    heldout_splits: Sequence[Any] = (),
    manifest: str | Path | Mapping[str, Any] | None = None,
    cases: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Append one chained `spend` line (amount >= 0, rounded to 6 decimals) under the ledger's
    lock. Extra scalar facts (judges, item counts) go in `detail`. Returns the line written.
    With a manifest (and the held-out case files), a line naming a held-out case id or hash,
    or quoting held-out case text, is refused (code 2) as by every writer."""
    m = load_manifest(manifest)
    if not isinstance(spend_kind, str) or not spend_kind.strip():
        raise LedgerError("spend_kind must be non-blank text")
    if isinstance(spent_usd, bool) or not isinstance(spent_usd, int | float):
        raise LedgerError("spent_usd must be a number")
    if not math.isfinite(spent_usd) or spent_usd < 0:
        raise LedgerError("spent_usd must be finite and >= 0")
    if not isinstance(project, str) or not project.strip():
        raise LedgerError("project must be non-blank text")
    entry: dict[str, Any] = {
        "kind": "spend",
        "schema": LEDGER_SCHEMA,
        "at": utc_stamp(at),
        "project": project,
        "spend_kind": text_field("spend_kind", spend_kind),
        "spent_usd": round(float(spent_usd), 6),
    }
    if run_id is not None:
        entry["run_id"] = text_field("run_id", run_id)
    if model is not None:
        entry["model"] = text_field("model", model)
    if n_cases is not None:
        if isinstance(n_cases, bool) or not isinstance(n_cases, int) or n_cases < 0:
            raise LedgerError("n_cases must be an integer >= 0")
        entry["n_cases"] = n_cases
    if note is not None:
        entry["note"] = text_field("note", note)
    if detail:
        bad = [
            k
            for k, v in detail.items()
            if not (_scalar(v) or (isinstance(v, list) and all(_scalar(x) for x in v)))
        ]
        if bad:
            raise LedgerError("detail values must be scalars or lists of scalars")
        entry["detail"] = {str(k): v for k, v in sorted(detail.items())}
    guard_ids(entry, m, held_splits(None, heldout_splits), cases)

    def check(rows: list[dict[str, Any]], e: dict[str, Any]) -> None:
        check_project(rows, e["project"], ledger)

    return append_spend_line(ledger, entry, check=check)


def _parse_map(field_map: Mapping[str, str] | None) -> dict[str, str]:
    out = dict(DEFAULT_MAP)
    for k, v in (field_map or {}).items():
        if k not in DEFAULT_MAP:
            raise LedgerError(f"--map names {k!r}; the fields are spent_usd and at")
        if not isinstance(v, str) or not v:
            raise LedgerError(f"--map {k}= needs a field name")
        out[k] = v
    return out


def _when(value: Any, n: int) -> dt.date:
    """The UTC date of a line: an ISO time (a zone converted to UTC, none taken as UTC) or a
    bare YYYY-MM-DD (assay-pipeline)."""
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"line {n} has no time to place it in a window")
    try:
        if len(value.strip()) == 10:
            return dt.date.fromisoformat(value.strip())
        t = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        raise LedgerError(f"line {n}: its time is not ISO 8601") from None
    if t.tzinfo is not None:
        t = t.astimezone(dt.UTC)
    return t.date()


def _in_window(day: dt.date, now: dt.date, window: str) -> bool:
    if window == "total":
        return True
    if window == "day":
        return day == now
    return (day.year, day.month) == (now.year, now.month)


@dataclass
class SpendStatus:
    """A spend check: `ok` (exit 0), `alarm` (exit 0, printed), `cap` (exit 3)."""

    status: Literal["ok", "alarm", "cap"]
    window: str
    window_start: str | None
    spent_usd: float
    estimate_usd: float
    total_usd: float
    cap_usd: float
    alarm_fraction: float
    alarm_at_usd: float
    lines: int
    lines_in_window: int
    chain: str  # chained | sealed | unchained | empty
    exit_code: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "spend-check-1",
            "status": self.status,
            "window": self.window,
            "window_start": self.window_start,
            "spent_usd": self.spent_usd,
            "estimate_usd": self.estimate_usd,
            "total_usd": self.total_usd,
            "cap_usd": self.cap_usd,
            "alarm_fraction": self.alarm_fraction,
            "alarm_at_usd": self.alarm_at_usd,
            "remaining_usd": round(self.cap_usd - self.total_usd, 6),
            "lines": self.lines,
            "lines_in_window": self.lines_in_window,
            "chain": self.chain,
            "exit_code": self.exit_code,
            "notes": list(self.notes),
        }


def check_spend(
    ledger: str | Path,
    *,
    cap: float,
    window: Window = "month",
    alarm_fraction: float = ALARM_FRACTION,
    add_estimate: float = 0.0,
    field_map: Mapping[str, str] | None = None,
    now: str | dt.datetime | None = None,
) -> SpendStatus:
    """Window spend (plus an estimate about to be spent) against a cap. `ok` below
    alarm_fraction x cap; `alarm` at or above it; `cap` when spend reaches the cap or spend plus
    a positive estimate would exceed it. Any project's ledger is read unchanged through
    `field_map` ({"spent_usd": "cost_usd", "at": "date"}); an unchained file is reported
    `unchained`, not an error. Raises (code 2) for an unreadable ledger, a broken chain in its
    chained part, or a line without a numeric amount."""
    for name, v in (("cap", cap), ("alarm_fraction", alarm_fraction), ("estimate", add_estimate)):
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise LedgerError(f"{name} must be a finite number")
    if cap <= 0:
        raise LedgerError("cap must be > 0")
    if not 0 < alarm_fraction <= 1:
        raise LedgerError("alarm_fraction must be in (0, 1]")
    if add_estimate < 0:
        raise LedgerError("the estimate must be >= 0")
    if window not in WINDOWS:
        raise LedgerError("window must be total, day or month")
    fmap = _parse_map(field_map)
    p = Path(ledger)
    if not p.is_file():
        raise LedgerError(f"{p}: no such ledger")
    problems, chain, _legacy = chain_state(p)
    if problems:
        raise LedgerError(f"{p.name}: the chain is broken ({problems[0]})")
    if isinstance(now, dt.datetime):
        today = (now.astimezone(dt.UTC) if now.tzinfo else now).date()
    elif isinstance(now, str):
        today = _when(now, 0)
    else:
        today = dt.datetime.now(dt.UTC).date()
    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise LedgerError(f"{p.name} is unreadable") from None
    spent, n_lines, n_window = 0.0, 0, 0
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if not line.strip():
            continue
        n_lines += 1
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict):
            raise LedgerError(f"line {n} is not a JSON object")
        amount = row.get(fmap["spent_usd"])
        if isinstance(amount, bool) or not isinstance(amount, int | float):
            raise LedgerError(f"line {n} has no numeric {fmap['spent_usd']}")
        if not math.isfinite(amount):
            raise LedgerError(f"line {n}: {fmap['spent_usd']} is not finite")
        if window != "total" and not _in_window(_when(row.get(fmap["at"]), n), today, window):
            continue
        n_window += 1
        spent += float(amount)
    spent = round(spent, 6)
    total = round(spent + add_estimate, 6)
    alarm_at = round(alarm_fraction * cap, 6)
    if spent >= cap or (add_estimate > 0 and total > cap):
        status: Literal["ok", "alarm", "cap"] = "cap"
    elif total >= alarm_at:
        status = "alarm"
    else:
        status = "ok"
    start = None
    if window == "day":
        start = today.isoformat()
    elif window == "month":
        start = today.replace(day=1).isoformat()
    notes = []
    if chain == "unchained":
        notes.append("unchained: the ledger has no hash chain (read as it is)")
    return SpendStatus(
        status=status,
        window=window,
        window_start=start,
        spent_usd=spent,
        estimate_usd=round(float(add_estimate), 6),
        total_usd=total,
        cap_usd=float(cap),
        alarm_fraction=float(alarm_fraction),
        alarm_at_usd=alarm_at,
        lines=n_lines,
        lines_in_window=n_window,
        chain=chain,
        exit_code=EXIT_COST_CAP if status == "cap" else EXIT_PASS,
        notes=notes,
    )


def running_total(ledger: str | Path) -> float:
    """Sum of `spent_usd` over every line (legacy and chained), rounded to 6 decimals."""
    p = Path(ledger)
    total = 0.0
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            total += float(json.loads(line)["spent_usd"])
    return round(total, 6)
