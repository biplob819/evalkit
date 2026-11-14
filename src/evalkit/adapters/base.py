"""The adapter interface (ADP-01/02/08/09): every provider looks the same to EvalKit."""

from __future__ import annotations

import importlib.metadata
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, TypeVar

from pydantic import Field

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

ALLOWED_PARAMS = frozenset({"effort"})  # no temperature/top_p/top_k (D-22)


class CompletionRequest(Model):
    model: Name  # alias from config, e.g. "sonnet"
    model_id: Name  # provider model id, e.g. "claude-sonnet-5-5"
    prompt: str
    system: str | None = None
    max_output_tokens: PosInt = 1024
    params: FrozenMap[str] = Field(default_factory=dict)
    json_schema: FrozenMap[JsonValue] | None = None  # structured output (D-24)


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
