"""Held-out discipline and tamper-evident logs (B5.14, D-65).

Four rules every project with a held-out split re-implements, in one place:

- HLD-01 a held-out split pinned by a committed manifest of hashes and counts, verified
  against the case files; results leave only as split-level aggregates.
- HLD-02 score once: each held-out case is scored at most once per rubric hash, recorded as
  a line of the HLD-04 log.
- HLD-03 exposure scan: held-out text (or a long-enough run of its words) in a git-tracked
  file fails the build.
- HLD-04 a hash-chained, append-only JSONL log, checked against git revisions; a scoring
  counts as certified only with a matching logged line.

Generic over any case file (JSONL, JSON, YAML or a folder of them) with an id field. Every
message names manifest ids (the first 12 hex characters of a case id's sha256), counts, file
paths or revisions, never held-out text. Lifted from course-assistant's tuning log and
release gate (its D-54, D-55, D-57, D-59) and assay-pipeline's held-out manifest (MIT).

    python -m evalkit.heldout manifest --split heldout=heldout/cases.jsonl --out M.json
    python -m evalkit.heldout verify --manifest M.json --split heldout=heldout/cases.jsonl
    python -m evalkit.heldout scan --cases heldout/cases.jsonl
    python -m evalkit.heldout verify-log heldout-log.jsonl --base "$BEFORE" --default-branch main
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "GENESIS",
    "AlreadyScored",
    "Exposure",
    "HeldoutError",
    "SplitAggregate",
    "append_entry",
    "append_only_problems",
    "build_manifest",
    "case_id_hash",
    "chain_problems",
    "ci_revisions",
    "content_hash",
    "exposure_scan",
    "ids_in_text",
    "is_certified",
    "load_cases",
    "main",
    "manifest_id",
    "read_log",
    "record_scoring",
    "scored_before",
    "split_aggregate",
    "tracked_files",
    "verify_manifest",
    "write_manifest",
]

MANIFEST_SCHEMA = "evalkit.heldout.manifest/1"
GENESIS = "0" * 64
SCORED = "heldout_scored"
# Reserved for B5.9's calibration pair (`evalkit.integrity`), written only by its own writer:
# the public `append_entry` and `record_scoring` refuse both.
CALIBRATION_SCORED = "calibration_scored"
CALIBRATION_PREFIX = "calibration:"
EXPOSURE_MIN_CHARS = 25  # course-assistant D-54
EXPOSURE_WINDOW = 10  # words
_SUFFIXES = (".jsonl", ".json", ".yaml", ".yml")


class HeldoutError(ValueError):
    """A held-out rule refused the request. Messages carry ids, counts and paths, never text."""


class AlreadyScored(HeldoutError):
    """A held-out case was already scored under this rubric (HLD-02)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


# =============================== HLD-01 cases and manifest ===============================


def case_id_hash(case_id: str) -> str:
    """sha256 hex of a case id, after Unicode NFC normalisation (so an id typed precomposed
    and the same id typed decomposed are one case). ASCII ids are unchanged by NFC."""
    return _sha(unicodedata.normalize("NFC", str(case_id)))


def manifest_id(case_id: str) -> str:
    """The printable handle of a case: the first 12 hex characters of its id hash."""
    return case_id_hash(case_id)[:12]


def content_hash(case: Mapping[str, Any], *, id_field: str = "id") -> str:
    """sha256 of the case without its id, as canonical JSON (sorted keys, compact, ASCII;
    non-JSON scalars such as YAML dates as `str`). The id is pinned by its own hash, so the
    same case under another id is still recognised. Raises `HeldoutError` for a case that has
    no canonical form (keys of mixed types, a self-referencing value)."""
    try:
        return _sha(_canonical({k: v for k, v in case.items() if k != id_field}))
    except (TypeError, ValueError, RecursionError):  # the message may quote the case
        raise HeldoutError("a case has keys of mixed types or a self-referencing value") from None


def _rows_of(data: Any, id_field: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        if id_field in data:
            return [data]
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    raise HeldoutError("holds neither a list of cases nor one case")


def _load_file(path: Path, id_field: str) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise HeldoutError(f"{path.name}: unreadable") from None
    where: list[str]
    if path.suffix == ".jsonl":
        rows: list[Any] = []
        where = []
        for n, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, RecursionError):
                raise HeldoutError(f"{path.name} line {n} is not JSON") from None
            where.append(f"line {n}")
    else:
        try:
            data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
        except (ValueError, yaml.YAMLError, RecursionError):  # messages may quote the text
            raise HeldoutError(f"{path.name} is not valid {path.suffix[1:].upper()}") from None
        if data is None:
            return []
        try:
            rows = _rows_of(data, id_field)
        except RecursionError:
            raise HeldoutError(f"{path.name} is nested too deeply") from None
        except HeldoutError as exc:
            raise HeldoutError(f"{path.name} {exc}") from None
        where = [f"case {n}" for n in range(1, len(rows) + 1)]
    out = []
    for row, at in zip(rows, where, strict=True):
        if not isinstance(row, Mapping):
            raise HeldoutError(f"{path.name} {at} is not a mapping")
        value = row.get(id_field)
        if not isinstance(value, str | int) or isinstance(value, bool) or not str(value).strip():
            raise HeldoutError(f"{path.name} {at} has no {id_field!r}")
        try:
            content_hash(row, id_field=id_field)
        except (HeldoutError, RecursionError):
            raise HeldoutError(
                f"{path.name} {at} has keys of mixed types or a self-referencing value"
            ) from None
        out.append(dict(row))
    return out


def load_cases(path: str | Path, *, id_field: str = "id") -> list[dict[str, Any]]:
    """Cases from a `.jsonl`, `.json` or `.yaml`/`.yml` file, or every such file directly
    inside a folder (sorted by name). Raises `HeldoutError` naming the file and position."""
    p = Path(path)
    if p.is_dir():
        try:
            files = sorted(f for f in p.iterdir() if f.is_file() and f.suffix in _SUFFIXES)
        except OSError:
            raise HeldoutError(f"{p.name}: unreadable folder") from None
        return [c for f in files for c in _load_file(f, id_field)]
    if not p.is_file():
        raise HeldoutError(f"{p.name}: no such case file or folder")
    if p.suffix not in _SUFFIXES:
        raise HeldoutError(f"{p.name}: not a .jsonl, .json, .yaml or .yml file")
    return _load_file(p, id_field)


def _entries(cases: Sequence[Mapping[str, Any]], id_field: str) -> list[dict[str, str]]:
    return [
        {
            "id_sha256": case_id_hash(str(c[id_field])),
            "content_sha256": content_hash(c, id_field=id_field),
        }
        for c in cases
    ]


def _with_hash(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "manifest_sha256": _sha(_canonical(body))}


def build_manifest(splits: Mapping[str, str | Path], *, id_field: str = "id") -> dict[str, Any]:
    """The manifest of hashes and counts for each named split's case file (HLD-01). Refuses a
    duplicate id within a split, a case (by id or content) in two splits, and an empty split."""
    if not splits:
        raise HeldoutError("no splits given")
    out: dict[str, list[dict[str, str]]] = {}
    seen_id: dict[str, str] = {}
    seen_content: dict[str, str] = {}
    for split in sorted(splits):
        entries = _entries(load_cases(splits[split], id_field=id_field), id_field)
        if not entries:
            raise HeldoutError(f"split {split} has no cases")
        ids = [e["id_sha256"] for e in entries]
        dup = sorted({i[:12] for i in ids if ids.count(i) > 1})
        if dup:
            raise HeldoutError(f"duplicate case id(s) in split {split}: {', '.join(dup)}")
        for e in entries:
            for key, seen in (("id_sha256", seen_id), ("content_sha256", seen_content)):
                other = seen.setdefault(e[key], split)
                if other != split:
                    raise HeldoutError(
                        f"case {e['id_sha256'][:12]} is in splits {other} and {split} "
                        f"(same {'id' if key == 'id_sha256' else 'content'})"
                    )
        out[split] = sorted(entries, key=lambda e: e["id_sha256"])
    return _with_hash(
        {
            "schema": MANIFEST_SCHEMA,
            "id_field": id_field,
            "counts": {s: len(v) for s, v in out.items()},
            "splits": out,
        }
    )


def write_manifest(
    path: str | Path, splits: Mapping[str, str | Path], *, id_field: str = "id"
) -> dict[str, Any]:
    """Build the manifest and write it (sorted keys, indent 2, LF, trailing newline)."""
    manifest = build_manifest(splits, id_field=id_field)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _split_problems(split: str, want: Any, got: list[dict[str, str]], count: Any) -> list[str]:
    if not isinstance(want, list) or not all(isinstance(e, Mapping) for e in want):
        return [f"split {split} in the manifest is not a list of entries"]
    expected = {str(e.get("id_sha256")): str(e.get("content_sha256")) for e in want}
    have: dict[str, str] = {}
    out = []
    for e in got:
        if e["id_sha256"] in have:
            out.append(f"duplicate case {e['id_sha256'][:12]} ({split})")
            continue
        have[e["id_sha256"]] = e["content_sha256"]
    out += [f"missing case {i[:12]} ({split})" for i in sorted(expected) if i not in have]
    out += [
        f"case {i[:12]} changed ({split})"
        for i in sorted(expected)
        if i in have and have[i] != expected[i]
    ]
    out += [f"unexpected case {i[:12]} ({split})" for i in sorted(have) if i not in expected]
    if count != len(got):
        out.append(f"split {split} holds {len(got)} case(s), the manifest says {count}")
    return out


def verify_manifest(
    path: str | Path, splits: Mapping[str, str | Path], *, id_field: str = "id"
) -> list[str]:
    """Problems with the case files against the committed manifest; empty when they match."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return [f"manifest {p.name} is unreadable or not JSON"]
    if not isinstance(data, dict):
        return [f"manifest {p.name} is not a JSON object"]
    out = []
    if data.get("schema") != MANIFEST_SCHEMA:
        out.append(f"manifest schema is {data.get('schema')!r}, expected {MANIFEST_SCHEMA!r}")
    body = {k: v for k, v in data.items() if k != "manifest_sha256"}
    if data.get("manifest_sha256") != _sha(_canonical(body)):
        out.append("manifest_sha256 does not match the manifest (edited by hand)")
    if data.get("id_field") != id_field:
        out.append(
            f"the manifest was built with id field {data.get('id_field')!r}, not {id_field!r}"
        )
    raw_splits, raw_counts = data.get("splits"), data.get("counts")
    pinned: dict[str, Any] = raw_splits if isinstance(raw_splits, dict) else {}
    counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}
    out += [f"missing split {s}" for s in sorted(pinned) if s not in splits]
    out += [f"unexpected split {s}" for s in sorted(splits) if s not in pinned]
    for split in sorted(set(pinned) & set(splits)):
        try:
            got = _entries(load_cases(splits[split], id_field=id_field), id_field)
        except HeldoutError as exc:
            out.append(f"split {split}: {exc}")
            continue
        out += _split_problems(split, pinned[split], got, counts.get(split))
    return out


@dataclass(frozen=True)
class SplitAggregate:
    """A split-level result: the only shape in which held-out scores leave (HLD-01)."""

    split: str
    n: int
    mean: float
    lo: float
    hi: float
    manifest_sha256: str


def _split_ids(manifest: Mapping[str, Any], split: str) -> set[str]:
    splits = manifest.get("splits")
    if not isinstance(splits, Mapping) or split not in splits:
        raise HeldoutError(f"the manifest has no split {split!r}")
    return {str(e["id_sha256"]) for e in splits[split]}


def split_aggregate(
    values: Mapping[str, float],
    manifest: Mapping[str, Any],
    *,
    split: str = "heldout",
    seed: int = 0,
    n_resamples: int = 10_000,
) -> SplitAggregate:
    """Mean and seeded percentile bootstrap interval over exactly the manifest's cases of
    `split`. A value for a case outside the split, a split case with no value, or a value
    that is not finite is refused."""
    from evalkit.stats import Resamples, interval

    want = _split_ids(manifest, split)
    by_hash = {case_id_hash(str(k)): v for k, v in values.items()}
    if len(by_hash) != len(values):
        raise HeldoutError("two values are for the same case id (after NFC normalisation)")
    stray = [h for h in by_hash if h not in want]
    if stray:
        raise HeldoutError(f"{len(stray)} value(s) are for cases outside split {split}")
    missing = sorted(h[:12] for h in want if h not in by_hash)
    if missing:
        raise HeldoutError(f"{len(missing)} {split} case(s) have no value: {', '.join(missing)}")
    xs = [float(by_hash[h]) for h in sorted(want)]
    if not all(math.isfinite(x) for x in xs):
        raise HeldoutError("every value must be finite")
    ci = interval(xs, Resamples(len(xs), n_resamples=n_resamples, seed=seed))
    return SplitAggregate(
        split=split,
        n=len(xs),
        mean=ci.point,
        lo=ci.lo,
        hi=ci.hi,
        manifest_sha256=str(manifest.get("manifest_sha256", "")),
    )


_TOKEN = re.compile(r"[\w\-.:]+")


def ids_in_text(text: str, manifest: Mapping[str, Any], *, split: str = "heldout") -> list[str]:
    """Manifest ids of `split` cases whose id appears as a token in `text` (a held-out report
    must give `[]`). Tokens are runs of letters, digits and `_-.:`, also with trailing `.:`
    removed; ids holding other characters are not detected."""
    want = _split_ids(manifest, split)
    found = set()
    for token in _TOKEN.findall(text):
        for t in {token, token.rstrip(".:")}:
            h = case_id_hash(t)
            if h in want:
                found.add(h[:12])
    return sorted(found)


# =============================== HLD-04 tamper-evident log ===============================

_RUBRIC = re.compile(r"[0-9a-z][0-9a-z._:+-]{0,127}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
LOCK_TIMEOUT = 60.0  # seconds to wait for another writer's lock on the log


def _rubric(value: Any) -> str:
    """A rubric hash, stripped and lowercased: 1 to 128 ASCII letters, digits or `._:+-`,
    starting with a letter or digit (a hex digest, `sha256:<hex>`, or a name such as
    `groundedness-v3`)."""
    if not isinstance(value, str) or not value.strip():
        raise HeldoutError("a scoring needs a non-blank rubric hash")
    norm = value.strip().lower()
    if not _RUBRIC.fullmatch(norm):
        raise HeldoutError(
            "a rubric hash is 1 to 128 ASCII letters, digits or ._:+- starting with a "
            f"letter or digit ({len(norm)} character(s) given)"
        )
    return norm


def _case_ids(case_ids: Any) -> list[str]:
    """Case ids as strings; a bare string (whose characters would each become an id) and any
    id that is not a string or an integer are refused."""
    if isinstance(case_ids, str | bytes | bytearray):
        raise HeldoutError("case_ids must be a collection of ids, not a single string")
    try:
        items = list(case_ids)
    except TypeError:
        raise HeldoutError("case_ids must be an iterable of ids") from None
    for i in items:
        if isinstance(i, bool) or not isinstance(i, str | int):
            raise HeldoutError("each case id must be a string or an integer")
    return [str(i) for i in items]


def _lines(path: Path) -> list[str]:
    raw = path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    return [x for x in raw.split("\n") if x.strip()]


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """Every entry of the log (empty when absent). Raises `HeldoutError` on a line that is
    not a JSON object."""
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for n, line in enumerate(_lines(p), start=1):
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict):
            raise HeldoutError(f"log line {n} is not a JSON object")
        out.append(row)
    return out


def _scoring_shape_ok(row: Mapping[str, Any]) -> bool:
    rubric, hashes = row.get("rubric_hash"), row.get("case_id_sha256")
    return (
        isinstance(rubric, str)
        and bool(rubric.strip())
        and isinstance(hashes, list)
        and bool(hashes)
        and all(isinstance(h, str) and _HEX64.fullmatch(h) for h in hashes)
    )


def _rescored(
    rows: Iterable[Mapping[str, Any]],
) -> Iterator[tuple[int, Mapping[str, Any], str | None, list[str]]]:
    """For each `heldout_scored` row (numbered from 1): the row, its normalised rubric hash
    (None when malformed) and the case hashes an earlier row already scored under it."""
    seen: set[tuple[str, str]] = set()
    for n, row in enumerate(rows, start=1):
        if row.get("kind") != SCORED:
            continue
        if not _scoring_shape_ok(row):
            yield n, row, None, []
            continue
        rubric = str(row["rubric_hash"]).strip().lower()
        hashes = sorted(set(row["case_id_sha256"]))
        yield n, row, rubric, [h for h in hashes if (rubric, h) in seen]
        seen.update((rubric, h) for h in hashes)


def _already(rubric: str, hashes: Iterable[str]) -> str:
    ids = sorted({h[:12] for h in hashes})
    return f"{len(ids)} held-out case(s) already scored under rubric {rubric}: " + ", ".join(ids)


def chain_problems(path: str | Path) -> list[str]:
    """Each line names the sha256 of the line before it (`prev`; `GENESIS` for the first), so
    an edited, dropped, inserted or reordered line breaks the chain. The score-once rule
    (HLD-02) is checked on the log itself: a malformed `heldout_scored` line, or one that
    scores a case already scored under the same (stripped, lowercased) rubric hash, is a
    problem however it was written."""
    prev = GENESIS
    out = []
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(_lines(Path(path)), start=1):
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict):
            out.append(f"log line {n} is not a JSON object")
            row = {}
        elif row.get("prev") != prev:
            out.append(f"log line {n} does not follow line {n - 1} (edited, dropped or reordered)")
        rows.append(row)
        prev = _sha(line)
    for n, _row, rubric, again in _rescored(rows):
        if rubric is None:
            out.append(f"log line {n} is a malformed {SCORED} entry")
        elif again:
            out.append(f"log line {n} scores {_already(rubric, again)} (HLD-02)")
    return out


@contextlib.contextmanager
def _log_lock(path: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """An exclusive lock on the log file (created when absent), held across read-check-append:
    `fcntl.flock` on POSIX; on Windows `msvcrt.locking` of one byte far past the end of the
    file, so readers of the log are never blocked. The OS releases it if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode="ab") as handle:  # binary; never written through: only locked
        fd = handle.fileno()
        _acquire(fd, path, timeout)
        try:
            yield
        finally:
            _release(fd)


_WIN_LOCK_OFFSET = 0x7FFFFFF0


def _try_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _acquire(fd: int, path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _try_lock(fd)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise HeldoutError(
                    f"{path.name}: another process holds the log lock (waited {timeout:g} s)"
                ) from None
            time.sleep(0.02)


def _release(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _append_locked(p: Path, entry: Mapping[str, Any], *, doing: str) -> dict[str, Any]:
    """Check the chain and the score-once rule, then append; the caller holds the lock."""
    return _append_many_locked(p, [entry], doing=doing)[0]


def _append_many_locked(
    p: Path, entries: Sequence[Mapping[str, Any]], *, doing: str
) -> list[dict[str, Any]]:
    """Check the chain and the score-once rule for every entry, chain each to the one before
    it, then append them all in ONE write, so a crash leaves all of them or none (a torn
    write leaves a line that is not JSON: a broken chain). The caller holds the lock."""
    lines = _lines(p) if p.is_file() else []
    if lines and chain_problems(p):
        raise HeldoutError(f"the log's hash chain is broken: restore it before {doing}")
    rows: list[Mapping[str, Any]] = list(read_log(p))
    prev = _sha(lines[-1]) if lines else GENESIS
    texts = []
    for entry in entries:
        if entry.get("kind") == SCORED:
            *_, (_, _, rubric, again) = _rescored([*rows, entry])
            if rubric is not None and again:
                raise AlreadyScored(_already(rubric, again))
        text = _canonical({**entry, "prev": prev})
        texts.append(text)
        rows.append(entry)
        prev = _sha(text)
    lead = "\n" if p.is_file() and p.stat().st_size and not p.read_bytes().endswith(b"\n") else ""
    _write_lines(p, lead + "".join(t + "\n" for t in texts))
    return [json.loads(t) for t in texts]


def _write_lines(p: Path, text: str) -> None:
    """Append `text` (whole lines) to the log in a single write."""
    view = memoryview(text.encode("utf-8"))
    with p.open(mode="ab", buffering=0) as fh:  # unbuffered: each write() is one system call
        while view:  # a regular file takes it in one write; loop only on a short write
            n = fh.write(view)
            if not n:
                raise HeldoutError(f"{p.name}: the log could not be written")
            view = view[n:]


def _reserved(entry: Mapping[str, Any]) -> str | None:
    """Why the public writers refuse an entry B5.9's calibration writer reserves (None: they
    do not). A calibration counts only as a pair written together by `evalkit.integrity`."""
    if entry.get("kind") == CALIBRATION_SCORED:
        return f"a {CALIBRATION_SCORED} entry"
    if "pair" in entry:  # a public line carrying a pair id could block an honest pair's check
        return "an entry carrying a calibration pair id"
    rubric = entry.get("rubric_hash")
    if (
        entry.get("kind") == SCORED
        and isinstance(rubric, str)
        and rubric.strip().lower().startswith(CALIBRATION_PREFIX)
    ):
        return f"a {SCORED} entry under a {CALIBRATION_PREFIX}* rubric"
    return None


def _refuse_reserved(entry: Mapping[str, Any]) -> None:
    why = _reserved(entry)
    if why:
        raise HeldoutError(
            f"{why} is written only by evalkit.integrity's calibration scoring (the pair of "
            "lines in one append), never through append_entry or record_scoring"
        )


def append_entry(path: str | Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Append one entry with `prev` set to the hash of the last line, under an exclusive lock
    on the log. Refuses a broken chain, an entry that sets `prev` itself, an entry that is not
    a mapping, a malformed `heldout_scored` entry, (`AlreadyScored`) a `heldout_scored`
    entry for a case already scored under the same rubric hash, and the entries B5.9 reserves
    for its calibration pair (a `calibration_scored` entry, or a `heldout_scored` entry whose
    rubric starts `calibration:`)."""
    if not isinstance(entry, Mapping):
        raise HeldoutError("a log entry must be a mapping")
    if "prev" in entry:
        raise HeldoutError("a log entry may not set prev: the log sets it")
    _refuse_reserved(entry)
    if entry.get("kind") == SCORED and not _scoring_shape_ok(entry):
        raise HeldoutError(
            f"a malformed {SCORED} entry: it needs a non-blank rubric_hash and a non-empty "
            "list of sha256 hex case_id_sha256"
        )
    p = Path(path)
    with _log_lock(p):
        return _append_locked(p, entry, doing="appending")


def is_certified(log: str | Path, case_ids: Iterable[str], rubric_hash: str) -> bool:
    """True only when the log exists, its chain and score-once rule hold, and a
    `heldout_scored` line has this rubric hash (normalised) and exactly these case ids (by
    hash). False on every error (unreadable log, arguments of the wrong type): it never
    raises. Append-only against git is CI's check (`append_only_problems`), not this call."""
    try:
        rubric = _rubric(rubric_hash)
        want = sorted({case_id_hash(i) for i in _case_ids(case_ids)})
        p = Path(log)
        if not want or not p.is_file() or chain_problems(p):
            return False
        return any(
            r == rubric and sorted(set(row["case_id_sha256"])) == want
            for _, row, r, _again in _rescored(read_log(p))
        )
    except Exception:  # fail closed: whatever went wrong, nothing is certified
        return False


# =============================== HLD-02 score once ===============================


def scored_before(log: str | Path, case_ids: Iterable[str], rubric_hash: str) -> list[str]:
    """Manifest ids of the given cases already scored under this rubric (normalised)."""
    rubric = _rubric(rubric_hash)
    want = {case_id_hash(i) for i in _case_ids(case_ids)}
    hit: set[str] = set()
    for _, row, r, _again in _rescored(read_log(log)):
        if r == rubric:
            hit |= want & set(row["case_id_sha256"])
    return sorted(h[:12] for h in hit)


def record_scoring(
    log: str | Path,
    case_ids: Iterable[str],
    rubric_hash: str,
    *,
    at: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record that these held-out cases were scored under this rubric; refuses (AlreadyScored)
    when any of them already was. The check and the append run under an exclusive lock on the
    log, so two processes cannot both score a case. The rubric hash is stored stripped and
    lowercased. Call it before showing the scores to anyone. `note` is logged as given: never
    put held-out text in it. A `calibration:` rubric is refused: B5.9 writes those itself."""
    ids = _case_ids(case_ids)
    hashes = sorted({case_id_hash(i) for i in ids})
    if not hashes:
        raise HeldoutError("no cases to record")
    rubric = _rubric(rubric_hash)
    _refuse_reserved({"kind": SCORED, "rubric_hash": rubric})
    entry: dict[str, Any] = {
        "kind": SCORED,
        "rubric_hash": rubric,
        "case_id_sha256": hashes,
        "n_cases": len(hashes),
        "at": at or dt.datetime.now(dt.UTC).date().isoformat(),
    }
    if note is not None:
        entry["note"] = note
    p = Path(log)
    with _log_lock(p):
        return _append_locked(p, entry, doing="scoring")


# =============================== git ===============================

SHALLOW_HINT = (
    "the clone is shallow: fetch the full history "
    "(actions/checkout with fetch-depth: 0, or git fetch --unshallow)"
)


class _Unreadable(Exception):
    """git failed to read a file at a revision for a reason other than its absence."""


class _Git:
    """git in one work tree; `run` returns None when git cannot be started."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def run(self, *args: str) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run(["git", *args], cwd=self.cwd, capture_output=True, check=False)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def out(self, *args: str) -> str | None:
        done = self.run(*args)
        if done is None or done.returncode:
            return None
        return done.stdout.decode("utf-8", errors="replace")

    def toplevel(self) -> Path | None:
        top = self.out("rev-parse", "--show-toplevel")
        return Path(top.strip()).resolve() if top and top.strip() else None

    def commit(self, rev: str) -> str | None:
        """The full sha of the commit `rev` names, or None when it is not in the clone."""
        if not rev or rev.startswith("-"):
            return None
        sha = self.out("rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
        return sha.strip() if sha and sha.strip() else None

    def exists(self, rev: str) -> bool:
        return self.commit(rev) is not None

    def entry(self, sha: str, rel: str) -> tuple[str, str] | None:
        """(type, object id) of the exact path `rel` at commit `sha`, compared byte for byte
        (case-sensitively); None when absent. Raises `_Unreadable` when git fails."""
        done = self.run("--literal-pathspecs", "ls-tree", "-z", "--full-tree", sha, "--", rel)
        if done is None or done.returncode:
            raise _Unreadable
        want = rel.encode("utf-8")
        for record in done.stdout.split(b"\0"):
            meta, sep, name = record.partition(b"\t")
            parts = meta.split()
            if sep and name == want and len(parts) == 3:
                return parts[1].decode("ascii", "replace"), parts[2].decode("ascii", "replace")
        return None

    def file_at(self, sha: str, rel: str) -> str:
        """The file at commit `sha`; empty when the path is absent there. Raises `_Unreadable`
        for any other failure (a folder at that path, a missing object, git not starting)."""
        found = self.entry(sha, rel)
        if found is None:
            return ""
        kind, oid = found
        if kind != "blob":
            raise _Unreadable
        done = self.run("cat-file", "blob", oid)
        if done is None or done.returncode:
            raise _Unreadable
        return done.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")

    def tracked_name(self, sha: str, top: Path, path: Path, rel: str) -> str | None:
        """The exact tracked path at `sha` naming the file `path`: `rel` itself, or on a
        case-insensitive file system the one tracked path that differs only in case and is the
        same file on disk. None when the file is not tracked there."""
        if self.entry(sha, rel) is not None:
            return rel
        names = self.out("ls-tree", "-r", "-z", "--name-only", "--full-tree", sha)
        if names is None:
            raise _Unreadable
        folded = rel.casefold()
        same = []
        for name in names.split("\0"):
            if name and name.casefold() == folded:
                with contextlib.suppress(OSError):
                    if os.path.samefile(top / name, path):
                        same.append(name)
        return same[0] if len(same) == 1 else None

    def shallow_cuts(self) -> set[str] | None:
        """Commits whose parents a shallow clone cut off (empty for a full clone); None when
        the clone is shallow but the list cannot be read."""
        flag = (self.out("rev-parse", "--is-shallow-repository") or "").strip()
        where = self.out("rev-parse", "--git-path", "shallow")
        path = Path(where.strip()) if where and where.strip() else None
        if path is not None and not path.is_absolute():
            path = self.cwd / path
        if flag != "true" and (path is None or not path.is_file()):
            return set()
        try:
            text = path.read_text(encoding="ascii") if path is not None else ""
        except (OSError, UnicodeDecodeError):
            return None
        cuts = {x.strip() for x in text.splitlines() if x.strip()}
        return cuts or None

    def lines(self, *args: str) -> list[str]:
        return [x for x in (self.out(*args) or "").splitlines() if x.strip()]


def _zero(rev: str | None) -> bool:
    return rev is None or not rev.strip() or set(rev.strip()) == {"0"}


def _first_diff(old: str, new: str) -> int:
    a, b = old.split("\n"), new.split("\n")
    for n, (x, y) in enumerate(zip(a, b, strict=False), start=1):
        if x != y:
            return n
    return min(len(a), len(b))


def append_only_problems(
    path: str | Path,
    revisions: Sequence[str],
    *,
    default_tip: str | None = None,
    root: str | Path | None = None,
) -> list[str]:
    """The log must be append-only against git (HLD-04): the log must be tracked at HEAD
    (under its exact, case-sensitive path); the working file starts with the file at HEAD and
    at each revision; every commit in `<rev>..HEAD` (full history, merges included) keeps the
    file of each parent as a prefix; at `default_tip` the two agree on their common length.
    Fails closed: a revision not in the clone, a commit whose parents a shallow clone cut off,
    a file git cannot read (anything but the path being absent at a revision), or no git
    repository is a problem."""
    p = Path(path).resolve()
    if not p.is_file():
        raise HeldoutError(f"{p.name}: no such log")
    git = _Git(Path(root).resolve() if root is not None else p.parent)
    top = git.toplevel()
    if top is None:
        return ["no git repository: the append-only check cannot run"]
    git = _Git(top)
    if not p.is_relative_to(top):
        return [f"the log {p.name} is not inside the git work tree"]
    head = git.commit("HEAD")
    if head is None:
        return [f"the log {p.name} is not tracked at HEAD (there is no commit yet)"]
    try:
        rel = git.tracked_name(head, top, p, p.relative_to(top).as_posix())
    except _Unreadable:
        return [f"git cannot read HEAD to find the log {p.name}"]
    if rel is None:
        return [f"the log {p.relative_to(top).as_posix()} is not tracked at HEAD (commit it)"]
    work = p.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    out: list[str] = []
    cache: dict[str, str | None] = {}

    def at(sha: str) -> str | None:
        if sha not in cache:
            try:
                cache[sha] = git.file_at(sha, rel)
            except _Unreadable:
                cache[sha] = None
                out.append(f"git cannot read {rel} at {sha[:12]} (fail closed)")
        return cache[sha]

    head_text = at(head)
    if head_text is not None and not work.startswith(head_text):
        out.append(f"the log rewrites line {_first_diff(head_text, work)} as committed at HEAD")
    cuts = git.shallow_cuts()
    if cuts is None:
        out.append(f"the shallow clone's list of cut commits cannot be read: {SHALLOW_HINT}")
        cuts = set()
    flagged: set[str] = set()
    for rev in dict.fromkeys(revisions):
        sha = git.commit(rev)
        if sha is None:
            out.append(f"revision {rev[:12]} is not in this clone (fetch the full history)")
            continue
        old = at(sha)
        if old is not None and not work.startswith(old):
            out.append(f"the log rewrites line {_first_diff(old, work)} as committed at {rev[:12]}")
        for line in git.lines(
            "rev-list", "--reverse", "--full-history", "--parents", f"{sha}..{head}"
        ):
            commit, *parents = line.split()
            if commit in cuts and commit not in flagged:
                flagged.add(commit)
                out.append(f"commit {commit[:12]} has its parents cut off: {SHALLOW_HINT}")
                continue
            new = at(commit)
            dropped = []
            for q in parents:
                old_q = at(q)
                if new is not None and old_q is not None and not new.startswith(old_q):
                    dropped.append(q)
            if dropped and commit not in flagged:
                flagged.add(commit)
                what = "merge" if len(parents) > 1 else "commit"
                out.append(
                    f"{what} {commit[:12]} drops or rewrites log lines of its parent "
                    f"{', '.join(q[:12] for q in dropped)}"
                )
    if default_tip is not None:
        tip = git.commit(default_tip)
        if tip is None:
            out.append(f"revision {default_tip[:12]} is not in this clone (fetch the full history)")
        else:
            main = at(tip)
            if main is not None and not (work.startswith(main) or main.startswith(work)):
                out.append(
                    f"the log disagrees with {default_tip} from line {_first_diff(main, work)}"
                )
    return list(dict.fromkeys(out))


def ci_revisions(
    root: str | Path, *, base: str | None = None, default_branch: str | None = None
) -> tuple[list[str], str | None]:
    """What CI compares the log with: the push base (on a new branch, whose base is empty or
    all zeros, the merge-base with the default branch tip) and every parent of HEAD; and the
    default branch tip (`origin/<branch>`, else `<branch>`; returned even when absent, so the
    check fails closed). Raises `HeldoutError` in a shallow clone that cut off HEAD's parents
    or the merge-base, telling CI to fetch the full history."""
    git = _Git(Path(root).resolve())
    tip: str | None = None
    if default_branch:
        tip = next(
            (r for r in (f"origin/{default_branch}", default_branch) if git.exists(r)),
            default_branch,
        )
    cuts = git.shallow_cuts()
    if cuts is None:
        raise HeldoutError(f"the shallow clone's cut commits cannot be read: {SHALLOW_HINT}")
    head = git.commit("HEAD")
    if head is not None and head in cuts:
        raise HeldoutError(f"HEAD {head[:12]} has its parents cut off: {SHALLOW_HINT}")
    revs: list[str] = []
    if not _zero(base):
        revs.append(str(base).strip())
    elif tip is not None and git.exists(tip):
        found = git.lines("merge-base", "HEAD", tip)
        if not found and cuts:
            raise HeldoutError(f"no merge-base of HEAD and {tip}: {SHALLOW_HINT}")
        revs += found[:1]
    parents = git.lines("rev-list", "--parents", "-n", "1", "HEAD")
    if parents:
        revs += parents[0].split()[1:]
    return list(dict.fromkeys(revs)), tip


# =============================== HLD-03 exposure scan ===============================


@dataclass(frozen=True, order=True)
class Exposure:
    """A tracked file holding held-out text: its path and the case's manifest id only."""

    path: str
    manifest_id: str


_JSON_ESCAPE = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt])')
_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
# a word broken over a line by a hyphen ("contami-\nnation"), also behind a quote or comment mark
_HYPHEN_BREAK = re.compile(r"(\w)-[ \t]*\r?\n[\s>#*/;-]*?(\w)")


def _normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    # \w keeps letters of every script; drop only the characters that are not word characters
    return " ".join(re.sub(r"[\W_]+", " ", folded).split())


def _unescape_json(text: str) -> str:
    return _JSON_ESCAPE.sub(
        lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "u" else _SIMPLE[m.group(1)],
        text,
    )


def _views(data: bytes) -> set[str]:
    """The normalised texts a file is compared as: as written, after JSON unescaping, after
    HTML unescaping, with hyphenated line breaks joined, and (when it holds NUL bytes, as
    UTF-16 or binary files do) with the NUL bytes removed. Undecodable bytes are dropped."""
    raw = [data.decode("utf-8", errors="ignore")]
    if b"\0" in data:
        raw.append(data.replace(b"\0", b"").decode("utf-8", errors="ignore"))
    texts: set[str] = set()
    for text in raw:
        unescaped = _unescape_json(text)
        for t in {text, unescaped, html.unescape(text), html.unescape(unescaped)}:
            texts.add(t)
            texts.add(_HYPHEN_BREAK.sub(r"\1\2", t))
    return {_normalise(t) for t in texts}


def _grams(words: Sequence[str], window: int) -> Iterator[str]:
    for i in range(len(words) - window + 1):
        yield " ".join(words[i : i + window])


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def tracked_files(root: str | Path) -> list[str]:
    """Paths `git ls-files` lists in the work tree at `root`, relative to it, sorted."""
    r = Path(root)
    if not r.is_dir():
        raise HeldoutError(f"{r}: not a folder")
    done = _Git(r).run("ls-files", "-z")
    if done is None or done.returncode:
        raise HeldoutError(f"{r}: not a git work tree")
    return sorted(f for f in done.stdout.decode("utf-8", errors="replace").split("\0") if f)


def _work_tree_contents(root: Path) -> Iterator[tuple[str, bytes]]:
    for rel in tracked_files(root):
        path = root / rel
        if not path.is_file():
            continue
        try:
            yield rel, path.read_bytes()
        except OSError:
            continue


def _index_contents(root: Path) -> Iterator[tuple[str, bytes]]:
    """Each index entry's staged content (`git show :path`), read in one `git cat-file`."""
    tracked_files(root)  # the same refusal for a folder that is not a work tree
    done = _Git(root).run("ls-files", "-s", "-z")
    if done is None or done.returncode:
        raise HeldoutError(f"{root}: not a git work tree")
    entries: list[tuple[str, str]] = []
    for record in done.stdout.split(b"\0"):
        meta, sep, name = record.partition(b"\t")
        parts = meta.split()
        if not sep or len(parts) != 3 or parts[0] == b"160000":  # 160000: a submodule
            continue
        entries.append((name.decode("utf-8", errors="replace"), parts[1].decode("ascii")))
    if not entries:
        return
    stdin = "".join(oid + "\n" for _, oid in entries).encode("ascii")
    try:
        batch = subprocess.run(
            ["git", "cat-file", "--batch"], cwd=root, input=stdin, capture_output=True, check=False
        )
    except (FileNotFoundError, NotADirectoryError):
        raise HeldoutError(f"{root}: git cannot be started") from None
    if batch.returncode:
        raise HeldoutError(f"{root}: git cannot read the index")
    data, pos = batch.stdout, 0
    for rel, _oid in entries:
        end = data.find(b"\n", pos)
        header = data[pos:end].split()
        if end < 0 or len(header) != 3:
            raise HeldoutError(f"{root}: git cannot read the staged {rel}")
        size = int(header[2])
        yield rel, data[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1


def exposure_scan(
    cases: Sequence[Mapping[str, Any]],
    root: str | Path,
    *,
    id_field: str = "id",
    fields: Sequence[str] | None = None,
    public: Sequence[Mapping[str, Any]] = (),
    min_chars: int = EXPOSURE_MIN_CHARS,
    window: int = EXPOSURE_WINDOW,
    index: bool = False,
) -> list[Exposure]:
    """Tracked files that hold held-out text (HLD-03): a case string of at least `min_chars`
    normalised characters on word boundaries, or any `window` consecutive words of one (at
    least `min_chars` characters). Normalised: NFKC, casefolded, every run of non-word
    characters one space. Each file is compared as written, after JSON unescaping, after HTML
    unescaping, with hyphenated line breaks joined, and with NUL bytes removed (files holding
    NUL bytes are scanned too, undecodable bytes dropped). `index=True` scans the staged
    content of each index entry instead of the work tree file (a pre-commit hook).

    `fields` limits held-out text to those top-level fields (default: every field but the
    id). A window of words that also occurs in a `public` case (a shared instruction
    template) is not held-out-specific and is ignored; a whole held-out string never is, so a
    held-out case copied into a public one is still caught."""
    if window < 1 or min_chars < 1:
        raise HeldoutError("window and min_chars must be at least 1")
    public_grams = {
        g
        for c in public
        for s in _strings(dict(c))
        for g in _grams(_normalise(s).split(" "), window)
    }
    wholes: list[tuple[str, str]] = []
    shingles: dict[str, set[str]] = {}
    if fields is not None:
        present = {k for c in cases if isinstance(c, Mapping) for k in c}
        missing = sorted(set(fields) - present)
        if missing:  # a misspelt or renamed field would otherwise turn the scan off silently
            raise HeldoutError(f"no case has the field(s) {', '.join(missing)}")
    for n, case in enumerate(cases, start=1):
        if not isinstance(case, Mapping):
            raise HeldoutError(f"case {n} is not a mapping")
        if id_field not in case:
            raise HeldoutError(f"case {n} has no {id_field!r}")
        mid = manifest_id(str(case[id_field]))
        keep = {k: v for k, v in case.items() if k != id_field and (fields is None or k in fields)}
        for s in _strings(keep):
            norm = _normalise(s)
            if len(norm) >= min_chars:
                wholes.append((f" {norm} ", mid))
            for gram in _grams(norm.split(" "), window):
                if len(gram) >= min_chars and gram not in public_grams:
                    shingles.setdefault(gram, set()).add(mid)
    r = Path(root)
    contents = _index_contents(r) if index else _work_tree_contents(r)
    found: set[Exposure] = set()
    for rel, data in contents:
        hits: set[str] = set()
        for view in _views(data):
            padded = f" {view} "
            hits |= {mid for needle, mid in wholes if needle in padded}
            if shingles:
                for gram in _grams(view.split(" "), window):
                    hits |= shingles.get(gram, set())
        found |= {Exposure(rel, mid) for mid in hits}
    return sorted(found)


# =============================== command line ===============================


def _split_args(values: Sequence[str]) -> dict[str, Path]:
    out = {}
    for v in values:
        name, sep, path = v.partition("=")
        if not sep or not name or not path:
            raise HeldoutError(f"--split takes NAME=PATH, got {v!r}")
        out[name] = Path(path)
    return out


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evalkit.heldout",
        description="Held-out manifest, exposure scan and tamper-evident log checks (B5.14). "
        "Exit codes: 0 clean, 1 a check failed, 2 an input error.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("manifest", help="write the manifest of hashes and counts")
    m.add_argument("--split", action="append", default=[], required=True, metavar="NAME=PATH")
    m.add_argument("--id-field", default="id")
    m.add_argument("--out", required=True, type=Path)
    v = sub.add_parser("verify", help="check case files against the committed manifest")
    v.add_argument("--manifest", required=True, type=Path)
    v.add_argument("--split", action="append", default=[], metavar="NAME=PATH")
    v.add_argument("--id-field", default="id")
    s = sub.add_parser("scan", help="fail if held-out text is in a git-tracked file")
    s.add_argument("--cases", required=True, type=Path)
    s.add_argument("--id-field", default="id")
    s.add_argument("--root", default=Path("."), type=Path)
    s.add_argument(
        "--field", action="append", default=None, help="a case field holding held-out text"
    )
    s.add_argument(
        "--public",
        action="append",
        default=[],
        type=Path,
        help="public cases whose shared word windows are not held-out text",
    )
    s.add_argument("--min-chars", type=int, default=EXPOSURE_MIN_CHARS)
    s.add_argument("--window", type=int, default=EXPOSURE_WINDOW)
    s.add_argument(
        "--staged",
        action="store_true",
        help="scan the staged (index) content of each file, as a pre-commit hook would",
    )
    lg = sub.add_parser("verify-log", help="check a log's hash chain and append-only history")
    lg.add_argument("log", type=Path)
    lg.add_argument("--rev", action="append", default=[], metavar="REV")
    lg.add_argument("--base", default=None, help="the push base (CI: the push's before)")
    lg.add_argument("--default-branch", default=None)
    return p


def _run(args: argparse.Namespace) -> int:
    if args.command == "manifest":
        manifest = write_manifest(args.out, _split_args(args.split), id_field=args.id_field)
        for split, n in manifest["counts"].items():
            print(f"{split}: {n}")
        print(f"manifest_sha256: {manifest['manifest_sha256']}")
        return 0
    if args.command == "verify":
        problems = verify_manifest(args.manifest, _split_args(args.split), id_field=args.id_field)
        for problem in problems:
            print(problem)
        print("manifest: OK" if not problems else f"manifest: {len(problems)} problem(s)")
        return 1 if problems else 0
    if args.command == "scan":
        cases = load_cases(args.cases, id_field=args.id_field)
        public = [c for p in args.public for c in load_cases(p, id_field=args.id_field)]
        files = tracked_files(args.root)
        found = exposure_scan(
            cases,
            args.root,
            id_field=args.id_field,
            fields=args.field,
            public=public,
            min_chars=args.min_chars,
            window=args.window,
            index=args.staged,
        )
        for e in found:
            print(f"{e.path}\t{e.manifest_id}")
        if found:
            n_cases = len({e.manifest_id for e in found})
            n_files = len({e.path for e in found})
            print(
                f"exposed: {n_cases} held-out case(s) in {n_files} of {len(files)} tracked file(s)"
            )
            return 1
        print(f"no held-out text in {len(files)} tracked file(s)")
        return 0
    log: Path = args.log
    if not log.is_file():
        raise HeldoutError(f"{log}: no such log")
    problems = chain_problems(log)
    revs = list(args.rev)
    tip = None
    if args.base is not None or args.default_branch:
        top = _Git(log.resolve().parent).toplevel()
        if top is None:
            raise HeldoutError(f"{log}: not in a git work tree")
        found_revs, tip = ci_revisions(top, base=args.base, default_branch=args.default_branch)
        revs += found_revs
    problems += append_only_problems(log, revs, default_tip=tip)
    for problem in problems:
        print(problem)
    print("log: OK" if not problems else f"log: {len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m evalkit.heldout`; returns the exit code."""
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except HeldoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
