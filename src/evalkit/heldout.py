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
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
import sys
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
    """sha256 hex of a case id."""
    return _sha(str(case_id))


def manifest_id(case_id: str) -> str:
    """The printable handle of a case: the first 12 hex characters of its id hash."""
    return case_id_hash(case_id)[:12]


def content_hash(case: Mapping[str, Any], *, id_field: str = "id") -> str:
    """sha256 of the case without its id, as canonical JSON (sorted keys, compact, ASCII;
    non-JSON scalars such as YAML dates as `str`). The id is pinned by its own hash, so the
    same case under another id is still recognised."""
    return _sha(_canonical({k: v for k, v in case.items() if k != id_field}))


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
            except ValueError:
                raise HeldoutError(f"{path.name} line {n} is not JSON") from None
            where.append(f"line {n}")
    else:
        try:
            data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
        except (ValueError, yaml.YAMLError):  # the parser's message may quote the text
            raise HeldoutError(f"{path.name} is not valid {path.suffix[1:].upper()}") from None
        if data is None:
            return []
        try:
            rows = _rows_of(data, id_field)
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
        out.append(dict(row))
    return out


def load_cases(path: str | Path, *, id_field: str = "id") -> list[dict[str, Any]]:
    """Cases from a `.jsonl`, `.json` or `.yaml`/`.yml` file, or every such file directly
    inside a folder (sorted by name). Raises `HeldoutError` naming the file and position."""
    p = Path(path)
    if p.is_dir():
        files = sorted(f for f in p.iterdir() if f.is_file() and f.suffix in _SUFFIXES)
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


def chain_problems(path: str | Path) -> list[str]:
    """Each line names the sha256 of the line before it (`prev`; `GENESIS` for the first), so
    an edited, dropped, inserted or reordered line breaks the chain."""
    prev = GENESIS
    out = []
    for n, line in enumerate(_lines(Path(path)), start=1):
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict):
            out.append(f"log line {n} is not a JSON object")
        elif row.get("prev") != prev:
            out.append(f"log line {n} does not follow line {n - 1} (edited, dropped or reordered)")
        prev = _sha(line)
    return out


def append_entry(path: str | Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Append one entry with `prev` set to the hash of the last line. Refuses a broken chain,
    an entry that sets `prev` itself, or an entry that is not a mapping."""
    if not isinstance(entry, Mapping):
        raise HeldoutError("a log entry must be a mapping")
    if "prev" in entry:
        raise HeldoutError("a log entry may not set prev: the log sets it")
    p = Path(path)
    lines: list[str] = []
    if p.is_file():
        if chain_problems(p):
            raise HeldoutError("the log's hash chain is broken: restore it before appending")
        lines = _lines(p)
    row = {**entry, "prev": _sha(lines[-1]) if lines else GENESIS}
    text = _canonical(row)
    p.parent.mkdir(parents=True, exist_ok=True)
    lead = "\n" if p.is_file() and p.stat().st_size and not p.read_bytes().endswith(b"\n") else ""
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(lead + text + "\n")
    return json.loads(text)


def is_certified(log: str | Path, case_ids: Iterable[str], rubric_hash: str) -> bool:
    """True only when the log exists, its chain holds, and a `heldout_scored` line has this
    rubric hash and exactly these case ids (by hash). Append-only against git is CI's check
    (`append_only_problems`), not part of this call."""
    p = Path(log)
    want = sorted({case_id_hash(str(i)) for i in case_ids})
    if not want or not p.is_file() or chain_problems(p):
        return False
    return any(
        e.get("kind") == SCORED
        and e.get("rubric_hash") == rubric_hash
        and isinstance(e.get("case_id_sha256"), list)
        and sorted(set(e["case_id_sha256"])) == want
        for e in read_log(p)
    )


# =============================== HLD-02 score once ===============================


def scored_before(log: str | Path, case_ids: Iterable[str], rubric_hash: str) -> list[str]:
    """Manifest ids of the given cases already scored under this rubric."""
    want = {case_id_hash(str(i)) for i in case_ids}
    hit: set[str] = set()
    for e in read_log(log):
        if e.get("kind") == SCORED and e.get("rubric_hash") == rubric_hash:
            hit |= want & set(e.get("case_id_sha256") or ())
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
    when any of them already was. Call it before showing the scores to anyone. `note` is
    logged as given: never put held-out text in it."""
    ids = [str(i) for i in case_ids]
    hashes = sorted({case_id_hash(i) for i in ids})
    if not hashes:
        raise HeldoutError("no cases to record")
    if not isinstance(rubric_hash, str) or not rubric_hash.strip():
        raise HeldoutError("a scoring needs a non-blank rubric hash")
    p = Path(log)
    if p.is_file() and chain_problems(p):
        raise HeldoutError("the log's hash chain is broken: restore it before scoring")
    done = scored_before(p, ids, rubric_hash)
    if done:
        raise AlreadyScored(
            f"{len(done)} held-out case(s) already scored under rubric {rubric_hash}: "
            + ", ".join(done)
        )
    entry: dict[str, Any] = {
        "kind": SCORED,
        "rubric_hash": rubric_hash,
        "case_id_sha256": hashes,
        "n_cases": len(hashes),
        "at": at or dt.datetime.now(dt.UTC).date().isoformat(),
    }
    if note is not None:
        entry["note"] = note
    return append_entry(p, entry)


# =============================== git ===============================


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

    def exists(self, rev: str) -> bool:
        return self.out("rev-parse", "--verify", "-q", f"{rev}^{{commit}}") is not None

    def show(self, rev: str, rel: str) -> str:
        """The file at `rev`; empty when the file is absent there."""
        text = self.out("show", f"{rev}:{rel}")
        return (text or "").replace("\r\n", "\n")

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
    """The log must be append-only against git (HLD-04): the working file starts with the file
    at HEAD and at each revision; every commit in `<rev>..HEAD` (full history, merges
    included) keeps the file of each parent as a prefix; at `default_tip` the two agree on
    their common length. A revision not in the clone, or no git repository, is a problem."""
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
    rel = p.relative_to(top).as_posix()
    work = p.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    out: list[str] = []
    if git.exists("HEAD"):
        head = git.show("HEAD", rel)
        if not work.startswith(head):
            out.append(f"the log rewrites line {_first_diff(head, work)} as committed at HEAD")
    cache: dict[str, str] = {}

    def at(sha: str) -> str:
        if sha not in cache:
            cache[sha] = git.show(sha, rel)
        return cache[sha]

    flagged: set[str] = set()
    for rev in dict.fromkeys(revisions):
        if not git.exists(rev):
            out.append(f"revision {rev[:12]} is not in this clone (fetch the full history)")
            continue
        old = at(rev)
        if not work.startswith(old):
            out.append(f"the log rewrites line {_first_diff(old, work)} as committed at {rev[:12]}")
        for line in git.lines(
            "rev-list", "--reverse", "--full-history", "--parents", f"{rev}..HEAD"
        ):
            sha, *parents = line.split()
            dropped = [q for q in parents if not at(sha).startswith(at(q))]
            if dropped and sha not in flagged:
                flagged.add(sha)
                what = "merge" if len(parents) > 1 else "commit"
                out.append(
                    f"{what} {sha[:12]} drops or rewrites log lines of its parent "
                    f"{', '.join(q[:12] for q in dropped)}"
                )
    if default_tip is not None:
        if not git.exists(default_tip):
            out.append(f"revision {default_tip[:12]} is not in this clone (fetch the full history)")
        else:
            main = at(default_tip)
            if not (work.startswith(main) or main.startswith(work)):
                out.append(
                    f"the log disagrees with {default_tip} from line {_first_diff(main, work)}"
                )
    return out


def ci_revisions(
    root: str | Path, *, base: str | None = None, default_branch: str | None = None
) -> tuple[list[str], str | None]:
    """What CI compares the log with: the push base (on a new branch, whose base is empty or
    all zeros, the merge-base with the default branch tip) and every parent of HEAD; and the
    default branch tip (`origin/<branch>`, else `<branch>`; returned even when absent, so the
    check fails closed)."""
    git = _Git(Path(root).resolve())
    tip: str | None = None
    if default_branch:
        tip = next(
            (r for r in (f"origin/{default_branch}", default_branch) if git.exists(r)),
            default_branch,
        )
    revs: list[str] = []
    if not _zero(base):
        revs.append(str(base).strip())
    elif tip is not None and git.exists(tip):
        found = git.lines("merge-base", "HEAD", tip)
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


def _normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    # \w keeps letters of every script; drop only the characters that are not word characters
    return " ".join(re.sub(r"[\W_]+", " ", folded).split())


def _unescape_json(text: str) -> str:
    return _JSON_ESCAPE.sub(
        lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "u" else _SIMPLE[m.group(1)],
        text,
    )


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


def exposure_scan(
    cases: Sequence[Mapping[str, Any]],
    root: str | Path,
    *,
    id_field: str = "id",
    fields: Sequence[str] | None = None,
    public: Sequence[Mapping[str, Any]] = (),
    min_chars: int = EXPOSURE_MIN_CHARS,
    window: int = EXPOSURE_WINDOW,
) -> list[Exposure]:
    """Tracked text files that hold held-out text (HLD-03): a case string of at least
    `min_chars` normalised characters on word boundaries, or any `window` consecutive words of
    one (at least `min_chars` characters). Normalised: NFKC, casefolded, every run of
    non-word characters one space; each file as written and after JSON unescaping.

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
    for n, case in enumerate(cases, start=1):
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
    files = tracked_files(r)
    found: set[Exposure] = set()
    for rel in files:
        path = r / rel
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue  # binary by content
        text = data.decode("utf-8", errors="replace")
        views = {_normalise(text)}
        unescaped = _unescape_json(text)
        if unescaped != text:
            views.add(_normalise(unescaped))
        hits: set[str] = set()
        for view in views:
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
