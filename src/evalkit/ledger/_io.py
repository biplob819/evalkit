"""Shared plumbing for the three ledgers (B5.7 spec §0, §8, §9).

Every line EvalKit writes is canonical JSON chained to the line before it by B5.14's writer
(`evalkit.heldout`): the same canonical form, the same exclusive file lock and one write per
append. A spend ledger may start with unchained legacy lines; the first chained line seals them
(OWNER D-73). Messages name files, line numbers and manifest ids only, never held-out text.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evalkit import heldout as H
from evalkit.gates import canonical

LEDGER_SCHEMA = "ledger-1"
MAX_TEXT = 500  # characters in a free-text field (note, hypothesis, change, reason)
RUNS_PATH = Path("docs") / "measurements" / "runs.jsonl"
EXPERIMENTS_PATH = Path("docs") / "measurements" / "experiments.jsonl"
SPEND_PATH = Path("docs") / "measurements" / "spend-ledger.jsonl"
DEFAULT_HELDOUT = "heldout"

EXIT_PASS, EXIT_ALARM, EXIT_INPUT, EXIT_COST_CAP, EXIT_PENDING = 0, 1, 2, 3, 4
_PRECEDENCE = {EXIT_INPUT: 4, EXIT_COST_CAP: 3, EXIT_PENDING: 2, EXIT_ALARM: 1, EXIT_PASS: 0}


class LedgerError(ValueError):
    """A ledger refused the request; `code` is the exit code (2 input, 1 a refused second
    look, 3 cost cap, 4 judging pending). Messages never carry held-out ids or text."""

    def __init__(self, message: str, code: int = EXIT_INPUT) -> None:
        super().__init__(message)
        self.code = code


def worst_exit(codes: Iterable[int]) -> int:
    """The exit code that wins under EvalKit's precedence 2 > 3 > 4 > 1 > 0 (GAT-09)."""
    return max(codes, key=lambda c: _PRECEDENCE.get(c, 5), default=EXIT_PASS)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    """B5.14's canonical form: sorted keys, compact, ASCII."""
    return H._canonical(obj)


def evalkit_version() -> str:
    from evalkit import __version__

    return __version__


def utc_stamp(at: str | None = None) -> str:
    """An entry time as `YYYY-MM-DDTHH:MM:SSZ` (UTC, seconds). Given: an ISO 8601 time with a
    zone. Not given: `SOURCE_DATE_EPOCH` when set (reproducible ledgers), else now."""
    if at is None:
        epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
        if epoch:
            try:
                when = dt.datetime.fromtimestamp(int(epoch), dt.UTC)
            except (ValueError, OverflowError, OSError):
                raise LedgerError("SOURCE_DATE_EPOCH must be whole seconds since 1970") from None
        else:
            when = dt.datetime.now(dt.UTC)
    else:
        try:
            when = dt.datetime.fromisoformat(str(at))
        except ValueError:
            raise LedgerError("at must be an ISO 8601 time with a zone") from None
        if when.tzinfo is None:
            raise LedgerError("at must carry a zone (e.g. Z or +05:30)")
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_field(name: str, value: Any, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise LedgerError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise LedgerError(f"{name} must be text")
    if required and not value.strip():
        raise LedgerError(f"{name} must not be blank")
    if len(value) > MAX_TEXT:
        raise LedgerError(f"{name} is {len(value)} characters; at most {MAX_TEXT}")
    return value


def held_splits(gates: Any = None, extra: Sequence[Any] = ()) -> frozenset[str]:
    """The split label values that are held out: `heldout` always, the gates file's
    `heldout_splits`, and any named on the command line (compared as canonical strings)."""
    out = {DEFAULT_HELDOUT} | {canonical(v) for v in extra}
    if gates is not None:
        out |= {canonical(v) for v in gates.heldout_splits}
    return frozenset(out)


def load_manifest(path: str | Path | Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """A B5.14 manifest, self-checked (`manifest_sha256` over the rest), or None."""
    if path is None:
        return None
    if isinstance(path, Mapping):
        data: Any = path
        name = "manifest"
    else:
        p = Path(path)
        name = p.name
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise LedgerError(f"manifest {name} is unreadable or not JSON") from None
    if not isinstance(data, Mapping) or not isinstance(data.get("splits"), Mapping):
        raise LedgerError(f"manifest {name} is not a B5.14 held-out manifest")
    body = {k: v for k, v in data.items() if k != "manifest_sha256"}
    if data.get("manifest_sha256") != sha256_text(canonical_json(body)):
        raise LedgerError(f"manifest {name}: manifest_sha256 does not match (edited by hand)")
    return data


_HEX_RUN = re.compile(r"[0-9a-f]{12,}")


def _case_text(
    manifest: Mapping[str, Any], splits: Sequence[str], cases: Mapping[str, str | Path]
) -> tuple[list[tuple[str, str]], dict[str, set[str]]]:
    """The held-out case strings as B5.14's exposure scan reads them (NFKC, casefolded, runs of
    non-word characters one space): whole strings of at least 25 characters, and every window
    of 10 words. Each case file is checked against the manifest first."""
    id_field = str(manifest.get("id_field", "id"))
    wholes: list[tuple[str, str]] = []
    shingles: dict[str, set[str]] = {}
    for split, path in sorted(cases.items()):
        if split not in splits:
            raise LedgerError(f"--cases {split}: not a held-out split of the manifest")
        try:
            loaded = H.load_cases(path, id_field=id_field)
            problems = H._split_problems(
                split,
                manifest["splits"][split],
                H._entries(loaded, id_field),
                (manifest.get("counts") or {}).get(split),
            )
        except H.HeldoutError as exc:
            raise LedgerError(f"--cases {split}: {exc}") from None
        if problems:
            raise LedgerError(
                f"--cases {split} does not match the manifest ({len(problems)} problem(s), "
                f"first: {problems[0]})"
            )
        for case in loaded:
            mid = H.manifest_id(str(case[id_field]))
            for s in H._strings({k: v for k, v in case.items() if k != id_field}):
                norm = H._normalise(s)
                if len(norm) >= H.EXPOSURE_MIN_CHARS:
                    wholes.append((f" {norm} ", mid))
                for gram in H._grams(norm.split(" "), H.EXPOSURE_WINDOW):
                    if len(gram) >= H.EXPOSURE_MIN_CHARS:
                        shingles.setdefault(gram, set()).add(mid)
    return wholes, shingles


def guard_ids(
    entry: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    held: Iterable[str],
    cases: Mapping[str, str | Path] | None = None,
) -> None:
    """Refuse (exit 2) a line that names a held-out case of the manifest (spec §8): its id; its
    id or content sha256, whole or as a prefix of at least 12 hex characters (the manifest id),
    in any letter case; and, when the held-out case files are given (`cases`, {split: path},
    checked against the manifest), any of its text, read with B5.14's exposure-scan
    normalisation (a whole case string of 25+ characters, or 10 consecutive words of one)."""
    if manifest is None:
        if cases:
            raise LedgerError("--cases needs --manifest (the case files are checked against it)")
        return
    held = sorted(set(held))
    splits = [s for s in held if s in manifest["splits"]]
    if not splits:
        raise LedgerError("the manifest has none of the held-out splits (" + ", ".join(held) + ")")
    text = canonical_json(entry)
    found: set[str] = set()
    for split in splits:
        found |= set(H.ids_in_text(text, manifest, split=split))
    hashes = {
        str(e[key]).lower()
        for split in splits
        for e in manifest["splits"][split]
        for key in ("id_sha256", "content_sha256")
        if isinstance(e, Mapping) and isinstance(e.get(key), str)
    }
    runs = _HEX_RUN.findall(text.lower())
    found |= {h[:12] for h in hashes if any(h[:12] in run for run in runs)}
    if found:
        raise LedgerError(
            f"refused: the entry names {len(found)} held-out case id(s) or id hash(es): "
            + ", ".join(sorted(found))
        )
    if not cases:
        return
    wholes, shingles = _case_text(manifest, splits, cases)
    quoted: set[str] = set()
    for s in H._strings(dict(entry)):
        view = H._normalise(s)
        padded = f" {view} "
        quoted |= {mid for needle, mid in wholes if needle in padded}
        for gram in H._grams(view.split(" "), H.EXPOSURE_WINDOW):
            quoted |= shingles.get(gram, set())
    if quoted:
        raise LedgerError(
            f"refused: the entry quotes held-out case text (case(s) {', '.join(sorted(quoted))})"
        )


def require_ledger(path: str | Path) -> Path:
    """A ledger that must already exist (reading commands): a missing or mistyped path is an
    error (exit 2), never an empty history. `history init` or the first `history add` makes
    one."""
    p = Path(path)
    if not p.is_file():
        raise LedgerError(
            f"{p}: no such ledger (check the path; `evalkit history init --ledger {p}` or the "
            "first `evalkit history add` creates it)"
        )
    return p


def init_ledger(path: str | Path) -> bool:
    """Create an empty ledger (and its folder). False, and nothing changed, when it exists."""
    p = Path(path)
    if p.exists():
        if not p.is_file():
            raise LedgerError(f"{p} exists and is not a file")
        read_entries(p)  # an existing file must at least be a readable ledger
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("x", encoding="utf-8"):
        pass
    return True


def read_entries(path: str | Path) -> list[dict[str, Any]]:
    try:
        return H.read_log(path)
    except H.HeldoutError as exc:
        raise LedgerError(f"{Path(path).name}: {exc}") from None


def ledger_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The entries EvalKit wrote (schema ledger-1), in order."""
    return [r for r in rows if r.get("schema") == LEDGER_SCHEMA]


def check_project(rows: Sequence[Mapping[str, Any]], project: str, path: str | Path) -> None:
    have = sorted({str(r.get("project")) for r in ledger_rows(rows)} - {project})
    if have:
        raise LedgerError(
            f"{Path(path).name} holds project {have[0]!r}; one ledger holds one project"
        )


def project_of(rows: Sequence[Mapping[str, Any]]) -> str | None:
    for r in ledger_rows(rows):
        if isinstance(r.get("project"), str):
            return str(r["project"])
    return None


def append(
    path: str | Path,
    entry: dict[str, Any],
    *,
    check: Callable[[list[dict[str, Any]], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Append one entry through B5.14's chained writer: under the log's exclusive lock, run
    `check(rows, entry)` (it may refuse, or fill in fields such as an experiment id), then check
    the chain and append in one write. A ledger file created for a refused entry is removed."""
    if "prev" in entry:
        raise LedgerError("an entry may not set prev: the ledger sets it")
    try:
        H._refuse_reserved(entry)
    except H.HeldoutError as exc:
        raise LedgerError(str(exc)) from None
    p = Path(path)
    existed = p.exists()
    try:
        with H._log_lock(p):
            rows = read_entries(p)
            if rows and not all("prev" in r for r in rows):
                raise LedgerError(
                    f"{p.name} has unchained lines: only a spend ledger may start with them"
                )
            if check is not None:
                check(rows, entry)
            return H._append_locked(p, entry, doing="appending")
    except H.HeldoutError as exc:
        _tidy(p, existed)
        raise LedgerError(f"{p.name}: {exc}") from None
    except BaseException:
        _tidy(p, existed)
        raise


def _tidy(p: Path, existed: bool) -> None:
    if not existed:
        try:
            if p.is_file() and p.stat().st_size == 0:
                p.unlink()
        except OSError:
            pass


# ---- the spend ledger's chain, with a sealed legacy prefix (OWNER D-73) -------------------------


def _parsed(lines: Sequence[str]) -> list[Any]:
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(None)
    return out


# Legacy prefixes EvalKit has sealed, recorded in code where cutting a ledger cannot remove them:
# a file holding one of these prefixes with no chained line after it had its whole chained part
# cut away (the seal included), so it is a broken chain, never a legacy file awaiting its seal.
# EvalKit's own docs/measurements/spend-ledger.jsonl: 22 legacy lines, sealed 2026-11-17.
KNOWN_SEALS: tuple[dict[str, Any], ...] = (
    {"lines": 22, "sha256": "77b7976283a07efc6beb24592ab1485cc9dfef0d6e23dc45fbe501f883333e21"},
)


def legacy_seal(prefix: Sequence[str]) -> dict[str, Any]:
    """What the first chained line records about the unchained lines before it: their count and
    the sha256 of their exact text (each line and its newline), so editing any of them, not only
    the last, breaks the seal."""
    return {"lines": len(prefix), "sha256": sha256_text("".join(x + "\n" for x in prefix))}


def chain_state(path: str | Path) -> tuple[list[str], str, int]:
    """(problems, state, legacy lines) of a ledger that may start with unchained lines. State:
    `empty`, `unchained` (no chained line yet: another project's ledger, or a legacy file not yet
    sealed), `chained` (every line chained from GENESIS) or `sealed` (legacy lines sealed by the
    first chained line, which names the last legacy line's sha256 as `prev` and records the
    prefix's count and sha256)."""
    p = Path(path)
    if not p.is_file():
        return [], "empty", 0
    lines = H._lines(p)
    if not lines:
        return [], "empty", 0
    rows = _parsed(lines)
    first = next((i for i, r in enumerate(rows) if isinstance(r, dict) and "prev" in r), None)
    problems: list[str] = []
    for n, r in enumerate(rows, start=1):
        if not isinstance(r, dict):
            problems.append(f"line {n} is not a JSON object")
    if first is None:
        for n, r in enumerate(rows, start=1):
            if isinstance(r, dict) and r.get("schema") == LEDGER_SCHEMA:
                problems.append(f"line {n} is a {LEDGER_SCHEMA} entry without prev")
        for seal in KNOWN_SEALS:
            k = int(seal["lines"])
            if k <= len(lines) and legacy_seal(lines[:k]) == seal:
                problems.append(
                    f"lines 1-{k} are a legacy prefix EvalKit has already sealed, but no chained "
                    "line follows: the chained part (its seal included) was cut away"
                )
        return problems, "unchained", len(lines)
    prefix = lines[:first]
    for n, r in enumerate(rows[:first], start=1):
        if isinstance(r, dict) and r.get("schema") == LEDGER_SCHEMA:
            problems.append(f"line {n} is a {LEDGER_SCHEMA} entry without prev")
    expected = sha256_text(prefix[-1]) if prefix else H.GENESIS
    head = rows[first]
    if prefix and (not isinstance(head, dict) or head.get("sealed") != legacy_seal(prefix)):
        problems.append(
            f"line {first + 1} does not seal the {len(prefix)} unchained line(s) before it "
            "(a legacy line was edited, added or dropped)"
        )
    for n in range(first, len(lines)):
        r = rows[n]
        if isinstance(r, dict) and "prev" not in r:
            problems.append(f"line {n + 1} is unchained after the chained part began")
        elif isinstance(r, dict) and r.get("prev") != expected:
            problems.append(f"line {n + 1} does not follow line {n} (edited, dropped or reordered)")
        expected = sha256_text(lines[n])
    return problems, ("sealed" if prefix else "chained"), len(prefix)


def append_spend_line(
    path: str | Path,
    entry: dict[str, Any],
    *,
    check: Callable[[list[dict[str, Any]], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Append a chained line to a spend ledger, under B5.14's lock, in one write. The first
    chained line after legacy lines names the last one's sha256 as `prev` and records their
    seal; a broken chain or seal is refused."""
    if "prev" in entry or "sealed" in entry:
        raise LedgerError("an entry may not set prev or sealed: the ledger sets them")
    p = Path(path)
    existed = p.exists()
    try:
        with H._log_lock(p):
            problems, state, n_legacy = chain_state(p)
            if problems:
                raise LedgerError(f"{p.name}: the chain is broken ({problems[0]}): restore it")
            lines = H._lines(p) if p.is_file() else []
            rows = [r for r in _parsed(lines) if isinstance(r, dict)]
            if check is not None:
                check(rows, entry)
            body = dict(entry)
            if state == "unchained" and lines:
                body["sealed"] = legacy_seal(lines[:n_legacy])
            prev = sha256_text(lines[-1]) if lines else H.GENESIS
            text = canonical_json({**body, "prev": prev})
            raw = p.read_bytes() if p.is_file() else b""
            lead = "\n" if raw and not raw.endswith(b"\n") else ""
            H._write_lines(p, lead + text + "\n")
            return json.loads(text)
    except H.HeldoutError as exc:
        _tidy(p, existed)
        raise LedgerError(f"{p.name}: {exc}") from None
    except BaseException:
        _tidy(p, existed)
        raise


# ---- verify (history verify) --------------------------------------------------------------------


def _shape_problems(rows: Sequence[Any]) -> list[str]:
    out: list[str] = []
    projects: set[str] = set()
    for n, r in enumerate(rows, start=1):
        if not isinstance(r, dict) or r.get("schema") != LEDGER_SCHEMA:
            continue
        for key in ("kind", "at", "project"):
            if not isinstance(r.get(key), str) or not r.get(key):
                out.append(f"line {n}: a {LEDGER_SCHEMA} entry needs {key}")
        if isinstance(r.get("project"), str):
            projects.add(r["project"])
        for key in ("note", "hypothesis", "change", "reason"):
            if isinstance(r.get(key), str) and len(r[key]) > MAX_TEXT:
                out.append(f"line {n}: {key} is longer than {MAX_TEXT} characters")
    if len(projects) > 1:
        out.append(f"the ledger holds {len(projects)} projects; one ledger holds one project")
    return out


def verify_ledger(
    path: str | Path,
    revisions: Sequence[str] | None = None,
    *,
    default_tip: str | None = None,
    root: str | Path | None = None,
) -> list[str]:
    """Problems with a ledger (empty when sound): its hash chain (a spend ledger's sealed legacy
    prefix included), each entry's shape, one project per ledger, and, when `revisions` is given
    (CI: `heldout.ci_revisions`), B5.14's append-only check against git."""
    p = Path(path)
    if not p.is_file():
        raise LedgerError(f"{p}: no such ledger")
    lines = H._lines(p)
    rows = _parsed(lines)
    is_spend = any(isinstance(r, dict) and r.get("kind") == "spend" for r in rows)
    unchained_lead = bool(rows) and not (isinstance(rows[0], dict) and "prev" in rows[0])
    if is_spend or unchained_lead:
        problems, state, n_legacy = chain_state(p)
        chained = [r for r in rows[n_legacy:] if isinstance(r, dict)]
        if n_legacy and any(r.get("kind") != "spend" for r in chained):
            problems.append("only a spend ledger may start with unchained lines")
        if state == "unchained" and is_spend:
            problems.append("a spend ledger with spend entries but no chained line")
    else:
        problems = H.chain_problems(p)
    problems += _shape_problems(rows)
    if revisions is not None:
        try:
            problems += H.append_only_problems(p, revisions, default_tip=default_tip, root=root)
        except H.HeldoutError as exc:
            problems.append(str(exc))
    return list(dict.fromkeys(problems))
