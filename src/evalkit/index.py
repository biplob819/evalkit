"""Cross-project eval index (B5.5, IDX-01, IDX-02, EFF-04; specs/B5.5-eval-index/spec.md).

Two steps, kept apart:

1. `snapshot` (`evalkit index snapshot`): the one explicit step that may reach a remote. Per
   registered project it fetches the registered summary file (and its named companions) at one
   commit with `git fetch --depth 1` and writes a local snapshot folder that records the commit.
2. `build_index` (`evalkit index build`): reads only local files (the registry, the index
   config, the snapshot folder) and renders `index.html` and `index.json`. No model call, no
   network, no clock: the same inputs give the same bytes.

Every registered project and EvalKit itself are listed. A project whose summary cannot be read
is shown as missing with the reason, never dropped and never filled from anything else
(IDX-02). Held-out rows are shown only at split level; every shown string is escaped, at most
64 characters and passed through a secret filter.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from evalkit.metrics import (
    CODE_RE,
    EFFICIENCY_NAMES,
    FIXED_UNITS,
    NAME_RE,
    SCHEMA,
    Metric,
    MetricsError,
    MetricsSummary,
    describe_validation,
    from_run_summary,
    loads_json,
    unit_kind,
    validate_metrics,
)

__all__ = [
    "CONFIG_SCHEMA",
    "EXIT_INPUT",
    "EXIT_MISSING",
    "EXIT_OK",
    "INDEX_SCHEMA",
    "SELF",
    "SNAPSHOT_SCHEMA",
    "Figure",
    "GateStatus",
    "Index",
    "IndexConfig",
    "IndexInputError",
    "ModelRow",
    "ProjectConfig",
    "ProjectEntry",
    "RegistryRow",
    "Selector",
    "SnapshotResult",
    "build_index",
    "index_json",
    "load_config",
    "parse_config",
    "read_project",
    "read_registry",
    "render_html",
    "snapshot",
    "write_index",
]

INDEX_SCHEMA = "index-1"
SNAPSHOT_SCHEMA = "index-snapshot-1"
CONFIG_SCHEMA = "index-config-1"
EXIT_OK, EXIT_MISSING, EXIT_INPUT = 0, 1, 2
SELF = "evalkit"  # EvalKit is the eighth project on its own index (D-62)
SELF_REPO = "biplob819/evalkit"
SELF_SUMMARY = "docs/results/summary.json"
MAX_FILE_BYTES = 16_000_000

# Fixed names of the fetched files inside a project's snapshot folder (content decides the
# format, never the name).
ROLE_FILES = {
    "summary": "summary.json",
    "run": "run.json",
    "gates": "gates.yaml",
    "experiments": "experiments.jsonl",
}
HELDOUT_KEPT = frozenset({"split", "model", "judge"})  # labels a held-out row may show
EFFICIENCY_KEYS = frozenset({"model", "mode", "price_date"})
KINDS = ("north-star", "adoption", "quality", "guardrail", "cost")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_SECRETS = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]{20,}"
    r"|\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{50,}"
    r"|\bpypi-[A-Za-z0-9_\-]{50,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


class IndexInputError(ValueError):
    """A registry, config or output folder the index cannot use. `problems` never echo file
    values."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


def _show(value: object, limit: int = 64) -> str:
    """A value from a project's file as the index may show it: at most `limit` characters (64
    by default; an interval method may take 128), key shapes hidden."""
    text = value if isinstance(value, str) else json.dumps(value)
    if len(text) > limit:
        text = f"<{len(text)} characters, not shown>"
    return "<hidden>" if _SECRETS.search(text) else text


# A label value shaped like a case, alert or member id in the index (stricter than metrics-1's
# own check): a prefix, a hyphen or underscore, then 8 or more hex characters or 3 or more
# digits (`a-0123456789abcdef`, `case-0042`, `member_00123`).
_ID_LABEL_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z][A-Za-z0-9]*)*[-_](?:[0-9A-Fa-f]{8,}|[0-9]{3,})"
)
ID_LABEL_PROBLEM = "a label value is shaped like an id: not shown"


def _id_label(labels: Mapping[str, Any]) -> bool:
    return any(isinstance(v, str) and _ID_LABEL_RE.fullmatch(v) for v in labels.values())


def _clean(text: str) -> str:
    """EvalKit's own message (a reason, a gate note, a stamp), which quotes file values only
    through `_show`/`safe_text`: key shapes hidden, at most 300 characters."""
    text = _SECRETS.sub("<hidden>", text)
    return text if len(text) <= 300 else text[:299] + "…"


def _named(label: str | None, metric: str | None) -> str:
    """A selector's display name: the config's `label` (EvalKit's own config, so up to 300
    characters like any EvalKit message), else the metric name from the project's file."""
    return _clean(label) if label else _show(metric or "")


# ---- registry ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryRow:
    project: str
    repo: str
    branch: str
    pin: str
    summary: str | None  # path in the project's repository; None when the registry says `-`

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "repo": self.repo,
            "branch": self.branch,
            "pin": self.pin,
            "summary": self.summary,
        }


SELF_ROW = RegistryRow(SELF, SELF_REPO, "main", "-", SELF_SUMMARY)
REGISTRY_COLUMNS = ("project", "repo", "branch", "pin", "summary")


def read_registry(path: str | Path) -> list[RegistryRow]:
    """The registered projects (docs/downstream.tsv), in file order. Raises IndexInputError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IndexInputError([f"cannot read the registry {path}: {type(exc).__name__}"]) from None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise IndexInputError(["the registry is empty"])
    header = lines[0].split("\t")
    missing = [c for c in REGISTRY_COLUMNS if c not in header]
    if missing:
        raise IndexInputError([f"the registry has no column(s) {', '.join(missing)}"])
    at = {c: header.index(c) for c in REGISTRY_COLUMNS}
    rows: list[RegistryRow] = []
    problems: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(lines[1:], start=2):
        cells = line.split("\t")
        cells += [""] * (len(header) - len(cells))
        cell = {c: cells[at[c]].strip() for c in REGISTRY_COLUMNS}
        name = cell["project"]
        if not PROJECT_RE.match(name):
            problems.append(f"registry line {i}: project must match [a-z0-9][a-z0-9._-]*")
            continue
        if name == SELF:
            problems.append(f"registry line {i}: {SELF!r} is EvalKit's own row, added by the index")
            continue
        if name in seen:
            problems.append(f"registry line {i}: project {name} is listed twice")
            continue
        if not REPO_RE.match(cell["repo"]):
            problems.append(f"registry line {i} ({name}): repo must be owner/name")
            continue
        seen.add(name)
        summary = cell["summary"] if cell["summary"] not in ("", "-") else None
        rows.append(RegistryRow(name, cell["repo"], cell["branch"] or "main", cell["pin"], summary))
    if problems:
        raise IndexInputError(problems)
    return rows


# ---- config -----------------------------------------------------------------------------------

LabelValue = StrictStr | StrictInt | StrictBool


def _path_problem(p: str) -> str | None:
    """Why a path in a project's repository is refused (None when fine)."""
    if not p or not p.strip() or "\0" in p or "\\" in p:
        return "must be a non-blank relative path with / separators"
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return "must be relative to the repository root"
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        return "must not contain empty, . or .. parts"
    if any(x == ".env" or x.startswith(".env.") for x in parts):
        return "must not name a .env file"
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---- the path selector (a project's own summary format; docs/index.md) ------------------------
#
#   path    := "$" step*
#   step    := "." field                       field access on an object
#            | "[?" cond ("&&" cond)* "]"      keep the list items whose fields all equal
#   cond    := field "==" literal
#   literal := "string" | number | true | false | null | []
#   field   := [A-Za-z_][A-Za-z0-9_]*
#
# Parsed by hand into steps and evaluated over plain JSON values: no eval, no exec, no
# attribute access, nothing read beyond the named steps.

PATH_MAX = 200
MAX_PATH_NODES = 100_000
_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LITERAL_RE = re.compile(
    r'"(?:[^"\\\x00-\x1f]|\\["\\/bfnrt])*"'
    r"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    r"|true|false|null|\[\]"
)


@dataclass(frozen=True)
class _Field:
    name: str


@dataclass(frozen=True)
class _Filter:
    conds: tuple[tuple[str, str], ...]  # (field, the literal's JSON text)


def _skip(text: str, i: int) -> int:
    while i < len(text) and text[i] == " ":
        i += 1
    return i


def parse_path(text: str) -> tuple[_Field | _Filter, ...]:
    """The steps of a path selector; raises ValueError naming the first problem."""
    if len(text) > PATH_MAX:
        raise ValueError(f"a path is at most {PATH_MAX} characters")
    if not text.startswith("$"):
        raise ValueError("a path starts with $")
    steps: list[_Field | _Filter] = []
    i = 1
    while i < len(text):
        if text[i] == ".":
            m = _FIELD_RE.match(text, i + 1)
            if m is None:
                raise ValueError(f"expected a field name after . at character {i + 1}")
            steps.append(_Field(m.group()))
            i = m.end()
        elif text.startswith("[?", i):
            i += 2
            conds: list[tuple[str, str]] = []
            while True:
                i = _skip(text, i)
                m = _FIELD_RE.match(text, i)
                if m is None:
                    raise ValueError(f"expected a field name in the filter at character {i + 1}")
                name, i = m.group(), _skip(text, m.end())
                if not text.startswith("==", i):
                    raise ValueError("a filter compares a field with == only")
                i = _skip(text, i + 2)
                lit = _LITERAL_RE.match(text, i)
                if lit is None:
                    raise ValueError(
                        "a filter value is a string in double quotes, a number, true, false, "
                        "null or []"
                    )
                conds.append((name, lit.group()))
                i = _skip(text, lit.end())
                if text.startswith("&&", i):
                    i += 2
                    continue
                if text.startswith("]", i):
                    i += 1
                    break
                raise ValueError(f"expected && or ] at character {i + 1}")
            steps.append(_Filter(tuple(conds)))
        else:
            raise ValueError(
                f"unexpected {text[i]!r} at character {i + 1}: a path has only .field and "
                "[?field == value] steps"
            )
    return tuple(steps)


def _equal(have: Any, want: Any) -> bool:
    """JSON equality with JSON's types: true is not 1, null is only null, [] only []."""
    if isinstance(want, bool) or isinstance(have, bool):
        return type(have) is type(want) and have == want
    if want is None:
        return have is None
    if isinstance(want, list):
        return isinstance(have, list) and have == want
    if isinstance(want, int | float):
        return isinstance(have, int | float) and have == want
    return isinstance(have, str) and have == want


def eval_path(path: str, data: Any) -> list[tuple[Any, Mapping[str, Any] | None]]:
    """Every value a path selects, in document order, each with the list item its last filter
    kept (None without a filter): the item is where a per-label fan-out reads its label."""
    nodes: list[tuple[Any, Mapping[str, Any] | None]] = [(data, None)]
    for step in parse_path(path):
        out: list[tuple[Any, Mapping[str, Any] | None]] = []
        if isinstance(step, _Field):
            out = [(n[step.name], s) for n, s in nodes if isinstance(n, dict) and step.name in n]
        else:
            conds = [(name, json.loads(lit)) for name, lit in step.conds]
            for n, _ in nodes:
                if isinstance(n, list):
                    out.extend(
                        (item, item)
                        for item in n
                        if isinstance(item, dict)
                        and all(k in item and _equal(item[k], v) for k, v in conds)
                    )
        if len(out) > MAX_PATH_NODES:
            raise ValueError(f"the path selects more than {MAX_PATH_NODES:,} values")
        nodes = out
    return nodes


def _unit_problem(unit: str) -> str | None:
    if unit in FIXED_UNITS or CODE_RE.match(unit):
        return None
    return "unit must be count, rate, bool, ratio, ms, s, or an upper-case code such as USD"


class Selector(_Strict):
    """One metric row: `metric` and the row's labels exactly (`*` matches any value); or, for a
    project's own summary format, a `path` (docs/index.md) that selects one figure, or one per
    value of the `per` field of the items its last filter keeps."""

    metric: StrictStr | None = None
    labels: dict[str, LabelValue] = Field(default_factory=dict)
    path: StrictStr | None = None  # own summary format only
    per: StrictStr | None = None  # path only: one figure per value of this field (fan-out)
    unit: StrictStr | None = None  # path only: the figure's unit (default ratio)
    ci_method: StrictStr | None = None  # path only: how the project made its interval
    label: StrictStr | None = None  # how the index names it (default: the metric name)
    direction: Literal["higher", "lower"] | None = None  # which way is better, when stated
    decimals: Annotated[int, Field(strict=True, ge=0, le=6)] | None = None  # fixed decimals
    companion: Selector | None = None  # a second figure shown beside each value (e.g. strict)
    caption: StrictStr | None = None  # the project's own words on the figure, shown beside it

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str | None) -> str | None:
        if v is not None and not NAME_RE.match(v):
            raise ValueError("metric must match [a-z][a-z0-9_.]*")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v is not None:
            parse_path(v)
        return v

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str | None) -> str | None:
        if v is not None and (problem := _unit_problem(v)):
            raise ValueError(problem)
        return v

    @model_validator(mode="after")
    def _one_kind(self) -> Selector:
        if (self.metric is None) == (self.path is None):
            raise ValueError("a selector names a metric (metrics-1, EvalKit) or a path, not both")
        if self.metric is not None and (self.per or self.unit or self.ci_method):
            raise ValueError("per, unit and ci_method belong to a path selector")
        if self.path is not None and self.labels:
            raise ValueError("a path selector has no labels: filter in the path instead")
        if self.per is not None:
            if not _FIELD_RE.fullmatch(self.per):
                raise ValueError("per must be a field name")
            if not any(isinstance(s, _Filter) for s in parse_path(self.path or "$")):
                raise ValueError("per needs a [?...] filter in the path: it reads the kept items")
        c = self.companion
        if c is not None:
            if c.companion is not None:
                raise ValueError("a companion has no companion of its own")
            if (c.path is None) != (self.path is None) or c.per != self.per:
                raise ValueError("a companion is the same kind of selector, with the same per")
        return self


class Pointer(_Strict):
    """A file in the project's repository that names its current results (a folder, or a file
    inside one, relative to the pointer's own folder); the summary is `file` in that folder."""

    path: StrictStr
    file: StrictStr

    @field_validator("path", "file")
    @classmethod
    def _paths(cls, v: str) -> str:
        if problem := _path_problem(v):
            raise ValueError(problem)
        return v


class SimulatedRule(_Strict):
    """`simulated` is true when the value at `path` equals `equals` (`true` for a flag), false
    when it holds another value, and not stated when the path selects nothing."""

    path: StrictStr
    equals: LabelValue = True

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        parse_path(v)
        return v


class OwnFormat(_Strict):
    """How to read a project's own summary format (neither metrics-1 nor EvalKit's): path
    selectors for its run id and run time, and the rule that says it is simulated."""

    run_id: StrictStr | None = None
    run_at: StrictStr | None = None
    simulated: SimulatedRule | None = None

    @field_validator("run_id", "run_at")
    @classmethod
    def _paths(cls, v: str | None) -> str | None:
        if v is not None:
            parse_path(v)
        return v


class ProjectConfig(_Strict):
    summary: StrictStr | None = None  # overrides the registry's summary cell
    pointer: Pointer | None = None  # the summary sits in the folder a pointer file names
    own_format: OwnFormat | None = None  # the project's own summary format, read with paths
    headline: Selector | None = None
    # when a headline will be designated, in the project's own words: shown after
    # "not designated" (e.g. "until E2.5"); only for a project with no headline yet
    headline_pending: StrictStr | None = None
    comparison: Selector | None = None  # an optional second row shown under the headline
    quality: Selector | None = None  # per model: `model` is added to its labels
    calibration: Selector | None = None  # metrics-1 only: the row that states calibration
    efficiency_labels: dict[str, LabelValue] = Field(default_factory=dict)
    gates: StrictStr | None = None
    experiments: StrictStr | None = None
    lessons: StrictStr | None = None
    kinds: dict[str, Literal["north-star", "adoption", "quality", "guardrail", "cost"]] = Field(
        default_factory=dict
    )

    @field_validator("summary", "gates", "experiments", "lessons")
    @classmethod
    def _paths(cls, v: str | None) -> str | None:
        if v is not None and (problem := _path_problem(v)):
            raise ValueError(problem)
        return v

    @model_validator(mode="after")
    def _consistent(self) -> ProjectConfig:
        if self.summary is not None and self.pointer is not None:
            raise ValueError("give summary or pointer, not both")
        if self.headline_pending is not None:
            if self.headline is not None:
                raise ValueError("headline_pending is for a project with no headline yet")
            if not self.headline_pending.strip() or len(self.headline_pending) > 120:
                raise ValueError("headline_pending is 1 to 120 characters")
        shown = [s for s in (self.headline, self.comparison) if s is not None]
        if self.own_format is not None:
            if any(s.path is None for s in shown):
                raise ValueError("an own_format project's headline and comparison use path")
            if self.quality or self.calibration or self.gates or self.efficiency_labels:
                raise ValueError(
                    "an own_format summary has no metrics-1 rows: no quality, calibration, "
                    "efficiency_labels or gates"
                )
        elif any(s.path is not None for s in [*shown, self.quality, self.calibration] if s):
            raise ValueError("a path selector needs own_format (metrics-1 rows use metric)")
        if self.quality is not None and self.quality.companion is not None:
            raise ValueError("quality has no companion")
        return self


class IndexConfig(_Strict):
    schema_version: Literal["index-config-1"]
    heldout_splits: list[LabelValue] = Field(default_factory=list)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)

    def for_project(self, name: str) -> ProjectConfig:
        return self.projects.get(name) or ProjectConfig()


EMPTY_CONFIG = IndexConfig(schema_version=CONFIG_SCHEMA)


def parse_config(text: str) -> IndexConfig:
    """Validate an index config (YAML, duplicate keys refused); raises IndexInputError."""
    import yaml

    from evalkit.gates import _yaml_loader

    try:
        data = yaml.load(text, Loader=_yaml_loader())
    except (yaml.YAMLError, ValueError, RecursionError):
        raise IndexInputError(["index config: not valid YAML (or a duplicate key)"]) from None
    if not isinstance(data, dict):
        raise IndexInputError(["index config: expected a mapping"])
    try:
        return IndexConfig.model_validate(data)
    except ValidationError as exc:
        raise IndexInputError(describe_validation(exc, "index config: ")) from None


def load_config(path: str | Path | None) -> IndexConfig:
    if path is None:
        return EMPTY_CONFIG
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IndexInputError([f"cannot read the index config: {type(exc).__name__}"]) from None
    return parse_config(text)


# ---- snapshot (the explicit fetch step) -------------------------------------------------------


@dataclass
class SnapshotResult:
    project: str
    status: Literal["ok", "missing"]
    commit: str | None
    reason: str
    files: dict[str, str] = field(default_factory=dict)  # role -> path in the repository

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "status": self.status,
            "commit": self.commit,
            "reason": self.reason,
            "files": dict(sorted(self.files.items())),
        }


class _GitError(Exception):
    pass


def _git(args: Sequence[str], cwd: Path | None = None) -> bytes:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ASKPASS": "",
        "LC_ALL": "C",
    }
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, env=env, timeout=300, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitError(f"git could not run ({type(exc).__name__})") from None
    if done.returncode != 0:
        raise _GitError(f"exit {done.returncode}")
    return done.stdout


def _write_snapshot(dest: Path, meta: dict[str, Any], blobs: Mapping[str, bytes]) -> None:
    """Replace `dest` with this snapshot: files first, snapshot.json last."""
    if dest.exists():
        if not dest.is_dir() or (any(dest.iterdir()) and not (dest / "snapshot.json").is_file()):
            raise IndexInputError([f"{dest} exists and is not a snapshot folder"])
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for role, data in blobs.items():
        (dest / ROLE_FILES[role]).write_bytes(data)
    text = json.dumps(meta, indent=2, sort_keys=True) + "\n"
    (dest / "snapshot.json").write_text(text, encoding="utf-8", newline="\n")


class _PointerError(Exception):
    pass


POINTER_MAX_BYTES = 4096


def _follow_pointer(
    ptr: Pointer,
    commit: str,
    kind: Any,
    read: Any,
) -> tuple[str, dict[str, Any]]:
    """The summary path a pointer file names at this commit (the pointer holds one relative
    path, to a folder or to a file inside one; the summary is `ptr.file` in that folder), and
    what the snapshot records about it. Raises _PointerError with a fixed reason."""
    if kind(ptr.path) != "blob":
        raise _PointerError(f"pointer {ptr.path} not found at commit {commit[:7]}")
    try:
        data = read(ptr.path)
    except (_GitError, ValueError):
        raise _PointerError("pointer file could not be read") from None
    if len(data) > POINTER_MAX_BYTES:
        raise _PointerError(f"pointer file is larger than {POINTER_MAX_BYTES:,} bytes")
    try:
        lines = [ln.strip() for ln in data.decode("utf-8").splitlines() if ln.strip()]
    except UnicodeDecodeError:
        raise _PointerError("pointer file is not UTF-8") from None
    if len(lines) != 1:
        raise _PointerError("pointer file must hold exactly one path")
    target = lines[0]
    if problem := _path_problem(target):
        raise _PointerError(f"pointer names a refused path: it {problem}")
    base = ptr.path.rsplit("/", 1)[0] if "/" in ptr.path else ""
    named = f"{base}/{target}" if base else target
    what = kind(named)
    if what not in ("tree", "blob"):
        raise _PointerError(f"pointer names nothing at commit {commit[:7]}")
    folder = named if what == "tree" else (named.rsplit("/", 1)[0] if "/" in named else "")
    summary = f"{folder}/{ptr.file}" if folder else ptr.file
    if problem := _path_problem(summary):
        raise _PointerError(f"pointer gives a refused summary path: it {problem}")
    return summary, {
        "path": ptr.path,
        "target": _show(target),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _snap_one(
    row: RegistryRow,
    cfg: ProjectConfig,
    source: str,
    ref: str,
    want: str | None,
    fetched_at: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    summary_path = cfg.summary or row.summary
    local = Path(source).is_dir()
    meta: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA,
        "project": row.project,
        "repo": row.repo,
        "source": "local" if local else source,
        "ref": ref,
        "commit": None,
        "fetched_at": fetched_at,
        "status": "missing",
        "reason": "",
        "evalkit_pin": row.pin if row.pin not in ("", "-") else None,
        "evalkit_version": None,
        "lessons": None,
        "files": {},
    }
    if summary_path is None and cfg.pointer is None:
        meta["reason"] = "no summary registered in the registry"
        return meta, {}
    paths = {"summary": summary_path, "gates": cfg.gates, "experiments": cfg.experiments}
    pointer = cfg.pointer.path if cfg.pointer is not None else None
    for role, p in [*paths.items(), ("lessons", cfg.lessons), ("pointer", pointer)]:
        if p is not None and (problem := _path_problem(p)):
            meta["reason"] = f"{role} path refused: {problem}"
            return meta, {}
    blobs: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="evalkit-index-") as tmp:
        repo = Path(tmp)
        try:
            _git(["init", "-q", "--bare", str(repo)])
            url = str(Path(source).resolve()) if local else source
            _git(["fetch", "-q", "--depth", "1", "--no-tags", url, ref], cwd=repo)
            commit = _git(["rev-parse", "FETCH_HEAD^{commit}"], cwd=repo).decode().strip()
        except _GitError as exc:
            meta["reason"] = f"git fetch failed ({exc})"
            return meta, {}
        meta["commit"] = commit
        if want is not None and not commit.startswith(want):
            meta["reason"] = "the fetched commit is not the one asked for"
            return meta, {}

        def kind(p: str) -> str | None:
            try:
                return _git(["cat-file", "-t", f"{commit}:{p}"], cwd=repo).decode().strip()
            except _GitError:
                return None

        def exists(p: str) -> bool:
            return kind(p) == "blob"

        def read(p: str) -> bytes:
            size = int(_git(["cat-file", "-s", f"{commit}:{p}"], cwd=repo).decode().strip())
            if size > MAX_FILE_BYTES:
                raise _GitError(f"larger than {MAX_FILE_BYTES:,} bytes")
            return _git(["cat-file", "blob", f"{commit}:{p}"], cwd=repo)

        if cfg.pointer is not None:
            try:
                summary_path, meta["pointer"] = _follow_pointer(cfg.pointer, commit, kind, read)
            except _PointerError as exc:
                meta["reason"] = str(exc)
                return meta, {}
            paths["summary"] = summary_path
        assert summary_path is not None
        if summary_path.rsplit("/", 1)[-1] == "summary.json":
            beside = summary_path.rsplit("/", 1)[0] + "/run.json" if "/" in summary_path else ""
            paths["run"] = beside or "run.json"
        if not exists(summary_path):
            meta["reason"] = f"summary file not found at commit {commit[:7]}"
            return meta, {}
        for role, p in paths.items():
            if p is None or (role != "summary" and not exists(p)):
                continue
            try:
                data = read(p)
            except (_GitError, ValueError) as exc:
                meta["reason"] = f"{role} file could not be read ({exc})"
                return meta, {}
            blobs[role] = data
            meta["files"][role] = {
                "path": p,
                "name": ROLE_FILES[role],
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        if cfg.lessons is not None and exists(cfg.lessons):
            meta["lessons"] = cfg.lessons
        if row.project == SELF and exists("pyproject.toml"):
            with contextlib.suppress(_GitError, ValueError):
                text = read("pyproject.toml").decode("utf-8")
                found = re.search(r'^version\s*=\s*"([^"\n]{1,32})"', text, re.M)
                meta["evalkit_version"] = found.group(1) if found else None
        elif row.project != SELF:
            # the EvalKit pin as the project itself states it at this commit (the registry's
            # cell can go stale): its pyproject.toml requirement and the version uv.lock chose
            meta["evalkit_declared"] = []
            if exists("pyproject.toml"):
                try:
                    meta["evalkit_declared"] = _declared(read("pyproject.toml"))
                except (_GitError, ValueError):
                    meta["evalkit_declared"] = None  # unreadable: not known, never guessed
            if exists("uv.lock"):
                with contextlib.suppress(_GitError, ValueError):
                    meta["evalkit_locked"] = _locked(read("uv.lock"))
    meta["status"], meta["reason"] = "ok", ""
    return meta, blobs


EVALKIT_DIST = "evalkit-harness"


def _dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement(req: str) -> str | None:
    """An `evalkit-harness` requirement as shown (canonical name, operators without spaces,
    credentials in a URL hidden), or None for another distribution."""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)\Z", req, re.S)
    if m is None or _dist_name(m.group(1)) != EVALKIT_DIST:
        return None
    extras = re.sub(r"\s+", "", m.group(2) or "")
    rest = m.group(3).strip()
    if rest.startswith("@"):
        rest = " @ " + re.sub(r"\s+", " ", rest[1:].strip())
    else:
        rest = re.sub(r"\s+", "", rest)
    text = re.sub(r"://[^/@\s]+@", "://<hidden>@", f"{EVALKIT_DIST}{extras}{rest}")
    return _clean(text)


def _declared(data: bytes) -> list[str]:
    """Every distinct EvalKit requirement in a pyproject.toml: `project.dependencies`, its
    optional dependencies and its dependency groups. Raises ValueError when unreadable."""
    import tomllib

    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ValueError("pyproject.toml is not valid TOML") from None
    found_project = doc.get("project")
    project: dict[str, Any] = found_project if isinstance(found_project, dict) else {}
    lists: list[Any] = [project.get("dependencies")]
    for table in (project.get("optional-dependencies"), doc.get("dependency-groups")):
        if isinstance(table, dict):
            lists += list(table.values())
    found: list[str] = []
    for items in lists:
        for item in items if isinstance(items, list) else []:
            req = _requirement(item) if isinstance(item, str) else None
            if req is not None and req not in found:
                found.append(req)
    return found


def _locked(data: bytes) -> str | None:
    """The EvalKit version uv.lock resolved (with the git commit when it is a git source)."""
    import tomllib

    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    packages = doc.get("package")
    for pkg in packages if isinstance(packages, list) else []:
        if not isinstance(pkg, dict) or _dist_name(str(pkg.get("name", ""))) != EVALKIT_DIST:
            continue
        version = pkg.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z.+!-]{1,32}", version):
            return None
        source = pkg.get("source")
        git = source.get("git") if isinstance(source, dict) else None
        sha = re.search(r"#([0-9a-f]{40})\Z", git) if isinstance(git, str) else None
        return f"{version} (git {sha.group(1)[:12]})" if sha else version
    return None


def snapshot(
    registry: str | Path | Sequence[RegistryRow],
    out: str | Path,
    *,
    config: IndexConfig | str | Path | None = None,
    sources: Mapping[str, str] | None = None,
    commits: Mapping[str, str] | None = None,
    root: str | Path | None = None,
    now: str | None = None,
) -> list[SnapshotResult]:
    """Fetch each registered project's summary at one commit into `out/<project>/` (the only
    step of the index that may reach a remote). `sources` maps a project to a git URL or a
    local repository (default `https://github.com/<repo>.git`; EvalKit's own row: `root`, the
    current directory by default); `commits` maps a project to the commit to read (default: its
    registry branch; EvalKit: HEAD). A failed fetch is recorded as `status: missing` with a
    fixed reason, never git's output. Raises IndexInputError for a bad registry or config."""
    rows = list(registry) if not isinstance(registry, str | Path) else read_registry(registry)
    cfg = config if isinstance(config, IndexConfig) else load_config(config)
    sources, commits = dict(sources or {}), dict(commits or {})
    known = {r.project for r in rows} | {SELF}
    unknown = sorted((set(sources) | set(commits)) - known)
    if unknown:
        raise IndexInputError([f"--source/--commit name unknown project(s): {', '.join(unknown)}"])
    for name, sha in commits.items():
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise IndexInputError([f"--commit {name}: give a hex commit (7 to 40 characters)"])
    fetched_at = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path(out)
    results: list[SnapshotResult] = []
    for row in [*rows, SELF_ROW]:
        is_self = row.project == SELF
        default = str(Path(root or Path.cwd())) if is_self else f"https://github.com/{row.repo}.git"
        source = sources.get(row.project, default)
        want = commits.get(row.project)
        ref = want or ("HEAD" if is_self else row.branch)
        meta, blobs = _snap_one(row, cfg.for_project(row.project), source, ref, want, fetched_at)
        _write_snapshot(out / row.project, meta, blobs if meta["status"] == "ok" else {})
        results.append(
            SnapshotResult(
                row.project,
                meta["status"],
                meta["commit"],
                meta["reason"],
                {k: v["path"] for k, v in meta["files"].items()} if meta["status"] == "ok" else {},
            )
        )
    return results


# ---- figures ----------------------------------------------------------------------------------


def _money(v: float, code: str) -> str:
    if code != "USD":
        return f"{_num(v)} {code}"
    from evalkit.report.svg import money

    return "-" + money(-v) if v < 0 else money(v)


def _num(v: float) -> str:
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.2f}" if abs(v) >= 100 else f"{v:.4g}"


def _fmt(unit: str, v: bool | int | float, decimals: int | None = None) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    kind = unit_kind(unit)
    if decimals is not None:  # the selector's fixed decimals (a rate as a percentage)
        if kind == "rate":
            return f"{v * 100:.{decimals}f}%"
        text = f"{v:,.{decimals}f}"
        if kind == "currency":
            if unit != "USD":
                return f"{text} {unit}"
            return f"-${text[1:]}" if text.startswith("-") else f"${text}"
        return {"ms": f"{text} ms", "s": f"{text} s"}.get(kind, text)
    if kind == "rate":
        return f"{v:.1%}"
    if kind == "currency":
        return _money(v, unit)
    if kind == "ms":
        return f"{v:,.0f} ms"
    if kind == "s":
        return f"{v:.3g} s"
    if kind == "count":
        return f"{v:,}" if isinstance(v, int) else f"{v:,.1f}"
    return _num(v)


@dataclass
class Figure:
    """One metric row as the index shows it, or why it is not shown (`problem`)."""

    metric: str
    labels: dict[str, str] = field(default_factory=dict)
    unit: str | None = None
    value: bool | int | float | None = None
    ci_low: float | int | None = None
    ci_high: float | int | None = None
    ci_method: str | None = None
    n: int | None = None
    k: int | None = None
    text: str = "—"
    note: str = ""
    problem: str | None = None
    heldout: bool = False
    direction: str | None = None  # "lower is better" / "higher is better", when configured
    direction_in_label: bool = False  # the label already says it: not repeated beside the value
    companion: Figure | None = None  # shown beside the value (e.g. the strict score)
    companion_label: str | None = None
    rows: list[Figure] = field(default_factory=list)  # a per-label fan-out: one per value

    @property
    def shown(self) -> str:
        """The value as a sentence part: the value, its companion, or each fanned-out row."""
        if self.problem:
            return self.problem
        if self.rows:
            return "; ".join(f"{_row_name(r)}: {r.shown}" for r in self.rows)
        if self.companion is not None:
            label = self.companion_label or self.companion.metric
            return f"{self.text} ({label} {self.companion.shown})"
        return self.text

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "labels": dict(sorted(self.labels.items())),
            "unit": self.unit,
            "value": self.value,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "ci_method": self.ci_method,
            "n": self.n,
            "k": self.k,
            "text": self.text,
            "shown": self.shown,
            "note": self.note,
            "problem": self.problem,
            "heldout": self.heldout,
            "direction": self.direction,
            "direction_in_label": self.direction_in_label,
            "companion": None if self.companion is None else self.companion.as_dict(),
            "companion_label": self.companion_label,
            "rows": [r.as_dict() for r in self.rows],
        }


def _figure(
    m: Metric,
    *,
    heldout: bool = False,
    note: str = "",
    scale: float = 1.0,
    decimals: int | None = None,
) -> Figure:
    value: bool | int | float = m.value
    lo, hi = m.ci_low, m.ci_high
    if scale != 1.0 and not isinstance(value, bool):
        value = value * scale
        lo = None if lo is None else lo * scale
        hi = None if hi is None else hi * scale
    labels = {
        _show(k): _show(v) for k, v in sorted(m.labels.items()) if not heldout or k in HELDOUT_KEPT
    }
    text = _fmt(m.unit, value, decimals)
    kind = unit_kind(m.unit)
    if kind == "rate" and m.k is not None and m.n and m.k in (0, m.n):
        from evalkit.stats import rate_wording

        text = rate_wording(m.k, m.n)
    elif lo is not None and hi is not None:
        text += f" [{_fmt(m.unit, lo, decimals)}, {_fmt(m.unit, hi, decimals)}]"
    return Figure(
        metric=_show(m.name),
        labels=labels,
        unit=m.unit,
        value=value,
        ci_low=lo,
        ci_high=hi,
        ci_method=_show(m.ci_method, 128) if m.ci_method is not None else None,
        n=m.n,
        k=m.k,
        text=text,
        note=note,
        heldout=heldout,
    )


def _problem(metric: str, problem: str, labels: Mapping[str, Any] | None = None) -> Figure:
    return Figure(
        metric=_show(metric),
        labels={_show(k): _show(v) for k, v in sorted((labels or {}).items())},
        problem=problem,
    )


def _same(have: Any, want: Any) -> bool:
    return want == "*" or (type(have) is type(want) and have == want)


def _is_heldout(labels: Mapping[str, Any], heldout: frozenset[str]) -> bool:
    from evalkit.gates import canonical

    return "split" in labels and canonical(labels["split"]) in heldout


def _select_row(
    metrics: MetricsSummary,
    sel: Selector,
    heldout: frozenset[str],
    extra: Mapping[str, Any] | None = None,
) -> Figure:
    """The one row a metric selector names: its metric and exactly its labels."""
    metric = sel.metric or ""
    want: dict[str, Any] = {**sel.labels, **(extra or {})}
    named = [m for m in metrics.metrics if m.name == metric]
    if not named:
        return _problem(metric, f"no metric named {_show(metric)}", want)
    rows = [
        m
        for m in named
        if set(m.labels) == set(want) and all(_same(m.labels[k], v) for k, v in want.items())
    ]
    if not rows:
        return _problem(metric, "matches no metric row", want)
    if len(rows) > 1:
        return _problem(metric, f"matches {len(rows)} rows (name every label)", want)
    m = rows[0]
    if _id_label(m.labels):
        return _problem(metric, ID_LABEL_PROBLEM, sel.labels)
    held = _is_heldout(m.labels, heldout)
    if held and not set(m.labels) <= HELDOUT_KEPT:
        return _problem(metric, "a held-out row below split level is not shown", want)
    note = "held-out split aggregate" if held else ""
    return _figure(m, heldout=held, note=note, decimals=sel.decimals)


DIRECTIONS = {"lower": "lower is better", "higher": "higher is better"}


def _select(
    metrics: MetricsSummary,
    sel: Selector,
    heldout: frozenset[str],
    extra: Mapping[str, Any] | None = None,
) -> Figure:
    """A metric selector's row, with its direction and its companion row beside it."""
    fig = _select_row(metrics, sel, heldout, extra)
    if fig.problem is None:
        fig.direction = DIRECTIONS.get(sel.direction or "")
        c = sel.companion
        if c is not None:
            fig.companion = _select_row(metrics, c, heldout, extra)
            fig.companion_label = _named(c.label, c.metric)
    return _captioned(fig, sel)


def _captioned(fig: Figure, sel: Selector) -> Figure:
    """The config's caption (the project's own words on the figure) joins the figure's note."""
    if sel.caption and fig.problem is None:
        fig.note = "; ".join(x for x in (fig.note, _clean(sel.caption)) if x)
    return fig


# ---- a project's own summary format (path selectors) ------------------------------------------


def _node_metric(name: str, node: Any, sel: Selector, labels: Mapping[str, Any]) -> Metric:
    """A selected value as a metrics-1 row: a number, or an object holding `point` with
    optional `lo`/`hi` (its interval) and `k`/`n` (its exact count). Raises ValueError."""
    row: dict[str, Any] = {"name": name, "unit": sel.unit or "ratio", "labels": dict(labels)}
    if isinstance(node, dict):
        if "point" not in node:
            raise ValueError("is not a figure: an object without point")
        row["value"] = node["point"]
        lo, hi = node.get("lo"), node.get("hi")
        if lo is not None or hi is not None:
            method = sel.ci_method or "as recorded by the project"
            row.update(ci_low=lo, ci_high=hi, ci_method=method)
        for key in ("k", "n"):
            if node.get(key) is not None:
                row[key] = node[key]
    elif isinstance(node, int | float) and not isinstance(node, bool):
        row["value"] = node
    else:
        raise ValueError("is not a figure: neither a number nor an object with point")
    try:
        return Metric.model_validate(row)
    except ValidationError as exc:
        raise ValueError("is not a valid figure: " + describe_validation(exc)[0]) from None


def _path_name(path: str) -> str:
    fields = [s.name for s in parse_path(path) if isinstance(s, _Field)]
    name = (fields[-1] if fields else "value").lower()
    return name if NAME_RE.match(name) else "value"


def _path_rows(data: Any, sel: Selector, heldout: frozenset[str]) -> list[Figure] | str:
    """Every figure a path selector selects (one per `per` value), or the problem."""
    path = sel.path or "$"
    name = _path_name(path)
    try:
        found = eval_path(path, data)
    except ValueError as exc:
        return str(exc)
    out: list[Figure] = []
    for node, item in found:
        labels: dict[str, Any] = {}
        if sel.per is not None:
            value = item.get(sel.per) if item is not None else None
            if isinstance(value, bool) or not isinstance(value, str | int):
                return f"an item has no {sel.per} (a string or an integer)"
            labels[sel.per] = value
            if _id_label(labels):
                return ID_LABEL_PROBLEM
        try:
            m = _node_metric(name, node, sel, labels)
        except ValueError as exc:
            return f"the selected value {exc}"
        held = _is_heldout(m.labels, heldout)
        if held and not set(m.labels) <= HELDOUT_KEPT:
            return "a held-out row below split level is not shown"
        out.append(_figure(m, heldout=held, decimals=sel.decimals))
    return out


def _select_path(data: Any, sel: Selector, heldout: frozenset[str]) -> Figure:
    """A path selector's figure: exactly one value (or, with `per`, one per distinct label
    value, in document order), with its direction and companion."""
    metric = _path_name(sel.path or "$")
    rows = _path_rows(data, sel, heldout)
    if isinstance(rows, str):
        return _problem(metric, rows)
    if not rows:
        return _problem(metric, "matches no value")
    keys = [tuple(sorted(r.labels.items())) for r in rows]
    if len(set(keys)) != len(keys):
        what = f"per {sel.per} value" if sel.per else "value"
        return _problem(metric, f"matches {len(rows)} values, not one {what}")
    companions: dict[tuple[Any, ...], Figure] = {}
    c = sel.companion
    if c is not None:
        found = _path_rows(data, c, heldout)
        if isinstance(found, str):
            return _problem(metric, f"companion: {found}")
        for r in found:
            key = tuple(sorted(r.labels.items()))
            if key in companions:
                return _problem(metric, "companion: matches several values for one row")
            companions[key] = r
    direction = DIRECTIONS.get(sel.direction or "")
    for r, key in zip(rows, keys, strict=True):
        if sel.per is None:  # a fan-out states its direction once, on the whole figure
            r.direction = direction
        if c is not None:
            r.companion = companions.get(key) or _problem(_path_name(c.path or "$"), "no value")
            r.companion_label = _named(c.label, r.companion.metric)
    if sel.per is None:
        return _captioned(rows[0], sel)
    fanned = Figure(
        metric=_show(metric),
        unit=rows[0].unit,
        text="; ".join(f"{_row_name(r)}: {r.text}" for r in rows),
        direction=direction,
        rows=rows,
    )
    return _captioned(fanned, sel)


def _path_scalar(data: Any, path: str | None) -> str | None:
    """The one string a path selects (a run id, a timestamp), else None."""
    if path is None:
        return None
    try:
        found = eval_path(path, data)
    except ValueError:
        return None
    if len(found) != 1 or not isinstance(found[0][0], str):
        return None
    return found[0][0]


def _own_simulated(data: Any, rule: SimulatedRule | None) -> bool | None:
    """True or false only when the file states it; None when the rule's path selects nothing."""
    if rule is None:
        return None
    try:
        found = eval_path(rule.path, data)
    except ValueError:
        return None
    if len(found) != 1:
        return None
    return _equal(found[0][0], rule.equals)


# ---- gates ------------------------------------------------------------------------------------


@dataclass
class GateStatus:
    status: str  # pass | fail | error | judging pending | stopped at the cost cap | not gated
    source: str
    exit_code: int | None = None
    failed: int = 0
    warned: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "exit_code": self.exit_code,
            "failed": self.failed,
            "warned": self.warned,
            "rows": self.rows,
            "errors": self.errors,
        }


def _gate_rows(report: Any) -> list[dict[str, Any]]:
    out = []
    for r in report.rows:
        d = r.as_dict()
        out.append(
            {
                "gate": d["gate"],
                "metric": _show(d["metric"]),
                "labels": {
                    _show(k): "<hidden>" if _id_label({k: v}) else _show(v)
                    for k, v in sorted(d["labels"].items())
                },
                "op": d["op"],
                "threshold": d["threshold"],
                "bound": d["bound"],
                "observed": d["observed"],
                "unit": d["unit"],
                "ci_low": d["ci_low"],
                "ci_high": d["ci_high"],
                "severity": d["severity"],
                "result": d["result"],
                "heldout_rows": d.get("heldout_rows", 0),
                "note": _clean(d["note"]),
            }
        )
    return out


# ---- projects ---------------------------------------------------------------------------------


@dataclass
class ModelRow:
    model: str
    quality: Figure | None
    efficiency: dict[str, Figure]
    mode: str | None = None
    price_date: str | None = None
    source: str = "efficiency block"  # or "from summary (no interval)" or "none"
    rank: int | None = None  # by cost per correct, 1 = lowest
    lowest: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "quality": None if self.quality is None else self.quality.as_dict(),
            "efficiency": {k: v.as_dict() for k, v in sorted(self.efficiency.items())},
            "mode": self.mode,
            "price_date": self.price_date,
            "source": self.source,
            "cost_per_correct_rank": self.rank,
            "lowest_cost_per_correct": self.lowest,
        }


@dataclass
class ProjectEntry:
    project: str
    repo: str
    status: Literal["ok", "missing"]
    reason: str = ""
    kind: str | None = None  # metrics-1 | evalkit-summary
    commit: str | None = None
    summary_path: str | None = None
    url: str | None = None
    fetched_at: str | None = None
    run_id: str | None = None
    run_at: str | None = None
    simulated: bool | None = None
    evalkit_version: str | None = None
    evalkit_version_source: str = "not recorded"
    evalkit_pin: str | None = None
    evalkit_pin_source: str = "the registry"
    registry_pin: str | None = None
    pin_note: str = ""  # set when the registry's cell disagrees with the project's pyproject
    headline: Figure | None = None
    headline_label: str | None = None
    headline_pending: str | None = None  # the config's words after "not designated"
    headline_comparison: Figure | None = None  # the config's optional comparison row
    comparison_label: str | None = None
    pointer: dict[str, Any] | None = None  # the pointer file the summary path was read from
    calibration: dict[str, Any] | None = None
    gate: GateStatus | None = None
    stamps: list[str] = field(default_factory=list)
    models: list[ModelRow] = field(default_factory=list)
    efficiency_note: str = ""
    comparison: str = ""
    improvements: list[dict[str, Any]] = field(default_factory=list)
    lessons_url: str | None = None
    problems: list[str] = field(default_factory=list)  # parts that could not be read
    story: dict[str, Any] = field(default_factory=dict)

    @property
    def run_date(self) -> str | None:
        return self.run_at[:10] if self.run_at else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "repo": self.repo,
            "status": self.status,
            "reason": self.reason,
            "kind": self.kind,
            "commit": self.commit,
            "summary_path": self.summary_path,
            "url": self.url,
            "fetched_at": self.fetched_at,
            "run_id": self.run_id,
            "run_date": self.run_date,
            "run_at": self.run_at,
            "simulated": self.simulated,
            "evalkit_version": self.evalkit_version,
            "evalkit_version_source": self.evalkit_version_source,
            "evalkit_pin": self.evalkit_pin,
            "evalkit_pin_source": self.evalkit_pin_source,
            "registry_pin": self.registry_pin,
            "pin_note": self.pin_note,
            "headline": None if self.headline is None else self.headline.as_dict(),
            "headline_label": self.headline_label,
            "headline_pending": self.headline_pending,
            "headline_comparison": (
                None if self.headline_comparison is None else self.headline_comparison.as_dict()
            ),
            "comparison_label": self.comparison_label,
            "pointer": self.pointer,
            "calibration": self.calibration,
            "gate": None if self.gate is None else self.gate.as_dict(),
            "stamps": self.stamps,
            "models": [m.as_dict() for m in self.models],
            "efficiency_note": self.efficiency_note,
            "cost_per_correct_comparison": self.comparison,
            "improvements": self.improvements,
            "lessons_url": self.lessons_url,
            "problems": self.problems,
            "story": self.story,
        }


class _Missing(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _pin(pin: str) -> str | None:
    """The registry's EvalKit requirement line (EvalKit's own file): shown up to 300 characters."""
    return None if pin.strip() in ("", "-") else _clean(pin.strip())


def _url(repo: str, commit: str | None, path: str | None) -> str | None:
    if commit is None or path is None or not SHA_RE.match(commit) or _path_problem(path):
        return None
    return f"https://github.com/{repo}/blob/{commit}/{path}"


def _read_role(folder: Path, snap: Mapping[str, Any], role: str) -> bytes:
    """A fetched file, checked against the sha256 the snapshot recorded."""
    meta = snap["files"].get(role)
    if not isinstance(meta, Mapping):
        raise _Missing(f"no {role} file in the snapshot")
    path = folder / ROLE_FILES[role]
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _Missing(f"{role} file cannot be read ({type(exc).__name__})") from None
    if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
        raise _Missing(f"{role} file changed since the snapshot (sha256 differs)")
    return data


def _text(data: bytes, role: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Missing(f"{role} file is not UTF-8") from None


def _load_snapshot(folder: Path, row: RegistryRow, summary_path: str | None) -> dict[str, Any]:
    path = folder / "snapshot.json"
    if not path.is_file():
        if summary_path is None:
            raise _Missing("no summary registered in the registry")
        raise _Missing("not fetched: no snapshot (run `evalkit index snapshot`)")
    try:
        snap = loads_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise _Missing("snapshot.json cannot be read") from None
    except MetricsError:
        raise _Missing("snapshot.json is not valid JSON") from None
    if not isinstance(snap, dict) or snap.get("schema_version") != SNAPSHOT_SCHEMA:
        raise _Missing(f"snapshot.json is not an {SNAPSHOT_SCHEMA} file")
    if snap.get("project") != row.project:
        raise _Missing("snapshot.json belongs to another project")
    if snap.get("status") not in ("ok", "missing") or not isinstance(snap.get("files"), dict):
        raise _Missing("snapshot.json has no status or files")
    commit = snap.get("commit")
    if commit is not None and not (isinstance(commit, str) and SHA_RE.match(commit)):
        raise _Missing("snapshot.json has a commit that is not a 40-character sha")
    for role, meta in snap["files"].items():
        if role not in ROLE_FILES or not isinstance(meta, dict):
            raise _Missing("snapshot.json lists an unknown file")
        p = meta.get("path")
        if not isinstance(p, str) or _path_problem(p) or not isinstance(meta.get("sha256"), str):
            raise _Missing("snapshot.json lists a file without a safe path and sha256")
    if snap["status"] == "missing":
        reason = snap.get("reason")
        raise _Missing(_show(reason) if isinstance(reason, str) and reason else "not fetched")
    if commit is None:
        raise _Missing("snapshot.json has no commit")
    return snap


def _scrub(problem: str) -> str:
    """A parse or validation problem without anything quoted from the source: quoted keys and
    values become '…', and a dict key in a location (`labels.<key>`) becomes `<key>`."""
    problem = re.sub(r"(?<![A-Za-z])'[^']*'(?![A-Za-z])|\"[^\"]*\"", "'…'", problem)
    return re.sub(r"\blabels\.[^\s:;,]+", "labels.<key>", problem)


def _loads_summary(text: str) -> Any:
    """Strict JSON (duplicate keys refused) with a reason that never echoes the source."""
    try:
        return loads_json(text)
    except MetricsError as exc:
        first = exc.problems[0]
        if first.startswith("duplicate key"):
            first = "a duplicate key in a JSON object"
        raise _Missing(f"summary is not valid JSON: {_scrub(first)}") from None


def _parse_summary(text: str) -> tuple[MetricsSummary, Any, str, bool | None]:
    """metrics-1, or an EvalKit summary.json (strict marker); else the reason it is neither.
    The last item is `simulated` as the file states it (None when it does not)."""
    from evalkit.runfolder import marker_problem
    from evalkit.schemas import RunSummary

    data = _loads_summary(text)
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version == SCHEMA:
        try:
            metrics = validate_metrics(data)
        except MetricsError as exc:
            problems = "; ".join(_scrub(p) for p in exc.problems[:3])
            raise _Missing(f"invalid metrics-1: {problems}") from None
        stated = data.get("simulated") if isinstance(data, dict) else None
        return metrics, None, "metrics-1", stated if isinstance(stated, bool) else None
    problem = marker_problem(data, "summary.json")
    if problem is not None:
        raise _Missing(f"not metrics-1 or an EvalKit summary.json: it {_scrub(problem)}")
    try:
        summary = RunSummary.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(_scrub(p) for p in describe_validation(exc)[:3])
        raise _Missing(f"invalid EvalKit summary: {problems}") from None
    try:
        return from_run_summary(summary), summary, "evalkit-summary", None
    except (ValueError, ValidationError) as exc:
        raise _Missing(f"cannot convert the summary ({type(exc).__name__})") from None


def _run_status(folder: Path, snap: Mapping[str, Any], summary: Any, entry: ProjectEntry) -> Any:
    from evalkit.gates import RunStatus

    stopped = False
    if "run" in snap["files"]:
        try:
            run = loads_json(_text(_read_role(folder, snap, "run"), "run"))
            if not isinstance(run, dict) or run.get("run_id") != summary.run_id:
                raise _Missing("run.json does not belong to this summary's run")
            flag = run.get("stopped_at_cost_cap", False)
            if not isinstance(flag, bool):
                raise _Missing("run.json: stopped_at_cost_cap must be true or false")
            stopped = flag
        except (_Missing, MetricsError) as exc:
            why = exc.reason if isinstance(exc, _Missing) else "run.json is not valid JSON"
            entry.problems.append(f"{why}; cost-cap state unknown")
    return RunStatus(
        kind="evalkit-summary", judging=summary.judging_status, stopped_at_cost_cap=stopped
    )


def _calibration(
    metrics: MetricsSummary, summary: Any, cfg: ProjectConfig, heldout: frozenset[str]
) -> dict[str, Any]:
    if summary is not None:
        judges = [
            {
                "judge": _show(j.judge),
                "kappa": j.kappa.point,
                "kappa_low": j.kappa.lo,
                "kappa_high": j.kappa.hi,
                "n_claims": j.n_claims,
            }
            for j in summary.judges
        ]
        if summary.calibration_status == "calibrated":
            per = "; ".join(
                f"{j['judge']} kappa {j['kappa']:.2f} [{j['kappa_low']:.2f}, {j['kappa_high']:.2f}]"
                f" on {j['n_claims']} held-out claims"
                for j in judges
            )
            text = f"calibrated: {per}"
        else:
            text = "uncalibrated: judge not calibrated for this task"
        return {"status": summary.calibration_status, "text": text, "judges": judges}
    if cfg.calibration is None:
        return {"status": "not stated", "text": "not stated in the metrics-1 file", "judges": []}
    fig = _select(metrics, cfg.calibration, heldout)
    if fig.problem:
        return {
            "status": "not stated",
            "text": f"calibration row: {fig.problem}",
            "judges": [],
            "figure": fig.as_dict(),
        }
    name = cfg.calibration.label or fig.metric
    return {
        "status": "stated",
        "text": f"stated: {name} {fig.text}",
        "judges": [],
        "figure": fig.as_dict(),
    }


def _gate(
    folder: Path,
    snap: Mapping[str, Any],
    metrics: MetricsSummary,
    status: Any,
    heldout: frozenset[str],
) -> tuple[GateStatus, frozenset[str]]:
    from evalkit.gates import DEFAULT_GATES, evaluate, parse_gates

    commit = str(snap["commit"])[:7]
    if "gates" in snap["files"]:
        source = f"gates file {_show(snap['files']['gates']['path'])} at {commit}"
        try:
            gates = parse_gates(_text(_read_role(folder, snap, "gates"), "gates"))
        except _Missing as exc:
            return GateStatus("error", source, errors=[exc.reason]), heldout
        except MetricsError as exc:
            errors = [_scrub(_show(p)) for p in exc.problems[:3]]
            return GateStatus("error", source, errors=errors), heldout
        held = heldout | frozenset(_canon(v) for v in gates.heldout_splits)
        gates = gates.model_copy(update={"heldout_splits": sorted(held)})
    elif status is not None:
        source = "EvalKit default gates: every model passes its thresholds"
        gates, held = DEFAULT_GATES.model_copy(update={"heldout_splits": sorted(heldout)}), heldout
    else:
        return GateStatus("not gated", "no gates file in the snapshot"), heldout
    from evalkit.gates import RunStatus

    report = evaluate(metrics, gates, status if status is not None else RunStatus())
    return (
        GateStatus(
            status=report.verdict,
            source=source,
            exit_code=report.exit_code,
            failed=sum(1 for r in report.rows if r.result == "fail"),
            warned=sum(1 for r in report.rows if r.result == "warn"),
            rows=_gate_rows(report),
            errors=[_clean(e) for e in report.errors],
        ),
        held,
    )


def _canon(v: object) -> str:
    from evalkit.gates import canonical

    return canonical(v)


FALLBACK = {  # a pre-0.3 EvalKit summary: (index name, summary metric, scale)
    "cost_per_correct": ("cost_per_correct_usd", 1.0),
    "cost_per_case": ("cost_per_100_usd", 0.01),
    "latency_p50": ("latency_p50_ms", 1.0),
    "latency_p95": ("latency_p95_ms", 1.0),
}


def _models(
    metrics: MetricsSummary, cfg: ProjectConfig, kind: str, heldout: frozenset[str]
) -> list[ModelRow]:
    narrow = cfg.efficiency_labels
    allowed = EFFICIENCY_KEYS | set(narrow)

    def eff_row(m: Metric) -> bool:
        return (
            m.name in EFFICIENCY_NAMES
            and "model" in m.labels
            and set(m.labels) <= allowed
            and all(k in m.labels and _same(m.labels[k], v) for k, v in narrow.items())
            and not (
                _is_heldout(m.labels, heldout) and not set(m.labels) <= HELDOUT_KEPT | {"mode"}
            )
        )

    eff = [m for m in metrics.metrics if eff_row(m)]
    q = cfg.quality
    qkeys = None if q is None else {*q.labels, "model"}
    # a quality selector that names a model designates that model's quality only: the other
    # models' cells say "not designated", never a row the project did not name
    named = q.labels.get("model") if q is not None else None
    quality_rows = [
        m
        for m in metrics.metrics
        if q is not None
        and m.name == q.metric
        and set(m.labels) == qkeys
        and (named is None or _same(m.labels["model"], named))
    ]
    order: list[str] = []
    for m in [*eff, *quality_rows]:
        name = m.labels["model"]
        if isinstance(name, str) and name not in order:
            order.append(name)
    if kind == "evalkit-summary":
        for m in metrics.metrics:
            if m.name == "cost_per_correct_usd" or m.name == "cost_per_100_usd":
                name = m.labels.get("model")
                if isinstance(name, str) and name not in order and set(m.labels) == {"model"}:
                    order.append(name)
    rows: list[ModelRow] = []
    for model in order:
        mine = [m for m in eff if m.labels["model"] == model]
        figures: dict[str, Figure] = {}
        for name in EFFICIENCY_NAMES:
            found = [m for m in mine if m.name == name]
            if len(found) == 1:
                figures[name] = _figure(found[0])
            elif found:
                figures[name] = _problem(name, f"ambiguous ({len(found)} rows)")
        source = "efficiency block" if figures else "none"
        # A summary written before 0.3 has no efficiency block (its model-level error rate is
        # still converted): its point cost and latency figures stand in, labelled as such.
        if kind == "evalkit-summary" and "cost_per_case" not in figures:
            for name, (old, scale) in FALLBACK.items():
                if name in figures:
                    continue
                found = [
                    m for m in metrics.metrics if m.name == old and m.labels == {"model": model}
                ]
                if len(found) == 1:
                    figures[name] = _figure(
                        found[0], note="from summary (no interval)", scale=scale
                    )
                    figures[name].metric = name
                    figures[name].unit = "USD" if name.startswith("cost") else "ms"
            if any(f.note == "from summary (no interval)" for f in figures.values()):
                source = "from summary (no interval)"
        first = next((m for m in mine if len([x for x in mine if x.name == m.name]) == 1), None)
        mode = first.labels.get("mode") if first is not None else None
        price = first.labels.get("price_date") if first is not None else None
        quality = None
        if q is not None and (named is None or _same(model, named)):
            quality = _select(metrics, q, heldout, {"model": model})
        rows.append(
            ModelRow(
                model=_show(model),
                quality=quality,
                efficiency=figures,
                mode=None if mode is None else _show(mode),
                price_date=None if price is None else _show(price),
                source=source,
            )
        )
    return rows


def _rank(rows: list[ModelRow]) -> tuple[list[ModelRow], str]:
    """Order by cost per correct (unknown last, then first-seen order); mark the lowest and say
    whether its interval lies below every other model's."""

    def cpc(r: ModelRow) -> Figure | None:
        f = r.efficiency.get("cost_per_correct")
        return (
            f if f is not None and f.problem is None and isinstance(f.value, int | float) else None
        )

    known = [(i, r) for i, r in enumerate(rows) if cpc(r) is not None]
    known.sort(key=lambda t: (float(cpc(t[1]).value), t[0]))  # type: ignore[union-attr,arg-type]
    rest = [r for r in rows if cpc(r) is None]
    ordered = [r for _, r in known] + rest
    for i, (_, r) in enumerate(known, start=1):
        r.rank = i
    if not known:
        return ordered, "no model has a cost per correct" if rows else ""
    best = known[0][1]
    best.lowest = True
    bf = cpc(best)
    assert bf is not None
    if len(known) == 1:
        return ordered, f"lowest cost per correct: {best.model} (the only model with one)"
    tied = [r for _, r in known if cpc(r).value == bf.value]  # type: ignore[union-attr]
    if len(tied) > 1:
        for r in tied:
            r.lowest = True
        names = ", ".join(r.model for r in tied)
        return ordered, f"lowest cost per correct: tied between {names}"
    others = [cpc(r) for _, r in known[1:]]
    if bf.ci_high is None or any(o is None or o.ci_low is None for o in others):
        verdict = "no interval to compare"
    elif all(o is not None and o.ci_low is not None and bf.ci_high < o.ci_low for o in others):
        verdict = "clear: its interval is below every other model's"
    else:
        verdict = "intervals overlap"
    return ordered, f"lowest cost per correct: {best.model} ({verdict})"


def _improvements(folder: Path, snap: Mapping[str, Any], entry: ProjectEntry) -> None:
    if "experiments" not in snap["files"]:
        return
    url = _url(entry.repo, entry.commit, snap["files"]["experiments"]["path"])
    try:
        text = _text(_read_role(folder, snap, "experiments"), "experiments")
    except _Missing as exc:
        entry.problems.append(exc.reason)
        return
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = loads_json(line)
        except MetricsError:
            entry.problems.append(f"experiments line {i} is not valid JSON")
            continue
        if not isinstance(row, dict) or row.get("kind") != "experiment_closed":
            continue
        p = row.get("primary_row")
        if not isinstance(p, dict) or not isinstance(p.get("metric"), str):
            entry.problems.append(f"experiments line {i}: no primary row")
            continue
        unit = str(p["unit"]) if isinstance(p.get("unit"), str) else "ratio"

        def fmt(v: Any, unit: str = unit) -> str:
            if isinstance(v, bool) or not isinstance(v, int | float):
                return "—"
            return _fmt(unit, v)

        diff = p.get("diff")
        change = fmt(diff)
        if isinstance(p.get("ci_low"), int | float) and isinstance(p.get("ci_high"), int | float):
            change += f" [{fmt(p['ci_low'])}, {fmt(p['ci_high'])}]"
        verdict = row.get("verdict")
        if isinstance(verdict, str):
            change += f", {_show(verdict)}"
        labels: dict[str, Any] = p["labels"] if isinstance(p.get("labels"), dict) else {}
        shown = ", ".join(f"{_show(k)}={_show(v)}" for k, v in sorted(labels.items()))
        entry.improvements.append(
            {
                "metric": _show(p["metric"]) + (f" ({shown})" if shown else ""),
                "before": fmt(p.get("old")),
                "after": fmt(p.get("new")),
                "change": change,
                "url": url,
            }
        )


def _read(
    folder: Path, row: RegistryRow, cfg: ProjectConfig, base_heldout: frozenset[str]
) -> ProjectEntry:
    summary_path = cfg.summary or row.summary
    entry = ProjectEntry(row.project, row.repo, "missing", summary_path=summary_path)
    entry.evalkit_pin = entry.registry_pin = _pin(row.pin)
    snap = _load_snapshot(folder, row, summary_path)
    entry.commit = snap["commit"]
    entry.fetched_at = _show(snap.get("fetched_at")) if snap.get("fetched_at") else None
    _pins(entry, snap, row)
    if "summary" not in snap["files"]:
        raise _Missing("no summary file in the snapshot")
    entry.summary_path = snap["files"]["summary"]["path"]
    entry.url = _url(row.repo, entry.commit, entry.summary_path)
    text = _text(_read_role(folder, snap, "summary"), "summary")
    ptr = snap.get("pointer")
    if (
        isinstance(ptr, dict)
        and isinstance(ptr.get("path"), str)
        and not _path_problem(ptr["path"])
    ):
        entry.pointer = {
            "path": _show(ptr["path"]),
            "target": _show(ptr.get("target")),
            "url": _url(row.repo, entry.commit, ptr["path"]),
        }
    lessons = snap.get("lessons")
    if isinstance(lessons, str) and not _path_problem(lessons):
        entry.lessons_url = _url(row.repo, entry.commit, lessons)
    if cfg.own_format is not None:
        _read_own(text, entry, cfg, cfg.own_format, base_heldout)
        entry.status = "ok"
        return entry
    metrics, summary, kind, stated = _parse_summary(text)
    entry.kind = kind
    entry.run_id = _show(metrics.run_id)
    entry.run_at = _show(metrics.created_at)
    # only what the file states: metrics-1's default `false` and an EvalKit summary (which has
    # no such field) say nothing about simulation
    entry.simulated = stated
    if metrics.provenance.evalkit:
        entry.evalkit_version = _show(metrics.provenance.evalkit)
        entry.evalkit_version_source = "recorded in the run"
    status = None
    if summary is not None:
        from evalkit.pipeline.aggregate import stamp_lines

        entry.stamps = [_clean(s) for s in stamp_lines(summary)]
        status = _run_status(folder, snap, summary, entry)
        if status.stopped_at_cost_cap:
            entry.stamps.append("stopped at the cost cap")
    if entry.simulated:
        entry.stamps.insert(0, "SIMULATED: results from a simulator, not production")
    entry.gate, heldout = _gate(folder, snap, metrics, status, base_heldout)
    if cfg.headline is not None:
        entry.headline = _select(metrics, cfg.headline, heldout)
        entry.headline_label = _named(cfg.headline.label, cfg.headline.metric)
    if cfg.comparison is not None:
        entry.headline_comparison = _select(metrics, cfg.comparison, heldout)
        entry.comparison_label = _named(cfg.comparison.label, cfg.comparison.metric)
    _direction_once(entry)
    entry.calibration = _calibration(metrics, summary, cfg, heldout)
    models = _models(metrics, cfg, kind, heldout)
    entry.models, entry.comparison = _rank(models)
    if not models:
        entry.efficiency_note = "no per-model efficiency in this summary (no model label)"
    _improvements(folder, snap, entry)
    entry.status = "ok"
    return entry


def _direction_once(entry: ProjectEntry) -> None:
    """A direction the label already states (`... lower is better)`) is not repeated."""
    for fig, label in (
        (entry.headline, entry.headline_label),
        (entry.headline_comparison, entry.comparison_label),
    ):
        if fig is not None and fig.direction and label and fig.direction in label.lower():
            fig.direction_in_label = True


def _norm_req(req: str) -> str:
    """A requirement as compared: lower case, no spaces, `_` and `-` alike in the name."""
    text = re.sub(r"\s+", "", req).lower()
    return re.sub(r"^evalkit[-_.]harness", "evalkit-harness", text)


def _pins(entry: ProjectEntry, snap: Mapping[str, Any], row: RegistryRow) -> None:
    """The EvalKit pin as the project's own pyproject.toml states it at the commit (and the
    version its uv.lock resolved), with a registry cell that disagrees flagged; the registry
    cell only when the pyproject states no EvalKit requirement. EvalKit's own row shows its
    repository's version at the commit, labelled as such (it is not the build that made the
    run)."""
    declared = snap.get("evalkit_declared")
    locked = snap.get("evalkit_locked")
    lock = f" (uv.lock: {_show(locked)})" if isinstance(locked, str) and locked else ""
    if row.project == SELF:
        version = snap.get("evalkit_version")
        if isinstance(version, str) and version:
            entry.evalkit_pin = f"evalkit-harness {_show(version)} (the repository at the commit)"
            entry.evalkit_pin_source = "pyproject.toml at the commit"
        return
    reqs = [r for r in declared if isinstance(r, str)] if isinstance(declared, list) else []
    if reqs:
        entry.evalkit_pin = _clean(re.sub(r"://[^/@\s]+@", "://<hidden>@", "; ".join(reqs)))
        entry.evalkit_pin_source = f"pyproject.toml at the commit{lock}"
        reg = row.pin.strip()
        if reg not in ("", "-") and _norm_req(reg) not in {_norm_req(r) for r in reqs}:
            entry.pin_note = f"the registry's pin ({entry.registry_pin}) differs"
    elif isinstance(declared, list):  # the snapshot looked and found none
        entry.evalkit_pin_source = "the registry (no EvalKit requirement in pyproject.toml)"


def _timestamp_or_none(v: str | None) -> str | None:
    if v is None:
        return None
    try:
        parsed = datetime.fromisoformat(v)
    except ValueError:
        return None
    return v if parsed.tzinfo is not None else None


def _read_own(
    text: str, entry: ProjectEntry, cfg: ProjectConfig, own: OwnFormat, heldout: frozenset[str]
) -> None:
    """A project's own summary format, read only through the config's path selectors: no
    metrics-1 rows, so no gates, calibration or per-model efficiency are read from it."""
    data = _loads_summary(text)
    if not isinstance(data, dict):
        raise _Missing("the project's own summary is not a JSON object")
    entry.kind = "own-format"
    run_id = _path_scalar(data, own.run_id)
    entry.run_id = _show(run_id) if run_id else None
    if own.run_at is not None:
        run_at = _timestamp_or_none(_path_scalar(data, own.run_at))
        if run_at is None:
            entry.problems.append("run time not read: the path selects no ISO 8601 time")
        entry.run_at = _show(run_at) if run_at else None
    entry.simulated = _own_simulated(data, own.simulated)
    if entry.simulated:
        entry.stamps.insert(0, "SIMULATED: results from a simulator, not production")
    entry.gate = GateStatus("not gated", "the project's own summary format: no gates file read")
    if cfg.headline is not None:
        entry.headline = _select_path(data, cfg.headline, heldout)
        entry.headline_label = _named(cfg.headline.label, entry.headline.metric)
    if cfg.comparison is not None:
        entry.headline_comparison = _select_path(data, cfg.comparison, heldout)
        entry.comparison_label = _named(cfg.comparison.label, entry.headline_comparison.metric)
    _direction_once(entry)
    entry.calibration = {
        "status": "not stated",
        "text": "not stated in the project's own summary format",
        "judges": [],
    }
    entry.efficiency_note = "no per-model efficiency in this summary (the project's own format)"


def read_project(
    folder: str | Path,
    row: RegistryRow,
    config: ProjectConfig | None = None,
    heldout_splits: Sequence[object] = (),
) -> ProjectEntry:
    """One project's entry from its snapshot folder. Never raises: a project that cannot be
    read is returned as missing with the reason (IDX-02)."""
    cfg = config or ProjectConfig()
    heldout = frozenset({"heldout", *(_canon(v) for v in heldout_splits)})
    try:
        entry = _read(Path(folder), row, cfg, heldout)
    except _Missing as exc:
        entry = _missing(row, cfg, exc.reason)
    except Exception as exc:  # an unreadable file never crashes the page
        entry = _missing(row, cfg, f"unexpected {type(exc).__name__}")
    if entry.headline is None and cfg.headline_pending:
        entry.headline_pending = _clean(cfg.headline_pending.strip())
    entry.story = _story(entry, cfg)
    return entry


def _not_designated(e: ProjectEntry) -> str:
    """ "not designated", followed by the config's headline_pending words when it gives them."""
    return f"not designated {e.headline_pending}" if e.headline_pending else "not designated"


def _missing(row: RegistryRow, cfg: ProjectConfig, reason: str) -> ProjectEntry:
    return ProjectEntry(
        project=row.project,
        repo=row.repo,
        status="missing",
        reason=_clean(reason),
        summary_path=cfg.summary or row.summary,
        evalkit_pin=_pin(row.pin),
        registry_pin=_pin(row.pin),
    )


# ---- the portfolio's story (inputs.md) ----------------------------------------------------------


def _labels_text(labels: Mapping[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items())


def _row_name(f: Figure) -> str:
    """A fanned-out row's name: its `per` label's value."""
    return ", ".join(f.labels.values())


def _gate_target(r: Mapping[str, Any]) -> str:
    t = r["threshold"]
    t_text = ("true" if t else "false") if isinstance(t, bool) else str(t)
    bound = "" if r["bound"] == "point" else f" ({r['bound']} bound)"
    return f"{r['op']} {t_text}{bound}"


def _observed(r: Mapping[str, Any]) -> str:
    v = r["observed"]
    if v is None:
        return "—"
    return _fmt(r["unit"] or "ratio", v)


def _kind(metric: str, unit: str | None, op: str, cfg: ProjectConfig, headline: str | None) -> str:
    if metric in cfg.kinds:
        return cfg.kinds[metric]
    if metric == headline:
        return "north-star"
    if (unit and unit_kind(unit) == "currency") or metric.startswith("cost") or "cost" in metric:
        return "cost"
    if unit == "count" and op in ("<=", "=="):
        return "guardrail"
    return "quality"


def _story(e: ProjectEntry, cfg: ProjectConfig) -> dict[str, Any]:
    if e.status == "missing":
        return {
            "headline": {"value": "missing", "label": e.project, "note": e.reason},
            "evals": [],
            "improvements": [],
            "learnings": [],
            "monitoring": [],
        }
    short = (e.commit or "")[:7]
    version = e.evalkit_version or "not recorded"
    if e.evalkit_pin:
        version += f" (pin {e.evalkit_pin})"
    if e.pin_note:
        version += f" ({e.pin_note})"
    h = e.headline
    label = e.headline_label or "headline"
    run = f"run {e.run_id}; " if e.run_id else ""
    if h is None:
        headline = {
            "value": _not_designated(e),
            "label": label,
            "note": f"{run}commit {short}",
        }
    elif h.problem:
        headline = {"value": "not shown", "label": label, "note": h.problem}
    else:
        ci = (
            f"interval {_fmt(h.unit or 'ratio', h.ci_low)} to {_fmt(h.unit or 'ratio', h.ci_high)}"
            f" ({h.ci_method}); "
            if h.ci_low is not None and h.ci_high is not None
            else ""
        )
        if h.rows and h.rows[0].ci_method:
            ci = f"intervals: {h.rows[0].ci_method}; "
        direction = f"{_direction_shown(h)}; " if _direction_shown(h) else ""
        simulated = "simulated; " if e.simulated else ""
        headline = {
            "value": h.shown,
            "label": label,
            "note": f"{ci}{direction}{simulated}{run}commit {short}; EvalKit {version}",
        }
    gate_pending = e.gate is not None and e.gate.status in (
        "judging pending",
        "stopped at the cost cap",
    )
    if e.kind == "evalkit-summary":
        trusted = e.calibration is not None and e.calibration.get("status") == "calibrated"
    else:  # metrics-1 states simulated (false by default); an own format must state false
        trusted = e.simulated is False
    evals: list[dict[str, Any]] = []
    shown = [(label, h), (e.comparison_label or "comparison", e.headline_comparison)]
    for name, f in shown:
        if f is None or f.problem:
            continue
        first = f.rows[0] if f.rows else f
        evals.append(
            {
                "name": name,
                "data": f"n={first.n}" if first.n is not None else "n not stated",
                "method": first.ci_method
                or ("exact count" if first.k is not None else "point value"),
                "result": f.shown,
                "status": "pending" if gate_pending else ("final" if trusted else "provisional"),
                "url": e.url,
            }
        )
    monitoring: list[dict[str, Any]] = []
    for r in e.gate.rows if e.gate is not None else []:
        res = r["result"]
        status = {"pass": "pass", "fail": "fail", "warn": "provisional"}.get(res, "pending")
        if gate_pending:
            status = "pending"
        elif status == "pass" and (e.simulated or (e.kind == "metrics-1" and e.simulated is None)):
            status = "provisional"
        name = r["metric"] + (f" ({_labels_text(r['labels'])})" if r["labels"] else "")
        data = f"{r['heldout_rows']} held-out row(s), split level" if r["heldout_rows"] else ""
        evals.append(
            {
                "name": name,
                "data": data or "gated row",
                "method": f"evalkit gate {_gate_target(r)}",
                "result": _observed(r),
                "status": status,
                "url": e.url,
            }
        )
        if res != "error":
            monitoring.append(
                {
                    "metric": name,
                    "kind": _kind(r["metric"], r["unit"], r["op"], cfg, h.metric if h else None),
                    "target": _gate_target(r),
                    "current": _observed(r),
                    "how": f"evalkit gate ({e.gate.source})" if e.gate else "",
                }
            )
    learnings = (
        [
            {
                "lesson": f"{e.project} lessons log",
                "now": f"read the log at commit {short}",
                "url": e.lessons_url,
            }
        ]
        if e.lessons_url
        else []
    )
    return {
        "headline": headline,
        "evals": evals,
        "improvements": list(e.improvements),
        "learnings": learnings,
        "monitoring": monitoring,
    }


# ---- the index --------------------------------------------------------------------------------


@dataclass
class Index:
    projects: list[ProjectEntry]

    @property
    def missing(self) -> list[ProjectEntry]:
        return [p for p in self.projects if p.status == "missing"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INDEX_SCHEMA,
            "n_projects": len(self.projects),
            "n_missing": len(self.missing),
            "projects": [p.as_dict() for p in self.projects],
        }


def build_index(
    registry: str | Path | Sequence[RegistryRow],
    snapshots: str | Path,
    config: IndexConfig | str | Path | None = None,
) -> Index:
    """Every registered project, then EvalKit, each read from `snapshots/<project>/` (local
    files only). Raises IndexInputError only for a bad registry or config; a project's own
    files never raise (IDX-02)."""
    rows = list(registry) if not isinstance(registry, str | Path) else read_registry(registry)
    cfg = config if isinstance(config, IndexConfig) else load_config(config)
    unknown = sorted(set(cfg.projects) - {r.project for r in rows} - {SELF})
    if unknown:
        raise IndexInputError([f"index config names unregistered project(s): {', '.join(unknown)}"])
    base = Path(snapshots)
    return Index(
        [
            read_project(base / r.project, r, cfg.for_project(r.project), cfg.heldout_splits)
            for r in [*rows, SELF_ROW]
        ]
    )


def index_json(index: Index) -> str:
    return json.dumps(index.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---- HTML -------------------------------------------------------------------------------------

EXTRA_CSS = """
.missing{color:var(--fail);font-weight:600}.sim{color:var(--warn-fg);background:var(--warn-bg);
border-color:var(--warn-fg)}.lowest{font-weight:700}
td.num{white-space:nowrap}h2 .badge{margin-left:8px;vertical-align:middle}
"""


def _e(v: object) -> str:
    return escape("—" if v is None else str(v), quote=True)


def _link(url: str | None, text: str) -> str:
    return f'<a href="{_e(url)}">{_e(text)}</a>' if url else _e(text)


def _direction_shown(f: Figure) -> str | None:
    return None if f.direction_in_label else f.direction


def _fig_html(f: Figure | None) -> str:
    if f is None:
        return "—"
    if f.problem:
        return f"<span class=muted>{_e(f.problem)}</span>"
    if f.rows:
        parts = "; ".join(f"{_e(_row_name(r))}: {_fig_html(r)}" for r in f.rows)
        notes = (f.note, _direction_shown(f))
        return parts + "".join(f" <span class=muted>({_e(x)})</span>" for x in notes if x)
    tip = f' title="{_e(f.ci_method)}"' if f.ci_method else ""
    html = f"<span{tip}>{_e(f.text)}</span>"
    if f.companion is not None:
        html += f" ({_e(f.companion_label or f.companion.metric)} {_fig_html(f.companion)})"
    return html + "".join(
        f" <span class=muted>({_e(x)})</span>" for x in (f.note, _direction_shown(f)) if x
    )


def _gate_html(g: GateStatus | None) -> str:
    if g is None:
        return "—"
    cls = {"pass": "pass", "fail": "fail", "error": "fail"}.get(g.status, "muted")
    counts = f" ({g.failed} failed, {g.warned} warned)" if g.exit_code is not None else ""
    return f'<span class="badge {cls}">{_e(g.status)}</span>{_e(counts)}'


def _version_html(p: ProjectEntry) -> str:
    pin = f"; pin {_e(p.evalkit_pin)}, from {_e(p.evalkit_pin_source)}" if p.evalkit_pin else ""
    if p.pin_note:
        pin += f'; <span class="badge fail">{_e(p.pin_note)}</span>'

    if p.evalkit_version:
        return (
            f"{_e(p.evalkit_version)} <span class=muted>({_e(p.evalkit_version_source)}{pin})"
            "</span>"
        )
    return f"<span class=muted>not recorded in the run{pin}</span>"


def _overview(index: Index) -> str:
    head = (
        "<table id=overview><caption>Every registered project and EvalKit, read at a pinned "
        "commit</caption><thead><tr><th scope=col>Project</th><th scope=col>Headline</th>"
        "<th scope=col>Calibration</th><th scope=col>Gates</th><th scope=col>EvalKit</th>"
        "<th scope=col>Run date</th><th scope=col>Commit</th></tr></thead><tbody>"
    )
    body = []
    for p in index.projects:
        name = f'<a href="#p-{_e(p.project)}">{_e(p.project)}</a>'
        if p.status == "missing":
            body.append(
                f"<tr><th scope=row>{name}</th><td colspan=6><span class=missing>missing</span>: "
                f"{_e(p.reason)}</td></tr>"
            )
            continue
        sim = ' <span class="badge sim">SIMULATED</span>' if p.simulated else ""
        if p.headline is None:
            headline = f"<span class=muted>{_e(_not_designated(p))}</span>"
        else:
            headline = f"{_e(p.headline_label)}: {_fig_html(p.headline)}"
        if p.headline_comparison is not None:
            headline += (
                f"<br><span class=muted>vs {_e(p.comparison_label)}:</span> "
                f"{_fig_html(p.headline_comparison)}"
            )
        cal = _e(p.calibration["status"]) if p.calibration else "—"
        commit = _link(p.url, (p.commit or "")[:7])
        body.append(
            f"<tr><th scope=row>{name}{sim}</th><td>{headline}</td><td>{cal}</td>"
            f"<td>{_gate_html(p.gate)}</td><td>{_version_html(p)}</td>"
            f"<td class=num>{_e(p.run_date)}</td><td>{commit}</td></tr>"
        )
    return head + "".join(body) + "</tbody></table>"


EFF_COLUMNS = (
    ("cost_per_correct", "Cost per correct"),
    ("cost_per_case", "Cost per case"),
    ("tokens_in_per_case", "Tokens in"),
    ("tokens_out_per_case", "Tokens out"),
    ("tokens_reasoning_per_case", "Reasoning"),
    ("tokens_cached_per_case", "Cached"),
    ("cache_hit_share", "Cache hits"),
    ("latency_p50", "p50"),
    ("latency_p95", "p95"),
    ("latency_p99", "p99"),
    ("error_rate", "Errors"),
    ("retry_rate", "Retries"),
    ("system_cost", "System cost"),
    ("eval_overhead_cost", "Eval overhead"),
)


def _models_html(p: ProjectEntry) -> str:
    if not p.models:
        return f"<p class=muted>{_e(p.efficiency_note)}</p>"
    cols = [(k, t) for k, t in EFF_COLUMNS if any(k in m.efficiency for m in p.models)]
    head = (
        "<table><caption>Quality and efficiency per model, ordered by cost per correct (the "
        "comparison metric)</caption><thead><tr><th scope=col>Model</th><th scope=col>Quality</th>"
        + "".join(f"<th scope=col>{_e(t)}</th>" for _, t in cols)
        + "<th scope=col>Mode</th><th scope=col>Price date</th><th scope=col>Source</th>"
        "</tr></thead><tbody>"
    )
    body = []
    for m in p.models:
        cls = " class=lowest" if m.lowest else ""
        mark = ' <span class="badge pass">lowest cost per correct</span>' if m.lowest else ""
        cells = "".join(f"<td class=num>{_fig_html(m.efficiency.get(k))}</td>" for k, _ in cols)
        quality = _fig_html(m.quality) if m.quality else "<span class=muted>not designated</span>"
        body.append(
            f"<tr{cls}><th scope=row>{_e(m.model)}{mark}</th><td>{quality}</td>"
            f"{cells}<td>{_e(m.mode or 'not recorded')}</td>"
            f"<td>{_e(m.price_date or 'not recorded')}</td><td>{_e(m.source)}</td></tr>"
        )
    note = f"<p>{_e(p.comparison)}</p>" if p.comparison else ""
    return head + "".join(body) + "</tbody></table>" + note


def _gate_table(g: GateStatus) -> str:
    if not g.rows and not g.errors:
        return f"<p class=muted>{_e(g.source)}</p>"
    rows = "".join(
        f"<tr><td>{_e(r['metric'])}</td><td>{_e(_labels_text(r['labels']) or '(any)')}</td>"
        f"<td>{_e(_gate_target(r))}</td><td class=num>{_e(_observed(r))}</td>"
        f'<td>{_e(r["severity"])}</td><td class="{_e(r["result"])}">{_e(r["result"])}</td></tr>'
        for r in g.rows
    )
    errors = "".join(f"<li>{_e(x)}</li>" for x in g.errors)
    return (
        f"<p>{_gate_html(g)} · {_e(g.source)}</p>"
        + (
            "<table><thead><tr><th scope=col>Metric</th><th scope=col>Labels</th>"
            "<th scope=col>Target</th><th scope=col>Observed</th><th scope=col>Severity</th>"
            f"<th scope=col>Result</th></tr></thead><tbody>{rows}</tbody></table>"
            if g.rows
            else ""
        )
        + (f"<ul>{errors}</ul>" if errors else "")
    )


def _project_html(p: ProjectEntry) -> str:
    anchor = f'<h2 id="p-{_e(p.project)}">{_e(p.project)}'
    if p.status == "missing":
        return (
            f'{anchor} <span class="badge fail">missing</span></h2><div class=card>'
            f"<p>{_e(p.reason)}</p><p class=meta>Repository {_e(p.repo)} · summary "
            f"{_e(p.summary_path or 'not registered')}</p></div>"
        )
    sim = ' <span class="badge sim">SIMULATED</span>' if p.simulated else ""
    via = (
        f" (named by {_link(p.pointer.get('url'), str(p.pointer.get('path')))})"
        if p.pointer
        else ""
    )
    meta = (
        f"<p class=meta>Run {_e(p.run_id)} · {_e(p.run_at)} · {_e(p.kind)} · "
        f"{_link(p.url, p.summary_path or '')}{via} at {_e((p.commit or '')[:7])} · EvalKit "
        f"{_version_html(p)}</p>"
    )
    stamps = (
        "<ul class=stamps>" + "".join(f"<li>{_e(s)}</li>" for s in p.stamps) + "</ul>"
        if p.stamps
        else ""
    )
    headline = (
        (
            f"<p class=muted>Headline {_e(_not_designated(p))}.</p>"
            if p.headline_pending
            else "<p class=muted>Headline not designated in the index config.</p>"
        )
        if p.headline is None
        else f"<p><strong>{_e(p.headline_label)}</strong>: {_fig_html(p.headline)}</p>"
    )
    if p.headline_comparison is not None:
        headline += (
            f"<p>Comparison, <strong>{_e(p.comparison_label)}</strong>: "
            f"{_fig_html(p.headline_comparison)}</p>"
        )
    cal = f"<p>Calibration: {_e(p.calibration['text'])}</p>" if p.calibration else ""
    problems = (
        "<ul>" + "".join(f"<li class=muted>{_e(x)}</li>" for x in p.problems) + "</ul>"
        if p.problems
        else ""
    )
    improvements = ""
    if p.improvements:
        improvements = (
            "<h3>Improvements (experiment ledger)</h3><ul>"
            + "".join(
                f"<li>{_link(i['url'], i['metric'])}: {_e(i['before'])} → {_e(i['after'])}, "
                f"change {_e(i['change'])}</li>"
                for i in p.improvements
            )
            + "</ul>"
        )
    lessons = f"<p>{_link(p.lessons_url, 'Lessons log')}</p>" if p.lessons_url else ""
    return (
        f"{anchor}{sim}</h2>{meta}{stamps}<div class=card>{headline}{cal}</div>"
        f"<div class=card><h3>Gates</h3>{_gate_table(p.gate) if p.gate else ''}</div>"
        f"<div class=card><h3>Quality and efficiency per model</h3>{_models_html(p)}</div>"
        f"{improvements}{lessons}{problems}"
    )


def render_html(index: Index) -> str:
    """The index page: self-contained, loads nothing, every value escaped, the same bytes for
    the same inputs."""
    from evalkit.report.html import CSS

    n, miss = len(index.projects), len(index.missing)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>EvalKit eval index</title><style>{CSS}{EXTRA_CSS}</style></head><body><main>"
        "<h1>EvalKit eval index</h1>"
        f"<p class=meta>{n} projects · {miss} missing · read from snapshots at pinned commits by "
        "<code>evalkit index build</code>; no model or network call. Held-out results appear "
        "only as split-level aggregates; simulated results are labelled.</p>"
        f"<div class=card>{_overview(index)}</div>"
        + "".join(_project_html(p) for p in index.projects)
        + "</main></body></html>\n"
    )


def write_index(index: Index, out: str | Path) -> list[Path]:
    """Write `index.html` and `index.json` into `out` atomically."""
    out = Path(out)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IndexInputError([f"cannot create {out}: {type(exc).__name__}"]) from None
    written = []
    for name, text in (("index.html", render_html(index)), ("index.json", index_json(index))):
        try:
            fd, tmp = tempfile.mkstemp(dir=out, prefix=f".{name}-")
            os.chmod(tmp, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, out / name)
        except OSError as exc:
            raise IndexInputError([f"cannot write {out / name}: {type(exc).__name__}"]) from None
        written.append(out / name)
    return written
