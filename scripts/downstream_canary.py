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

Exit 0 when every project run passes, 1 when one does not, 2 on bad input.
"""

from __future__ import annotations

import argparse
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
    "note",
)
_SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped)")
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
    note: str

    def deselected(self) -> list[str]:
        return [] if self.deselect in {"", "-"} else self.deselect.split(",")


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
        rows.append(Row(*(c.strip() for c in cells)))
    return rows


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_RE.search(k)}
    env.pop("VIRTUAL_ENV", None)
    env["EVALKIT_DOWNSTREAM"] = "1"
    return env


def commands(row: Row, evalkit_root: Path, test_files: list[str]) -> list[list[str]]:
    pytest_cmd = ["uv", "run", "--no-sync", "pytest", "-q", "-p", "no:cacheprovider"]
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


def parse_counts(text: str) -> dict[str, int]:
    out = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    lines = [ln for ln in text.splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
    for n, word in _COUNT_RE.findall(lines[-1] if lines else text):
        out["errors" if word.startswith("error") else word] = int(n)
    return out


class Runner(Protocol):
    def __call__(self, cmd: list[str], cwd: Path) -> tuple[int, str]: ...


def real_runner(cmd: list[str], cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {TIMEOUT_S}s"
    return p.returncode, p.stdout + p.stderr


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


def run_one(row: Row, checkout: Path, evalkit_root: Path, runner: Runner = real_runner) -> dict:
    report = downstream_imports.audit(checkout)
    result: dict = {
        "repo": row.repo,
        "commit": _git_head(checkout),
        "pin": row.pin,
        "files": report.test_files,
        "audit": report.counts(),
        "counts": parse_counts(""),
    }
    if not report.test_files:
        return {**result, "status": "no-tests", "detail": "no test file imports evalkit"}
    for step, cmd in enumerate(commands(row, evalkit_root, report.test_files)):
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
            failed = rc != 0 or counts["failed"] or counts["errors"]
            tail = out.strip().splitlines()[-40:]
            return {
                **result,
                "counts": counts,
                "status": "fail" if failed else "pass",
                "detail": "" if not failed else "\n".join(tail),
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


def _record(projects: dict[str, dict]) -> dict:
    return {
        "date": dt.date.today().isoformat(),
        "evalkit_commit": _git_head(ROOT),
        "projects": projects,
    }


def _self_test() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "p"
        (proj / "tests").mkdir(parents=True)
        (proj / "tests" / "test_a.py").write_text("import evalkit\n", encoding="utf-8")
        row = Row("p", "me/p", "main", "x", "--extra e", "auto", "-", "-", "")
        ok = run_one(row, proj, ROOT, FakeRunner("== 2 passed in 0.1s =="))
        bad = run_one(row, proj, ROOT, FakeRunner("== 1 failed, 1 passed in 0.1s ==", 1))
        setup = run_one(row, proj, ROOT, FakeRunner("", 0, fail_step=1))
        checks += [
            (ok["status"] == "pass", f"pass: {ok['status']}"),
            (bad["status"] == "fail", f"fail: {bad['status']}"),
            (setup["status"] == "setup-error", f"setup: {setup['status']}"),
        ]
        os.environ["SELFTEST_API_KEY"] = "x"
        checks.append(("SELFTEST_API_KEY" not in child_env(), "key leaked into child env"))
        del os.environ["SELFTEST_API_KEY"]
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
    auto = [r for r in rows if r.tests == "auto"]
    if args.matrix:
        include = [{"project": r.project, "repo": r.repo, "branch": r.branch} for r in auto]
        print(json.dumps({"include": include}))
        return 0
    if args.run_one:
        match = [r for r in auto if r.project == args.run_one]
        if not match or not args.checkout:
            print(
                f"downstream_canary: unknown project or no --checkout: {args.run_one}",
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
    record = _record(projects)
    if args.out:
        args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if all(p["status"] == "pass" for p in projects.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
