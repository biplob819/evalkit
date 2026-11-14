"""A fake ModelAdapter for pipeline tests: scripted outputs, no network, no spend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from evalkit.adapters import (
    BatchRequest,
    BatchResult,
    CompletionRequest,
    ModelAdapter,
    RawCompletion,
)


class ScriptedAdapter(ModelAdapter):
    provider = "fake"

    def __init__(self, reply: Callable[[CompletionRequest], str | Exception | RawCompletion]):
        self.reply = reply
        self.requests: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> RawCompletion:
        await asyncio.sleep(0)  # a real call yields to other tasks
        self.requests.append(req)
        out = self.reply(req)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, RawCompletion):
            return out
        return RawCompletion(
            model=req.model,
            model_snapshot=req.model_id + "-snap",
            params=dict(req.params),
            raw_text=out,
            tokens_in=600,
            tokens_out=60,
            tokens_reasoning=200,
            latency_ms=900,
            cost_usd=0.001,
        )

    async def submit_batch(self, reqs: list[BatchRequest]) -> str:  # pragma: no cover
        raise NotImplementedError

    async def collect_batch(
        self, batch_id: str, reqs: Mapping[str, CompletionRequest]
    ) -> list[BatchResult] | None:  # pragma: no cover
        raise NotImplementedError


class ScriptedBatchAdapter(ScriptedAdapter):
    """Batch support on top of ScriptedAdapter: `pending_polls` collects return None first."""

    def __init__(self, reply, pending_polls: int = 0, item_error=None, drop=None):
        super().__init__(reply)
        self.pending_polls = pending_polls
        self.item_error = item_error or (lambda cid: None)
        self.drop = drop or (lambda cid: False)  # ids the provider returns nothing for
        self.submitted: list[list[BatchRequest]] = []
        self.polls = 0

    async def submit_batch(self, reqs: list[BatchRequest]) -> str:  # type: ignore[override]
        await asyncio.sleep(0)  # a real submit is a network call: let other tasks run
        self.submitted.append(list(reqs))
        return f"batch_{len(self.submitted)}"

    async def collect_batch(self, batch_id, reqs):  # type: ignore[override]
        await asyncio.sleep(0)  # a real status check is a network call
        self.polls += 1
        if self.polls <= self.pending_polls:
            return None
        n = int(batch_id.removeprefix("batch_")) - 1
        sent = {r.custom_id: r.request for r in self.submitted[n]}  # results belong to that batch
        assert set(reqs) <= set(sent), "collect asked for ids that were not in this batch"
        out = []
        for cid in reqs:
            if self.drop(cid):
                continue
            req = sent[cid]
            err = self.item_error(cid)
            if err:
                out.append(BatchResult(custom_id=cid, error=err))
            else:
                raw = await self.complete(req)
                out.append(
                    BatchResult(custom_id=cid, completion=raw.model_copy(update={"batch": True}))
                )
        return out
