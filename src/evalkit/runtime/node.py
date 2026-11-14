"""The node contract (RUN-01): typed input, typed output, a code version and a cache key."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from evalkit.runtime.canonical import sha256_hex
from evalkit.schemas._base import FrozenMap

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class RunContext(BaseModel):
    """What a node call depends on besides its input. `run_id` is not part of the cache key,
    so a rerun under a new run id reuses every completed result (exact rescoring, D-2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    model: str | None = None
    params: FrozenMap[float | int | str | bool] = Field(default_factory=dict)
    prompt_hash: str | None = None
    seed: int = 0


class Node(ABC, Generic[In, Out]):
    """One step of an evaluation graph. Subclasses set `name`, `code_version`, `In`, `Out`.

    Bump `code_version` whenever the node's logic changes: it is part of the cache key,
    so old cached outputs stop matching (RUN-07 enforces the bump).
    """

    name: ClassVar[str]
    code_version: ClassVar[str]
    In: ClassVar[type[BaseModel]]
    Out: ClassVar[type[BaseModel]]
    provider: ClassVar[str | None] = None  # concurrency pool; None shares the "_local" pool

    def cache_key(self, inp: In, ctx: RunContext) -> str:
        return sha256_hex(
            {
                "node": self.name,
                "code_version": self.code_version,
                "input": inp,
                "model": ctx.model,
                "params": dict(ctx.params),
                "prompt_hash": ctx.prompt_hash,
                "seed": ctx.seed,
            }
        )

    def estimate_cost(self, inp: In, ctx: RunContext) -> float:
        """Upper-bound cost of one call, used by the spend cap. Free nodes return 0."""
        return 0.0

    def cost_of(self, out: Out) -> float:
        """Actual cost of a finished call; defaults to the output's `cost_usd` if it has one."""
        return float(getattr(out, "cost_usd", 0.0) or 0.0)

    @abstractmethod
    async def run(self, inp: In, ctx: RunContext) -> Out: ...
