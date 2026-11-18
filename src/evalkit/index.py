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
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from evalkit.metrics import (
    EFFICIENCY_NAMES,
    NAME_RE,
    SCHEMA,
    Metric,
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


def _show(value: object) -> str:
    """A value from a project's file as the index may show it: at most 64 characters, key
    shapes hidden."""
    text = safe_text(value)
    return "<hidden>" if _SECRETS.search(text) else text


def _clean(text: str) -> str:
    """EvalKit's own message (a reason, a gate note, a stamp), which quotes file values only
    through `_show`/`safe_text`: key shapes hidden, at most 300 characters."""
    text = _SECRETS.sub("<hidden>", text)
    return text if len(text) <= 300 else text[:299] + "…"


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


class Selector(_Strict):
    """One metric row: `metric` and the row's labels exactly (`*` matches any value)."""

    metric: StrictStr
    labels: dict[str, LabelValue] = Field(default_factory=dict)
    label: StrictStr | None = None  # how the index names it (default: the metric name)

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("metric must match [a-z][a-z0-9_.]*")
        return v


class ProjectConfig(_Strict):
    summary: StrictStr | None = None  # overrides the registry's summary cell
    headline: Selector | None = None
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
    if summary_path is None:
        meta["reason"] = "no summary registered in the registry"
        return meta, {}
    paths = {"summary": summary_path, "gates": cfg.gates, "experiments": cfg.experiments}
    for role, p in [*paths.items(), ("lessons", cfg.lessons)]:
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

        def exists(p: str) -> bool:
            try:
                kind = _git(["cat-file", "-t", f"{commit}:{p}"], cwd=repo).decode().strip()
            except _GitError:
                return False
            return kind == "blob"

        def read(p: str) -> bytes:
            size = int(_git(["cat-file", "-s", f"{commit}:{p}"], cwd=repo).decode().strip())
            if size > MAX_FILE_BYTES:
                raise _GitError(f"larger than {MAX_FILE_BYTES:,} bytes")
            return _git(["cat-file", "blob", f"{commit}:{p}"], cwd=repo)

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
    meta["status"], meta["reason"] = "ok", ""
    return meta, blobs


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


def _fmt(unit: str, v: bool | int | float) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    kind = unit_kind(unit)
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
            "note": self.note,
            "problem": self.problem,
            "heldout": self.heldout,
        }


def _figure(m: Metric, *, heldout: bool = False, note: str = "", scale: float = 1.0) -> Figure:
    value: bool | int | float = m.value
    lo, hi = m.ci_low, m.ci_high
    if scale != 1.0 and not isinstance(value, bool):
        value = value * scale
        lo = None if lo is None else lo * scale
        hi = None if hi is None else hi * scale
    labels = {
        _show(k): _show(v) for k, v in sorted(m.labels.items()) if not heldout or k in HELDOUT_KEPT
    }
    text = _fmt(m.unit, value)
    kind = unit_kind(m.unit)
    if kind == "rate" and m.k is not None and m.n and m.k in (0, m.n):
        from evalkit.stats import rate_wording

        text = rate_wording(m.k, m.n)
    elif lo is not None and hi is not None:
        text += f" [{_fmt(m.unit, lo)}, {_fmt(m.unit, hi)}]"
    return Figure(
        metric=_show(m.name),
        labels=labels,
        unit=m.unit,
        value=value,
        ci_low=lo,
        ci_high=hi,
        ci_method=_show(m.ci_method) if m.ci_method is not None else None,
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


def _select(
    metrics: MetricsSummary,
    sel: Selector,
    heldout: frozenset[str],
    extra: Mapping[str, Any] | None = None,
) -> Figure:
    """The one row a selector names: its metric and exactly its labels."""
    want: dict[str, Any] = {**sel.labels, **(extra or {})}
    named = [m for m in metrics.metrics if m.name == sel.metric]
    if not named:
        return _problem(sel.metric, f"no metric named {_show(sel.metric)}", want)
    rows = [
        m
        for m in named
        if set(m.labels) == set(want) and all(_same(m.labels[k], v) for k, v in want.items())
    ]
    if not rows:
        return _problem(sel.metric, "matches no metric row", want)
    if len(rows) > 1:
        return _problem(sel.metric, f"matches {len(rows)} rows (name every label)", want)
    m = rows[0]
    held = _is_heldout(m.labels, heldout)
    if held and not set(m.labels) <= HELDOUT_KEPT:
        return _problem(sel.metric, "a held-out row below split level is not shown", want)
    return _figure(m, heldout=held, note="held-out split aggregate" if held else "")


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
                "labels": {_show(k): _show(v) for k, v in sorted(d["labels"].items())},
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
    headline: Figure | None = None
    headline_label: str | None = None
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
            "headline": None if self.headline is None else self.headline.as_dict(),
            "headline_label": self.headline_label,
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


def _parse_summary(text: str) -> tuple[MetricsSummary, Any, str]:
    """metrics-1, or an EvalKit summary.json (strict marker); else the reason it is neither."""
    from evalkit.runfolder import marker_problem
    from evalkit.schemas import RunSummary

    try:
        data = loads_json(text)
    except MetricsError as exc:
        raise _Missing(f"summary is not valid JSON: {exc.problems[0]}") from None
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version == SCHEMA:
        try:
            return validate_metrics(data), None, "metrics-1"
        except MetricsError as exc:
            raise _Missing("invalid metrics-1: " + "; ".join(exc.problems[:3])) from None
    problem = marker_problem(data, "summary.json")
    if problem is not None:
        raise _Missing(f"not metrics-1 or an EvalKit summary.json: it {problem}")
    try:
        summary = RunSummary.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(describe_validation(exc)[:3])
        raise _Missing(f"invalid EvalKit summary: {problems}") from None
    try:
        return from_run_summary(summary), summary, "evalkit-summary"
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
            return GateStatus("error", source, errors=[_show(p) for p in exc.problems[:3]]), heldout
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
    quality_rows = [
        m
        for m in metrics.metrics
        if q is not None and m.name == q.metric and set(m.labels) == qkeys
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
        if q is not None:
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
    entry.evalkit_pin = _pin(row.pin)
    snap = _load_snapshot(folder, row, summary_path)
    entry.commit = snap["commit"]
    entry.fetched_at = _show(snap.get("fetched_at")) if snap.get("fetched_at") else None
    if isinstance(snap.get("evalkit_pin"), str):
        entry.evalkit_pin = _pin(snap["evalkit_pin"])
    if "summary" not in snap["files"]:
        raise _Missing("no summary file in the snapshot")
    entry.summary_path = snap["files"]["summary"]["path"]
    entry.url = _url(row.repo, entry.commit, entry.summary_path)
    metrics, summary, kind = _parse_summary(_text(_read_role(folder, snap, "summary"), "summary"))
    entry.kind = kind
    entry.run_id = _show(metrics.run_id)
    entry.run_at = _show(metrics.created_at)
    entry.simulated = bool(metrics.simulated)
    if metrics.provenance.evalkit:
        entry.evalkit_version = _show(metrics.provenance.evalkit)
        entry.evalkit_version_source = "recorded in the run"
    elif isinstance(snap.get("evalkit_version"), str):
        entry.evalkit_version = _show(snap["evalkit_version"])
        entry.evalkit_version_source = "pyproject.toml at the commit"
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
        entry.headline_label = _show(cfg.headline.label or cfg.headline.metric)
    entry.calibration = _calibration(metrics, summary, cfg, heldout)
    models = _models(metrics, cfg, kind, heldout)
    entry.models, entry.comparison = _rank(models)
    if not models:
        entry.efficiency_note = "no per-model efficiency in this summary (no model label)"
    _improvements(folder, snap, entry)
    lessons = snap.get("lessons")
    if isinstance(lessons, str) and not _path_problem(lessons):
        entry.lessons_url = _url(row.repo, entry.commit, lessons)
    entry.status = "ok"
    return entry


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
    entry.story = _story(entry, cfg)
    return entry


def _missing(row: RegistryRow, cfg: ProjectConfig, reason: str) -> ProjectEntry:
    return ProjectEntry(
        project=row.project,
        repo=row.repo,
        status="missing",
        reason=_clean(reason),
        summary_path=cfg.summary or row.summary,
        evalkit_pin=_pin(row.pin),
    )


# ---- the portfolio's story (inputs.md) ----------------------------------------------------------


def _labels_text(labels: Mapping[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items())


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
    h = e.headline
    label = e.headline_label or "headline"
    if h is None:
        headline = {"value": "not designated", "label": label, "note": f"run {e.run_id}"}
    elif h.problem:
        headline = {"value": "not shown", "label": label, "note": h.problem}
    else:
        ci = (
            f"interval {_fmt(h.unit or 'ratio', h.ci_low)} to {_fmt(h.unit or 'ratio', h.ci_high)}"
            f" ({h.ci_method}); "
            if h.ci_low is not None and h.ci_high is not None
            else ""
        )
        headline = {
            "value": h.text,
            "label": label,
            "note": f"{ci}run {e.run_id}; commit {short}; EvalKit {version}",
        }
    gate_pending = e.gate is not None and e.gate.status in (
        "judging pending",
        "stopped at the cost cap",
    )
    trusted = (
        e.calibration is not None and e.calibration.get("status") == "calibrated"
        if e.kind == "evalkit-summary"
        else not e.simulated
    )
    evals: list[dict[str, Any]] = []
    if h is not None and not h.problem:
        evals.append(
            {
                "name": label,
                "data": f"n={h.n}" if h.n is not None else "n not stated",
                "method": h.ci_method or ("exact count" if h.k is not None else "point value"),
                "result": h.text,
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
        elif status == "pass" and e.simulated:
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


def _fig_html(f: Figure | None) -> str:
    if f is None:
        return "—"
    if f.problem:
        return f"<span class=muted>{_e(f.problem)}</span>"
    tip = f' title="{_e(f.ci_method)}"' if f.ci_method else ""
    note = f" <span class=muted>({_e(f.note)})</span>" if f.note else ""
    return f"<span{tip}>{_e(f.text)}</span>{note}"


def _gate_html(g: GateStatus | None) -> str:
    if g is None:
        return "—"
    cls = {"pass": "pass", "fail": "fail", "error": "fail"}.get(g.status, "muted")
    counts = f" ({g.failed} failed, {g.warned} warned)" if g.exit_code is not None else ""
    return f'<span class="badge {cls}">{_e(g.status)}</span>{_e(counts)}'


def _version_html(p: ProjectEntry) -> str:
    if p.evalkit_version:
        return f"{_e(p.evalkit_version)} <span class=muted>({_e(p.evalkit_version_source)})</span>"
    pin = f"; pin {_e(p.evalkit_pin)}" if p.evalkit_pin else ""
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
            headline = "<span class=muted>not designated</span>"
        else:
            headline = f"{_e(p.headline_label)}: {_fig_html(p.headline)}"
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
        body.append(
            f"<tr{cls}><th scope=row>{_e(m.model)}{mark}</th><td>{_fig_html(m.quality)}</td>"
            f"{cells}<td>{_e(m.mode)}</td><td>{_e(m.price_date)}</td><td>{_e(m.source)}</td></tr>"
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
    meta = (
        f"<p class=meta>Run {_e(p.run_id)} · {_e(p.run_at)} · {_e(p.kind)} · "
        f"{_link(p.url, p.summary_path or '')} at {_e((p.commit or '')[:7])} · EvalKit "
        f"{_version_html(p)}</p>"
    )
    stamps = (
        "<ul class=stamps>" + "".join(f"<li>{_e(s)}</li>" for s in p.stamps) + "</ul>"
        if p.stamps
        else ""
    )
    headline = (
        "<p class=muted>Headline not designated in the index config.</p>"
        if p.headline is None
        else f"<p><strong>{_e(p.headline_label)}</strong>: {_fig_html(p.headline)}</p>"
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
