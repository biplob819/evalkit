"""Code-version discipline (RUN-07): a node's behaviour cannot change without a version bump.

The manifest is keyed by the node's `name` (the value that goes into the cache key) and
records its `code_version` plus a hash over everything that defines its behaviour: the
source of every Node class in its MRO (so a shared base class counts) and of its `In` and
`Out` schema classes. If that hash changes while `code_version` stays the same, cached
outputs produced by the old logic would be served as if new; `check()` reports exactly that,
and `--update` refuses to record it.

Prompts and other string constants attached to the class are hashed by value. Methods that
cannot change a node's output (`estimate_cost`, `pool`: budgeting and scheduling) are left out,
so tightening a cost estimate never invalidates cached work.

Limit: module-level helper functions a node calls are not hashed. Keep node logic on the
class, or bump `code_version` by hand when a helper changes.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import pkgutil
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

from evalkit.runtime.node import Node

MANIFEST = Path(__file__).with_name("code_versions.json")
BUMP_HINT = "bump code_version"


def node_classes(*modules: ModuleType) -> dict[str, type[Node[Any, Any]]]:
    """Concrete Node subclasses defined in the given modules, keyed by module:qualname."""
    found: dict[str, type[Node[Any, Any]]] = {}
    for mod in modules:
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, Node)
                and obj is not Node
                and not inspect.isabstract(obj)
                and obj.__module__ == mod.__name__
            ):
                found[f"{obj.__module__}:{obj.__qualname__}"] = obj
    return found


def package_modules(package: str = "evalkit") -> list[ModuleType]:
    pkg = importlib.import_module(package)
    mods = [pkg]
    for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{package}."):
        mods.append(importlib.import_module(info.name))
    return mods


NON_OUTPUT_METHODS = frozenset({"estimate_cost", "pool"})
HASH_SCHEME = "2"  # 2: non-output methods excluded


def _source(cls: type) -> str:
    try:
        src = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError):
        return f"<no source: {cls.__module__}.{cls.__qualname__}>"
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return "\n".join(line.rstrip() for line in src.splitlines())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            node.body = [
                b
                for b in node.body
                if not (
                    isinstance(b, ast.FunctionDef | ast.AsyncFunctionDef)
                    and b.name in NON_OUTPUT_METHODS
                )
            ] or [ast.Pass()]
    return ast.unparse(tree)


def _string_constants(c: type) -> str:
    """Class-level string attributes (prompts, templates) hashed by value, so editing a
    prompt defined as a module constant and attached to the class forces a bump."""
    items = sorted(
        (k, v) for k, v in vars(c).items() if isinstance(v, str) and not k.startswith("__")
    )
    return repr(items)


def behaviour_hash(cls: type[Node[Any, Any]]) -> str:
    parts = []
    for c in cls.__mro__:
        if issubclass(c, Node) and c is not Node:
            parts += [_source(c), _string_constants(c)]
    for attr in ("In", "Out"):
        schema = getattr(cls, attr, None)
        if isinstance(schema, type):
            parts.append(_source(schema))
    return hashlib.sha256("\n\0\n".join(parts).encode("utf-8")).hexdigest()


def _named(
    classes: dict[str, type[Node[Any, Any]]],
) -> tuple[dict[str, type[Node[Any, Any]]], list[str]]:
    by_name: dict[str, type[Node[Any, Any]]] = {}
    problems: list[str] = []
    for qn, cls in sorted(classes.items()):
        name = getattr(cls, "name", None)
        version = getattr(cls, "code_version", None)
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            problems.append(f"{qn}: a concrete Node needs non-empty `name` and `code_version`")
            continue
        if name in by_name:
            other = by_name[name]
            problems.append(
                f"{qn}: node name {name!r} also used by {other.__module__}:{other.__qualname__}"
            )
            continue
        by_name[name] = cls
    return by_name, problems


def check(
    manifest: dict[str, dict[str, str]], classes: dict[str, type[Node[Any, Any]]]
) -> list[str]:
    by_name, problems = _named(classes)
    for name, cls in sorted(by_name.items()):
        entry = manifest.get(name)
        digest = behaviour_hash(cls)
        if entry is None:
            problems.append(f"{name}: not in the code-version manifest (run with --update)")
        elif entry["hash"] != digest and entry["code_version"] == cls.code_version:
            problems.append(
                f"{name}: behaviour changed but code_version is still {cls.code_version!r}; "
                f"{BUMP_HINT} so stale cached outputs are not reused"
            )
        elif entry["hash"] != digest or entry["code_version"] != cls.code_version:
            problems.append(f"{name}: manifest is stale (run with --update)")
    for name in sorted(set(manifest) - set(by_name)):
        problems.append(f"{name}: in the manifest but no longer exists (run with --update)")
    return problems


def build_manifest(classes: dict[str, type[Node[Any, Any]]]) -> dict[str, dict[str, str]]:
    by_name, problems = _named(classes)
    if problems:
        raise ValueError("; ".join(problems))
    return {
        name: {"code_version": cls.code_version, "hash": behaviour_hash(cls)}
        for name, cls in sorted(by_name.items())
    }


def load_manifest(path: Path = MANIFEST) -> dict[str, dict[str, str]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def write_manifest(manifest: dict[str, dict[str, str]], path: Path = MANIFEST) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
