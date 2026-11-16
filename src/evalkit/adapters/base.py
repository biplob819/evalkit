"""The adapter interface (ADP-01/02/08/09): every provider looks the same to EvalKit.

v0.2 (D-59) adds `SystemAdapter`: a system under test that answers whole cases.
"""

from __future__ import annotations

import importlib.metadata
import itertools
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import Field, model_validator

from evalkit.schemas._base import (
    FrozenMap,
    JsonValue,
    Model,
    Name,
    NonBlank,
    NonNegFloat,
    NonNegInt,
    PosInt,
)
from evalkit.schemas.benchmark import Case

ALLOWED_PARAMS = frozenset({"effort"})  # no temperature/top_p/top_k (D-22)


class CompletionRequest(Model):
    model: Name  # alias from config, e.g. "sonnet"
    model_id: Name  # provider model id, e.g. "claude-sonnet-5-5"
    prompt: str
    system: str | None = None
    max_output_tokens: PosInt = 1024
    params: FrozenMap[str] = Field(default_factory=dict)
    json_schema: FrozenMap[JsonValue] | None = None  # structured output (D-24)
    # Optional multi-turn conversation (an agent's history): alternating user/assistant
    # turns, starting and ending with a user turn. When set, providers send these turns
    # instead of `prompt`, and `prompt` must equal `render_turns(turns)` so caching keys see
    # the same text. Each closed turn is a cacheable prefix: `cache` asks the provider to cache
    # it (Anthropic automatic caching; OpenAI caches long prefixes on its own). Prefixes inside
    # one message do not cache (measured, D-51).
    turns: tuple[tuple[Literal["user", "assistant"], str], ...] | None = None
    cache: bool = False

    @model_validator(mode="after")
    def _turns_valid(self) -> CompletionRequest:
        if self.turns is None:
            return self
        roles = [r for r, _ in self.turns]
        if not roles or roles[0] != "user" or roles[-1] != "user":
            raise ValueError("turns must start and end with a user turn")
        if any(a == b for a, b in itertools.pairwise(roles)):
            raise ValueError("turns must alternate between user and assistant")
        if render_turns(self.turns) != self.prompt:
            raise ValueError("prompt must equal render_turns(turns)")
        return self


def render_turns(turns: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> str:
    """One text for a conversation (the cache key and providers' fallback)."""
    return "".join(f"<{role}>\n{text}\n</{role}>\n" for role, text in turns)


class RawCompletion(Model):
    """What a provider returned, before EvalKit parses the answer (A2.1 builds Completion)."""

    model: Name
    model_snapshot: str
    params: FrozenMap[str]
    raw_text: str
    tokens_in: NonNegInt  # whole prompt, including cached and cache-write tokens
    tokens_out: NonNegInt  # visible output only
    tokens_reasoning: NonNegInt = 0  # thinking/reasoning tokens, billed as output
    tokens_cached: NonNegInt = 0
    tokens_cache_write: NonNegInt = 0
    latency_ms: NonNegInt
    cost_usd: NonNegFloat
    request_id: str | None = None
    stop_reason: str = ""
    refusal: bool = False
    batch: bool = False


class BatchRequest(Model):
    custom_id: NonBlank
    request: CompletionRequest


class BatchResult(Model):
    custom_id: NonBlank
    completion: RawCompletion | None = None
    error: str | None = None


def check_params(req: CompletionRequest, *, require_effort: bool = True) -> None:
    bad = set(req.params) - ALLOWED_PARAMS
    if bad:
        raise ValueError(
            f"unsupported request params {sorted(bad)}: only {sorted(ALLOWED_PARAMS)} are "
            "allowed (sampling parameters are rejected by current models, D-22)"
        )
    if require_effort and "effort" not in req.params:
        raise ValueError(
            f"{req.model}: set params.effort explicitly; per-model defaults differ (D-24)"
        )


def check_unique_ids(reqs: list[BatchRequest]) -> None:
    ids = [r.custom_id for r in reqs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate custom_id values in batch: {dupes}")


class ModelAdapter(ABC):
    provider: ClassVar[str]

    @abstractmethod
    async def complete(self, req: CompletionRequest) -> RawCompletion: ...

    @abstractmethod
    async def submit_batch(self, reqs: list[BatchRequest]) -> str: ...

    @abstractmethod
    async def collect_batch(
        self, batch_id: str, reqs: Mapping[str, CompletionRequest]
    ) -> list[BatchResult] | None:
        """None while the batch is still running; otherwise one result per custom_id."""


_REGISTRY: dict[str, type[ModelAdapter]] = {}
ENTRY_POINT_GROUP = "evalkit.adapters"
_BUILTIN = frozenset({"anthropic", "openai", "openai_compatible"})


A = TypeVar("A", bound=type["ModelAdapter"])


def register(name: str) -> Callable[[A], A]:
    def deco(cls: A) -> A:
        _REGISTRY[name] = cls
        return cls

    return deco


def discover() -> dict[str, type[ModelAdapter]]:
    """Built-in adapters plus any registered by other packages under `evalkit.adapters`."""
    from evalkit.adapters import anthropic as _a  # noqa: F401 - registers on import
    from evalkit.adapters import openai as _o  # noqa: F401

    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in _BUILTIN:
            warnings.warn(f"plugin adapter {ep.name!r} ignored: the name is built in", stacklevel=2)
            continue
        if ep.name in _REGISTRY:
            continue
        try:
            cls = ep.load()
        except Exception as exc:  # a broken plugin must not take down the built-ins
            warnings.warn(f"plugin adapter {ep.name!r} failed to load: {exc}", stacklevel=2)
            continue
        if not (isinstance(cls, type) and issubclass(cls, ModelAdapter)):
            warnings.warn(
                f"plugin {ep.name!r} is not a ModelAdapter subclass; ignored", stacklevel=2
            )
            continue
        _REGISTRY[ep.name] = cls
    return dict(_REGISTRY)


def get_adapter(name: str, **kwargs: Any) -> ModelAdapter:
    adapters = discover()
    if name not in adapters:
        raise KeyError(f"unknown adapter {name!r}; available: {sorted(adapters)}")
    return adapters[name](**kwargs)


# ---- systems under test (v0.2, D-59) -----------------------------------------------------------


class SystemAdapter(ModelAdapter):
    """A system under test: it answers one whole `Case`, not a bare prompt.

    `answer` receives the full case (id, question, context, meta) and the request a model would
    have been sent (the task's rendered prompt, the answer schema, the output budget). It
    returns a `RawCompletion` whose `raw_text` is the task's answer JSON, which EvalKit then
    validates, extracts, judges and scores exactly like a model's answer. Route a model alias
    to it in `evalkit.yaml` with `systems: {alias: registered-name}`.

    `version` is part of every cache key: bump it whenever the system's answers would change,
    or a rerun serves the old answers from the cache. `estimate_cost` is the most one call can
    cost; it is held against `--max-cost` while the call runs. Raise a transient error
    (`ConnectionError`, `TimeoutError`, anything with a 429 or 5xx `status_code`) for a failure
    worth recording as `error="transport"`; any other exception stops the run.
    """

    provider: ClassVar[str] = "system"
    version: ClassVar[str] = "1"

    @abstractmethod
    async def answer(self, case: Case, request: CompletionRequest) -> RawCompletion: ...

    def estimate_cost(self, case: Case, request: CompletionRequest) -> float:
        return 0.0

    async def complete(self, req: CompletionRequest) -> RawCompletion:
        raise TypeError(
            f"{type(self).__name__} is a system under test and needs the case: call "
            "answer(case, request)"
        )

    async def submit_batch(self, reqs: list[BatchRequest]) -> str:
        raise NotImplementedError("systems under test have no batch API")

    async def collect_batch(
        self, batch_id: str, reqs: Mapping[str, CompletionRequest]
    ) -> list[BatchResult] | None:
        raise NotImplementedError("systems under test have no batch API")


_SYSTEMS: dict[str, type[SystemAdapter]] = {}
_FAILED_SYSTEMS: set[str] = set()
SYSTEM_ENTRY_POINT_GROUP = "evalkit.systems"

S = TypeVar("S", bound=type[SystemAdapter])


def register_system(name: str) -> Callable[[S], S]:
    """Register a SystemAdapter subclass under a unique name."""

    def deco(cls: S) -> S:
        if not (isinstance(cls, type) and issubclass(cls, SystemAdapter)):
            raise TypeError(f"system {name!r} must subclass SystemAdapter")
        if name in _SYSTEMS and _SYSTEMS[name] is not cls:
            raise ValueError(f"a system named {name!r} is already registered")
        _SYSTEMS[name] = cls
        return cls

    return deco


def discover_systems() -> dict[str, type[SystemAdapter]]:
    """Registered systems plus any published under the `evalkit.systems` entry-point group.
    A plugin that fails to load warns once and is skipped."""
    try:
        eps = list(importlib.metadata.entry_points(group=SYSTEM_ENTRY_POINT_GROUP))
    except Exception as exc:  # broken metadata must not break runs that use no system
        warnings.warn(f"could not read system plugins: {exc}", stacklevel=2)
        eps = []
    for ep in eps:
        source = getattr(ep, "value", repr(ep))
        if ep.name in _SYSTEMS or source in _FAILED_SYSTEMS:
            continue
        try:
            cls = ep.load()
        except Exception as exc:
            warnings.warn(f"plugin system {ep.name!r} failed to load: {exc}", stacklevel=2)
            _FAILED_SYSTEMS.add(source)
            continue
        if not (isinstance(cls, type) and issubclass(cls, SystemAdapter)):
            warnings.warn(
                f"plugin {ep.name!r} is not a SystemAdapter subclass; ignored", stacklevel=2
            )
            _FAILED_SYSTEMS.add(source)
            continue
        _SYSTEMS[ep.name] = cls
    return dict(_SYSTEMS)


def get_system(name: str, **kwargs: Any) -> SystemAdapter:
    systems = discover_systems()
    if name not in systems:
        raise KeyError(f"unknown system {name!r}; registered: {sorted(systems)}")
    return systems[name](**kwargs)
