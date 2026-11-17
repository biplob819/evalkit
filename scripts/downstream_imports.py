#!/usr/bin/env python3
"""List every EvalKit name a downstream checkout imports, and class it against the public
contract (DSC-02, D-61). Downstream code is parsed, never imported.

Usage:
    python scripts/downstream_imports.py <checkout>            # grouped text report
    python scripts/downstream_imports.py <checkout> --json     # stable JSON
    python scripts/downstream_imports.py <checkout> --tests    # test files that import evalkit
    python scripts/downstream_imports.py --self-test

Classes:
    public            the module and name are in the public contract (check_public_api.PUBLIC),
                      or a public module imported whole
    public-elsewhere  the name is public but imported from a submodule path; fix the import
    internal          anything else
    private           an internal name starting with "_"

Exit 0 for a readable checkout, 2 when the path is not a directory.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_public_api import PUBLIC

SCANNED_DIRS = ("src", "tests", "scripts", "demo")
SKIPPED_PARTS = {".git", "node_modules", "__pycache__", "site-packages", ".tox", ".nox"}
KINDS = ("public", "public-elsewhere", "internal", "private")


@dataclass(frozen=True)
class Use:
    module: str
    name: str  # "*" for `import evalkit.x`
    kind: str
    files: int
    public_at: str = ""  # for public-elsewhere: where the name is public


@dataclass
class Report:
    root: str
    uses: list[Use] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    files_scanned: list[str] = field(default_factory=list)
    unparsable: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in KINDS}
        for u in self.uses:
            out[u.kind] += 1
        return dict(sorted(out.items()))


def classify(module: str, name: str) -> tuple[str, str]:
    """(kind, public_at) for one imported name."""
    if name in PUBLIC.get(module, ()) or (name == "*" and module in PUBLIC):
        return "public", ""
    if name != "*" and f"{module}.{name}" in PUBLIC:  # `from evalkit import scorers`
        return "public", ""
    if name != "*":
        for pkg, names in PUBLIC.items():
            if module.startswith(pkg + ".") and name in names:
                return "public-elsewhere", pkg
    if name.startswith("_") and name != "__version__":
        return "private", ""
    return "internal", ""


def _is_venv(path: Path) -> bool:
    return (path / "pyvenv.cfg").is_file() or path.name.startswith(".venv")


def _skipped(root: Path, rel: Path) -> bool:
    """Skip VCS, cache and virtual-environment folders (marked by pyvenv.cfg or named .venv*),
    and nothing else: a folder merely named like `tests/devenv` is scanned."""
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if part in SKIPPED_PARTS or _is_venv(current):
            return True
    return False


def is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") and (name.startswith("test_") or name.endswith("_test.py"))


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for d in SCANNED_DIRS:
        base = root / d
        if base.is_dir():
            files += [p for p in base.rglob("*.py") if not _skipped(root, p.relative_to(root))]
    return sorted(files)


def _imports(tree: ast.AST) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "evalkit":
                found |= {(node.module, a.name) for a in node.names}
        elif isinstance(node, ast.Import):
            found |= {(a.name, "*") for a in node.names if a.name.split(".")[0] == "evalkit"}
    return found


def audit(root: Path) -> Report:
    report = Report(root=str(root))
    seen: dict[tuple[str, str], int] = {}
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        report.files_scanned.append(rel)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            report.unparsable.append(rel)
            continue
        found = _imports(tree)
        for key in found:
            seen[key] = seen.get(key, 0) + 1
        if found and is_test_file(rel):
            report.test_files.append(rel)
    for (module, name), n in sorted(seen.items()):
        kind, at = classify(module, name)
        report.uses.append(Use(module, name, kind, n, at))
    return report


def to_json(report: Report) -> str:
    data = {
        "uses": [asdict(u) for u in report.uses],
        "counts": report.counts(),
        "test_files": report.test_files,
        "unparsable": report.unparsable,
    }
    return json.dumps(data, indent=2, sort_keys=True)


def to_text(report: Report) -> str:
    lines = [f"EvalKit imports in {report.root}"]
    for kind in KINDS:
        rows = [u for u in report.uses if u.kind == kind]
        if not rows:
            continue
        lines.append(f"\n{kind} ({len(rows)})")
        for u in rows:
            name = f"import {u.module}" if u.name == "*" else f"{u.module}.{u.name}"
            hint = f" -> {u.public_at}" if u.public_at else ""
            lines.append(f"  {name}{hint}  [{u.files} file{'s' if u.files != 1 else ''}]")
    if report.unparsable:
        lines.append("\nunparsable: " + ", ".join(report.unparsable))
    return "\n".join(lines)


def _self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "a.py").write_text(
            "from evalkit.scorers import sql_match\n"
            "from evalkit.adapters.base import SystemAdapter\n"
            "from evalkit.cost import call_cost\n"
            "from evalkit.stats import _quantile\n",
            encoding="utf-8",
        )
        (root / "src" / "bad.py").write_text("from evalkit import (\n", encoding="utf-8")
        (root / "tests" / "test_a.py").write_text("import evalkit\n", encoding="utf-8")
        r = audit(root)
        got = {(u.module, u.name): u.kind for u in r.uses}
        want = {
            ("evalkit.scorers", "sql_match"): "public",
            ("evalkit.adapters.base", "SystemAdapter"): "public-elsewhere",
            ("evalkit.cost", "call_cost"): "internal",
            ("evalkit.stats", "_quantile"): "private",
            ("evalkit", "*"): "public",
        }
        checks = [
            (got == want, f"classes: {got}"),
            (r.unparsable == ["src/bad.py"], f"unparsable: {r.unparsable}"),
            (r.test_files == ["tests/test_a.py"], f"tests: {r.test_files}"),
        ]
    failed = [msg for ok, msg in checks if not ok]
    for msg in failed:
        print(f"self-test FAIL: {msg}", file=sys.stderr)
    if not failed:
        print("downstream_imports self-test: OK")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("checkout", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tests", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.checkout:
        ap.error("checkout is required")
    root = Path(args.checkout)
    if not root.is_dir():
        print(f"downstream_imports: {root} is not a directory", file=sys.stderr)
        return 2
    report = audit(root)
    if args.tests:
        print("\n".join(report.test_files))
    elif args.json:
        print(to_json(report))
    else:
        print(to_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
