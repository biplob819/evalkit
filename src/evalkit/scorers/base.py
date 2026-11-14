"""The Scorer protocol, registry and plugin discovery (DET-04, DET-05).

Built-in scorers can never be replaced: the public `scorer` decorator refuses an existing
name, and discovery restores and reports any built-in a plugin tampers with. Plugins load
once per process; a broken plugin warns once and is skipped from then on.
"""

from __future__ import annotations

import functools
import importlib.metadata
import unicodedata
import warnings
from collections.abc import Callable
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import Field

from evalkit.schemas import Case, Completion
from evalkit.schemas._base import FrozenMap, JsonValue, Model, Name

ENTRY_POINT_GROUP = "evalkit.scorers"


class ScoreResult(Model):
    value: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    details: FrozenMap[JsonValue] = Field(default_factory=dict)


@runtime_checkable
class Scorer(Protocol):
    name: str
    version: str

    def __call__(self, case: Case, completion: Completion, **ctx: Any) -> ScoreResult: ...


def is_valid_scorer(obj: object) -> bool:
    return (
        isinstance(obj, Scorer)
        and isinstance(getattr(obj, "name", None), str)
        and bool(obj.name)
        and isinstance(getattr(obj, "version", None), str)
        and callable(obj)
    )


class FunctionScorer:
    """Wraps a plain function as a Scorer with a name and version."""

    __name__: str
    __wrapped__: Callable[..., ScoreResult]

    def __init__(self, fn: Callable[..., ScoreResult], name: str, version: str) -> None:
        self.fn, self.name, self.version = fn, name, version
        functools.update_wrapper(self, fn)

    def __call__(self, case: Case, completion: Completion, **ctx: Any) -> ScoreResult:
        return self.fn(case, completion, **ctx)

    def __reduce__(self) -> tuple[Any, tuple[str]]:
        return (get_scorer, (self.name,))  # pickles by registered name

    def __repr__(self) -> str:
        return f"<scorer {self.name} v{self.version}>"


_REGISTRY: dict[str, Scorer] = {}
_BUILTIN: dict[str, Scorer] = {}
_PLUGIN_SOURCE: dict[str, str] = {}  # scorer name -> entry point value that registered it
_FAILED: set[str] = set()  # entry point values that failed; not retried this process


def _register(s: Scorer, *, builtin: bool) -> None:
    if s.name in _REGISTRY:
        raise ValueError(f"a scorer named {s.name!r} is already registered")
    _REGISTRY[s.name] = s
    if builtin:
        _BUILTIN[s.name] = s


def scorer(
    name: Name, version: str = "1"
) -> Callable[[Callable[..., ScoreResult]], FunctionScorer]:
    """Decorator that turns a function into a registered Scorer. Names are unique: an
    existing name, built-in or not, is refused."""

    def deco(fn: Callable[..., ScoreResult]) -> FunctionScorer:
        s = FunctionScorer(fn, name, version)
        _register(s, builtin=fn.__module__.startswith("evalkit.scorers."))
        return s

    return deco


def normalise_label(label: str) -> str:
    """NFKC, strip whitespace and zero-width characters, case-fold (DET-01)."""
    text = unicodedata.normalize("NFKC", label)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text.strip().casefold()


def discover() -> dict[str, Scorer]:
    """Built-in scorers plus any registered by other packages under `evalkit.scorers`."""
    from evalkit.scorers import deterministic as _d  # noqa: F401 - registers built-ins

    builtins_before = dict(_BUILTIN)
    try:
        eps = list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # broken metadata must not take down the built-ins
        warnings.warn(f"could not read scorer plugins: {exc}", stacklevel=2)
        eps = []
    for ep in eps:
        source = getattr(ep, "value", repr(ep))
        if source in _FAILED:
            continue
        if ep.name in builtins_before:
            warnings.warn(f"plugin scorer {ep.name!r} ignored: the name is built in", stacklevel=2)
            _FAILED.add(source)
            continue
        if ep.name in _REGISTRY:
            if _PLUGIN_SOURCE.get(ep.name) not in (None, source):
                warnings.warn(
                    f"plugin scorer {ep.name!r} from {source} ignored: already provided by "
                    f"{_PLUGIN_SOURCE[ep.name]}",
                    stacklevel=2,
                )
                _FAILED.add(source)
            continue
        try:
            obj = ep.load()
        except Exception as exc:
            warnings.warn(f"plugin scorer {ep.name!r} failed to load: {exc}", stacklevel=2)
            _FAILED.add(source)
            continue
        if not is_valid_scorer(obj):
            warnings.warn(f"plugin {ep.name!r} is not a Scorer; ignored", stacklevel=2)
            _FAILED.add(source)
            continue
        if obj.name != ep.name:
            warnings.warn(
                f"plugin {ep.name!r} reports name {obj.name!r}; names must match, ignored",
                stacklevel=2,
            )
            _FAILED.add(source)
            continue
        _REGISTRY[ep.name] = obj
        _PLUGIN_SOURCE[ep.name] = source
    for name, original in builtins_before.items():
        if _REGISTRY.get(name) is not original:
            warnings.warn(f"a plugin replaced built-in scorer {name!r}; restored", stacklevel=2)
            _REGISTRY[name] = original
            _BUILTIN[name] = original
    for name in list(_BUILTIN):
        if name not in builtins_before:  # a plugin used the decorator: not a built-in
            del _BUILTIN[name]
    return dict(_REGISTRY)


def get_scorer(name: str) -> Scorer:
    scorers = discover()
    if name not in scorers:
        raise KeyError(f"unknown scorer {name!r}; available: {sorted(scorers)}")
    return scorers[name]
