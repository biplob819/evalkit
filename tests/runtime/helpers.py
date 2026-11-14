"""Tiny nodes used by the runtime tests."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from evalkit.runtime import Node, RunContext


class Num(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: int


class Paid(BaseModel):
    model_config = ConfigDict(frozen=True)
    y: int
    cost_usd: float


class Double(Node[Num, Num]):
    name = "double"
    code_version = "1"
    In = Num
    Out = Num

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, inp: Num, ctx: RunContext) -> Num:
        self.calls += 1
        return Num(x=inp.x * 2)


class Flaky(Node[Num, Num]):
    """Fails on one chosen input until `broken` is cleared."""

    name = "flaky"
    code_version = "1"
    In = Num
    Out = Num

    def __init__(self, broken: int) -> None:
        self.broken: int | None = broken
        self.calls = 0

    async def run(self, inp: Num, ctx: RunContext) -> Num:
        self.calls += 1
        if inp.x == self.broken:
            raise RuntimeError(f"boom on {inp.x}")
        return Num(x=inp.x + 100)


class Slow(Node[Num, Num]):
    """Records the peak number of concurrent calls."""

    name = "slow"
    code_version = "1"
    In = Num
    Out = Num
    provider: ClassVar[str | None] = "anthropic"

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def run(self, inp: Num, ctx: RunContext) -> Num:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return Num(x=inp.x)


class Spend(Node[Num, Paid]):
    name = "spend"
    code_version = "1"
    In = Num
    Out = Paid
    provider: ClassVar[str | None] = "openai"

    def __init__(self, cost: float = 1.0) -> None:
        self.cost = cost
        self.calls = 0

    def estimate_cost(self, inp: Num, ctx: RunContext) -> float:
        return self.cost

    async def run(self, inp: Num, ctx: RunContext) -> Paid:
        self.calls += 1
        return Paid(y=inp.x, cost_usd=self.cost)
