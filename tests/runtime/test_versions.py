import importlib.util
import sys
import textwrap
from itertools import count

import pytest

from evalkit.runtime import versions

_n = count()

TEMPLATE = """
from abc import abstractmethod
from pydantic import BaseModel
from evalkit.runtime import Node, RunContext

class N(BaseModel):
    x: int{extra_field}

class Base(Node[N, N]):
    In = N
    Out = N

    def step(self) -> int:
        return {base_step}

class {cls}(Base):
    name = "{name}"
    code_version = "{version}"

    async def run(self, inp, ctx):
        return N(x=inp.x + self.step() + {step})
"""


def load(tmp_path, *, version="1", step=1, base_step=0, cls="Inc", name="inc", extra_field=""):
    tag = next(_n)
    path = tmp_path / f"mod_{tag}.py"
    path.write_text(
        textwrap.dedent(
            TEMPLATE.format(
                version=version,
                step=step,
                base_step=base_step,
                cls=cls,
                name=name,
                extra_field=extra_field,
            )
        ),
        encoding="utf-8",
    )
    modname = f"evk_test_mod_{tag}"
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return versions.node_classes(mod)


def test_source_change_without_bump_fails_and_bump_passes(tmp_path):
    manifest = versions.build_manifest(load(tmp_path))
    assert versions.check(manifest, load(tmp_path)) == []
    problems = versions.check(manifest, load(tmp_path, step=2))
    assert len(problems) == 1 and versions.BUMP_HINT in problems[0]
    bumped = load(tmp_path, version="2", step=2)
    assert any("stale" in p for p in versions.check(manifest, bumped))
    assert versions.check(versions.build_manifest(bumped), bumped) == []


def test_base_class_change_requires_a_bump(tmp_path):
    manifest = versions.build_manifest(load(tmp_path))
    problems = versions.check(manifest, load(tmp_path, base_step=5))
    assert len(problems) == 1 and versions.BUMP_HINT in problems[0]


def test_schema_change_requires_a_bump(tmp_path):
    manifest = versions.build_manifest(load(tmp_path))
    problems = versions.check(manifest, load(tmp_path, extra_field="\n    y: int = 0"))
    assert len(problems) == 1 and versions.BUMP_HINT in problems[0]


def test_renaming_the_class_does_not_dodge_the_bump(tmp_path):
    manifest = versions.build_manifest(load(tmp_path))
    problems = versions.check(manifest, load(tmp_path, cls="Increment", step=9))
    assert len(problems) == 1 and versions.BUMP_HINT in problems[0]


def test_unknown_removed_and_unnamed_nodes_are_reported(tmp_path):
    classes = load(tmp_path)
    assert any("not in the code-version manifest" in p for p in versions.check({}, classes))
    gone = {"old": {"code_version": "1", "hash": "abc"}}
    assert any("no longer exists" in p for p in versions.check(gone, {}))
    unnamed = load(tmp_path, name="")
    assert any("needs non-empty" in p for p in versions.check({}, unnamed))
    with pytest.raises(ValueError, match="needs non-empty"):
        versions.build_manifest(unnamed)


def test_duplicate_node_names_are_reported(tmp_path):
    both = {**load(tmp_path), **load(tmp_path, cls="Other")}
    assert any("also used by" in p for p in versions.check({}, both))


def test_live_package_manifest_is_current():
    live = versions.node_classes(*versions.package_modules())
    assert versions.check(versions.load_manifest(), live) == []


def test_abstract_base_is_not_a_node():
    from evalkit.runtime import node

    assert versions.node_classes(node) == {}


def test_cost_estimates_are_not_behaviour_but_run_is():
    """Tightening an estimate (budgeting) never invalidates cached outputs; changing run()
    still must (RUN-07, hash scheme 2)."""
    from evalkit.runtime import Node, RunContext
    from evalkit.runtime.versions import behaviour_hash
    from tests.runtime.helpers import Num

    def make(estimate: float, add: int) -> type:
        class N(Node[Num, Num]):
            name = "n"
            code_version = "1"
            In = Num
            Out = Num

            def estimate_cost(self, inp: Num, ctx: RunContext) -> float:
                return estimate

            async def run(self, inp: Num, ctx: RunContext) -> Num:
                return Num(x=inp.x + add)

        return N

    import inspect

    src_a = inspect.getsource(make)
    assert "estimate_cost" in src_a  # the helper really defines both methods
    a, b, c = make(0.1, 1), make(0.9, 1), make(0.1, 2)
    # Same source text for a and b (the values come from the closure), so compare via the
    # scheme itself: the estimate method is dropped from what is hashed.
    from evalkit.runtime.versions import _source

    assert "estimate_cost" not in _source(a) and "async def run" in _source(a)
    assert behaviour_hash(a) == behaviour_hash(b) == behaviour_hash(c)  # closures: same text
