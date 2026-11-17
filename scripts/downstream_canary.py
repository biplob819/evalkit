#!/usr/bin/env python3
"""Run each downstream project's EvalKit-importing tests against this checkout (DSC-03, D-61).

The registry is docs/downstream.tsv. For each project with tests `auto`:
    uv sync --frozen <install>                   in the project checkout
    uv pip install --no-deps -e <this checkout>  so this EvalKit replaces the project's pin
    uv run --no-sync pytest -m "not live" <test files that import evalkit>
        (minus the registry's `deselect` ids: tests that check the installed pin itself, which
        fail by design when the canary replaces the pin)
with every key, token, secret and password removed from the environment.

Usage:
    python scripts/downstream_canary.py --matrix                       # CI matrix JSON
    python scripts/downstream_canary.py --run-one NAME --checkout DIR [--out FILE]
    python scripts/downstream_canary.py --all --workdir DIR [--out FILE]  # clone and run all
    python scripts/downstream_canary.py --self-test

Statuses: pass; known-drift (only the registry's `known` ids failed); fail; setup-error (sync,
install, a stale node id, or a checkout holding a .env file); nothing-ran (nothing collected or
everything skipped); no-tests (no test file imports evalkit). Registry `tests=all` runs the
whole suite, for projects that reach EvalKit only through their own code.

Exit 0 unless a project is `fail` or `setup-error` (then 1); 2 on bad input.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
import downstream_imports

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "downstream.tsv"
COLUMNS = (
    "project",
    "repo",
    "branch",
    "pin",
    "install",
    "tests",
    "summary",
    "deselect",
    "known",
    "note",
)
# The child environment is an allowlist: nothing else from this shell reaches downstream code.
ENV_ALLOW = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "CI",
    # network plumbing a sync needs behind a proxy, and the Python setup-uv selected
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "UV_PYTHON",
}  # not UV_INDEX_URL: an index URL can carry credentials
ENV_ALLOW_WINDOWS = {"SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR"}
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped)")
# pytest's final line, e.g. "== 1 failed, 41 passed, 2 skipped in 9.10s ==" or, under -q,
# "41 passed in 9.10s"; anchored so no other output line can be taken for it.
_SUMMARY_RE = re.compile(
    r"^=*\s*(?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)"
    r"(?:, )?)+ in [\d.]+s(?: \([\d:]+\))?\s*=*$"
)
_SHORT_RE = re.compile(r"^(FAILED|ERROR|PASSED) (.+?)(?: - .*)?$")
STDERR_MARK = "----- stderr -----"
_NODE_RE = re.compile(r"tests/[\w/.-]+\.py(?:::\w+)+")
TIMEOUT_S = 1800


@dataclass(frozen=True)
class Row:
    project: str
    repo: str
    branch: str
    pin: str
    install: str
    tests: str
    summary: str
    deselect: str  # comma-separated pytest node ids that check the installed pin itself
    known: str  # comma-separated node ids known to fail against EvalKit main (drift noted)
    note: str

    def deselected(self) -> list[str]:
        return _ids(self.deselect)

    def known_failures(self) -> list[str]:
        return _ids(self.known)


def _ids(cell: str) -> list[str]:
    return [] if cell in {"", "-"} else cell.split(",")


def load_registry(path: Path = REGISTRY) -> list[Row]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or tuple(lines[0].split("\t")) != COLUMNS:
        raise ValueError(f"{path}: header must be the columns {', '.join(COLUMNS)}")
    rows = []
    for i, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) != len(COLUMNS):
            raise ValueError(f"{path} line {i}: {len(cells)} cells, expected {len(COLUMNS)}")
        row = Row(*(c.strip() for c in cells))
        bad = [n for n in row.deselected() + row.known_failures() if not _NODE_RE.fullmatch(n)]
        if bad:
            raise ValueError(f"{path} line {i}: not a pytest node id: {', '.join(bad)}")
        rows.append(row)
    return rows


def _uv_cache_dir() -> str:
    if os.environ.get("UV_CACHE_DIR"):
        return os.environ["UV_CACHE_DIR"]
    p = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, check=False)
    return p.stdout.strip()


def child_env(home: Path) -> dict[str, str]:
    """Allowlisted environment: no keys, tokens, DSNs or profiles, and a private, empty HOME
    (one per project run) so dotfiles such as ~/.aws or ~/.netrc are out of reach and nothing
    carries from one project to the next. The uv cache is kept for speed."""
    allow = ENV_ALLOW | (ENV_ALLOW_WINDOWS if os.name == "nt" else set())
    env = {k: v for k, v in os.environ.items() if k in allow}
    env["HOME"] = env["USERPROFILE"] = str(home)
    cache = _uv_cache_dir()
    if cache:
        env["UV_CACHE_DIR"] = cache
    env["EVALKIT_DOWNSTREAM"] = "1"
    return env


def commands(row: Row, evalkit_root: Path, test_files: list[str]) -> list[list[str]]:
    """`test_files` empty means the project's whole suite (registry `tests=all`)."""
    pytest_cmd = ["uv", "run", "--no-sync", "pytest", "-q", "-rfEp", "-p", "no:cacheprovider"]
    return [
        ["uv", "sync", "--frozen", *row.install.split()],
        ["uv", "pip", "install", "--no-deps", "-e", str(evalkit_root)],
        [
            *pytest_cmd,
            "-m",
            "not live",
            *(f"--deselect={node}" for node in row.deselected()),
            *test_files,
        ],
    ]


def _stdout(text: str) -> str:
    return text.split(STDERR_MARK, 1)[0]


def parse_counts(text: str) -> dict[str, int]:
    """Counts from pytest's final summary line on stdout only, never from other output."""
    out = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    lines = [ln for ln in _stdout(text).splitlines() if _SUMMARY_RE.match(ln.strip())]
    for n, word in _COUNT_RE.findall(lines[-1] if lines else ""):
        out["errors" if word.startswith("error") else word] = int(n)
    return out


def _short(text: str, kinds: set[str]) -> list[str]:
    """Node ids (spaces allowed, e.g. `test_p[a b]`) from pytest's -r short summary."""
    found = set()
    for ln in _stdout(text).splitlines():
        m = _SHORT_RE.match(ln.strip())
        if m and m[1] in kinds:
            found.add(m[2])
    return sorted(found)


def parse_failures(text: str) -> list[str]:
    return _short(text, {"FAILED", "ERROR"})


def parse_passed(text: str) -> list[str]:
    return _short(text, {"PASSED"})


def stale_ids(checkout: Path, ids: list[str]) -> list[str]:
    """Node ids whose file or test function no longer exists (pytest would ignore them)."""
    stale = []
    for node in ids:
        path, *names = node.split("::")
        f = checkout / path
        if not f.is_file() or not names:
            stale.append(node)
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            stale.append(node)
            continue
        # Lenient on purpose: a name defined anywhere in the file (nested under `if`, or a
        # method a class inherits from one defined there) counts as present.
        defined = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        if any(name.split("[")[0] not in defined for name in names):
            stale.append(node)
    return stale


class Runner(Protocol):
    def __call__(self, cmd: list[str], cwd: Path) -> tuple[int, str]: ...


class RealRunner:
    """Runs commands with the allowlisted environment and this run's private HOME."""

    def __init__(self, home: Path):
        self.env = child_env(home)

    def __call__(self, cmd: list[str], cwd: Path) -> tuple[int, str]:
        try:
            p = subprocess.run(
                cmd,
                cwd=cwd,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {TIMEOUT_S}s"
        return p.returncode, f"{p.stdout}\n{STDERR_MARK}\n{p.stderr}"


class FakeRunner:
    """A runner for tests: records calls, answers pytest with a fixed summary line."""

    def __init__(self, pytest_line: str, pytest_rc: int = 0, fail_step: int | None = None):
        self.pytest_line, self.pytest_rc, self.fail_step = pytest_line, pytest_rc, fail_step
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: Path) -> tuple[int, str]:
        step = len(self.calls)
        self.calls.append(cmd)
        if step == self.fail_step:
            return 1, "boom"
        return (self.pytest_rc, self.pytest_line) if step == 2 else (0, "")


def _git_head(path: Path) -> str:
    p = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=False
    )
    return p.stdout.strip() if p.returncode == 0 else ""


STATUSES = ("pass", "known-drift", "fail", "setup-error", "nothing-ran", "no-tests")
FAILING = {"fail", "setup-error"}


def run_one(row: Row, checkout: Path, evalkit_root: Path, runner: Runner | None = None) -> dict:
    if runner is not None:
        return _run_one(row, checkout, evalkit_root, runner)
    with tempfile.TemporaryDirectory(prefix="evalkit-canary-home-") as home:
        return _run_one(row, checkout, evalkit_root, RealRunner(Path(home)))


def _run_one(row: Row, checkout: Path, evalkit_root: Path, runner: Runner) -> dict:
    report = downstream_imports.audit(checkout)
    files = [] if row.tests == "all" else report.test_files
    result: dict = {
        "repo": row.repo,
        "commit": _git_head(checkout),
        "pin": row.pin,
        "tests": row.tests,
        "files": files,
        "audit": report.counts(),
        "imports_not_public": sorted(
            (f"import {u.module}" if u.name == "*" else f"{u.module}.{u.name}")
            + (f" -> {u.public_at}" if u.public_at else "")
            + f" [{u.kind}]"
            for u in report.uses
            if u.kind != "public"
        ),
        "counts": parse_counts(""),
    }
    if (checkout / ".env").exists():
        detail = "the checkout holds a .env file; run the canary on a fresh clone"
        return {**result, "status": "setup-error", "detail": detail}
    stale = stale_ids(checkout, row.deselected() + row.known_failures())
    if stale:
        return {**result, "status": "setup-error", "detail": "stale node ids: " + ", ".join(stale)}
    if row.tests == "auto" and not files:
        return {**result, "status": "no-tests", "detail": "no test file imports evalkit"}
    for step, cmd in enumerate(commands(row, evalkit_root, files)):
        rc, out = runner(cmd, checkout)
        if step < 2 and rc != 0:
            tail = out.strip().splitlines()[-15:]
            return {
                **result,
                "status": "setup-error",
                "detail": " ".join(cmd[:3]) + ": " + "\n".join(tail),
            }
        if step == 2:
            counts = parse_counts(out)
            failures = parse_failures(out)
            known = set(row.known_failures())
            every_failure_named = len(failures) == counts["failed"] + counts["errors"]
            if rc == 5 or (rc == 0 and counts["passed"] == 0):
                status = "nothing-ran"  # nothing collected, or everything skipped
            elif rc == 0 and not counts["failed"] and not counts["errors"]:
                status = "pass"
            elif failures and every_failure_named and set(failures) <= known:
                status = "known-drift"
            else:
                status = "fail"
            tail = out.strip().splitlines()[-40:]
            return {
                **result,
                "counts": counts,
                "failures": failures,
                "known_fixed": sorted(known & set(parse_passed(out))),  # seen passing
                "status": status,
                "detail": "" if status == "pass" else "\n".join(tail),
            }
    raise AssertionError("unreachable")


def _clone(row: Row, workdir: Path) -> Path:
    dest = workdir / row.project
    if not dest.exists():
        url = f"https://github.com/{row.repo}.git"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--branch", row.branch, url, str(dest)],
            check=True,
        )
    return dest


def _src_matches_commit() -> bool:
    p = subprocess.run(
        ["git", "status", "--porcelain", "--", "src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return p.returncode == 0 and not p.stdout.strip()


def _record(projects: dict[str, dict]) -> dict:
    """The run record. `finding` per project is written by the person recording a baseline."""
    return {
        "date": dt.date.today().isoformat(),
        "evalkit_commit": _git_head(ROOT),
        "evalkit_src_matches_commit": _src_matches_commit(),
        "command": "python scripts/downstream_canary.py " + " ".join(sys.argv[1:]),
        "spend_usd": 0,
        "projects": projects,
    }


def _self_test() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "p"
        (proj / "tests").mkdir(parents=True)
        (proj / "tests" / "test_a.py").write_text("import evalkit\n", encoding="utf-8")
        row = Row("p", "me/p", "main", "x", "--extra e", "auto", "-", "-", "-", "")
        ok = run_one(row, proj, ROOT, FakeRunner("== 2 passed in 0.1s =="))
        bad = run_one(row, proj, ROOT, FakeRunner("== 1 failed, 1 passed in 0.1s ==", 1))
        setup = run_one(row, proj, ROOT, FakeRunner("", 0, fail_step=1))
        checks += [
            (ok["status"] == "pass", f"pass: {ok['status']}"),
            (bad["status"] == "fail", f"fail: {bad['status']}"),
            (setup["status"] == "setup-error", f"setup: {setup['status']}"),
        ]
        os.environ["SELFTEST_DB_PASS"] = "x"
        leaked = "SELFTEST_DB_PASS" in child_env(Path(tmp))
        checks.append((not leaked, "secret leaked into child env"))
        del os.environ["SELFTEST_DB_PASS"]
    failed = [m for good, m in checks if not good]
    for m in failed:
        print(f"self-test FAIL: {m}", file=sys.stderr)
    if not failed:
        print("downstream_canary self-test: OK")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--matrix", action="store_true")
    g.add_argument("--run-one", metavar="NAME")
    g.add_argument("--all", action="store_true")
    g.add_argument("--self-test", action="store_true")
    ap.add_argument("--checkout", type=Path)
    ap.add_argument("--workdir", type=Path)
    ap.add_argument("--evalkit", type=Path, default=ROOT)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    rows = load_registry()
    auto = [r for r in rows if r.tests in {"auto", "all"}]
    if args.matrix:
        include = [{"project": r.project, "repo": r.repo, "branch": r.branch} for r in auto]
        print(json.dumps({"include": include}))
        return 0
    if args.run_one:
        match = [r for r in auto if r.project == args.run_one]
        if not match or not args.checkout or not args.checkout.is_dir():
            print(
                f"downstream_canary: unknown project, or --checkout missing: {args.run_one}",
                file=sys.stderr,
            )
            return 2
        targets = [(match[0], args.checkout)]
    else:
        if not args.workdir:
            print("downstream_canary: --all needs --workdir", file=sys.stderr)
            return 2
        args.workdir.mkdir(parents=True, exist_ok=True)
        targets = [(r, _clone(r, args.workdir)) for r in auto]
    projects = {}
    for row, checkout in targets:
        res = run_one(row, checkout.resolve(), args.evalkit.resolve())
        projects[row.project] = res
        c = res["counts"]
        print(
            f"{row.project:24} {res['status']:12} "
            f"passed {c['passed']}, failed {c['failed']}, errors {c['errors']}"
        )
        if os.environ.get("GITHUB_ACTIONS") and res["status"] not in {"pass", *FAILING}:
            note = res.get("detail", "")[:200].replace("\n", " ")
            print(f"::warning title=canary {row.project}::{res['status']}: {note}")
    record = _record(projects)
    if args.out:
        args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if any(p["status"] in FAILING for p in projects.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
