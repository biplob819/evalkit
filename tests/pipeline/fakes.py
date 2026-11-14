"""A fake ModelAdapter for pipeline tests: scripted outputs, no network, no spend."""

from __future__ import annotations

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
