"""Anthropic Messages and Message Batches adapter (ADP-01, ADP-03).

- No sampling parameters (D-22), no refusal fallbacks (D-23), effort set explicitly from
  request params (D-24), JSON via `output_config.format` (D-24).
- `output_tokens` includes thinking; thinking is reported separately as tokens_reasoning.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from typing import Any

from evalkit.adapters.base import (
    BatchRequest,
    BatchResult,
    CompletionRequest,
    ModelAdapter,
    RawCompletion,
    check_params,
    check_unique_ids,
    register,
)
from evalkit.cost import Price, call_cost, load_prices, price_for
from evalkit.ratelimit import TokenBucket
from evalkit.retry import with_retries
from evalkit.runtime.canonical import to_jsonable


@register("anthropic")
class AnthropicAdapter(ModelAdapter):
    provider = "anthropic"

    require_effort = True

    def __init__(
        self,
        client: Any = None,
        prices: dict[str, Price] | None = None,
        bucket: TokenBucket | None = None,
        **retry: Any,
    ):
        self._client = client
        self._prices = prices if prices is not None else load_prices()
        self._bucket = bucket  # optional requests-per-second pacing (ADP-07)
        self._retry = retry

    async def _paced(self, call: Any) -> Any:
        if self._bucket is not None:
            await self._bucket.acquire()
        return await with_retries(call, **self._retry)

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic  # optional dependency, imported only when used

            self._client = anthropic.AsyncAnthropic(max_retries=0)
        return self._client

    def build_params(self, req: CompletionRequest) -> dict[str, Any]:
        check_params(req, require_effort=self.require_effort)
        params: dict[str, Any] = {
            "model": req.model_id,
            "max_tokens": req.max_output_tokens,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.turns:
            params["messages"] = [{"role": r, "content": t} for r, t in req.turns]
        if req.cache:
            # automatic caching: the breakpoint follows the last block as the history grows
            params["cache_control"] = {"type": "ephemeral"}
        if req.system:
            params["system"] = req.system
        output_config: dict[str, Any] = {}
        if "effort" in req.params:
            output_config["effort"] = req.params["effort"]
        if req.json_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": to_jsonable(req.json_schema),
            }
        if output_config:
            params["output_config"] = output_config
        return params

    def parse(
        self, message: Any, req: CompletionRequest, latency_ms: int, *, batch: bool = False
    ) -> RawCompletion:
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        u = message.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        details = getattr(u, "output_tokens_details", None)
        thinking = (getattr(details, "thinking_tokens", 0) or 0) if details else 0
        thinking = min(thinking, u.output_tokens)  # never bill more reasoning than output
        tokens_in = u.input_tokens + cache_read + cache_write
        tokens_out = max(u.output_tokens - thinking, 0)
        cost = call_cost(
            price_for(self._prices, req.model_id),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_reasoning=thinking,
            tokens_cached=cache_read,
            tokens_cache_write=cache_write,
            batch=batch,
        )
        stop = getattr(message, "stop_reason", "") or ""
        return RawCompletion(
            model=req.model,
            model_snapshot=getattr(message, "model", "") or "",
            params=dict(req.params),
            raw_text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_reasoning=thinking,
            tokens_cached=cache_read,
            tokens_cache_write=cache_write,
            latency_ms=latency_ms,
            cost_usd=cost,
            request_id=getattr(message, "_request_id", None) or getattr(message, "id", None),
            stop_reason=stop,
            refusal=stop == "refusal",
            batch=batch,
        )

    async def complete(self, req: CompletionRequest) -> RawCompletion:
        params = self.build_params(req)
        t0 = time.perf_counter()
        message = await self._paced(lambda: self.client.messages.create(**params))
        return self.parse(message, req, int((time.perf_counter() - t0) * 1000))

    async def submit_batch(self, reqs: list[BatchRequest]) -> str:
        check_unique_ids(reqs)
        requests = [
            {"custom_id": r.custom_id, "params": self.build_params(r.request)} for r in reqs
        ]
        batch = await self._paced(lambda: self.client.messages.batches.create(requests=requests))
        return str(batch.id)

    async def collect_batch(
        self, batch_id: str, reqs: Mapping[str, CompletionRequest]
    ) -> list[BatchResult] | None:
        status = await with_retries(
            lambda: self.client.messages.batches.retrieve(batch_id), **self._retry
        )
        if status.processing_status != "ended":
            return None
        results = self.client.messages.batches.results(batch_id)
        if inspect.isawaitable(results):
            results = await results
        out: list[BatchResult] = []
        seen: set[str] = set()
        async for item in results:
            cid = item.custom_id
            seen.add(cid)
            if cid not in reqs:
                continue  # not ours; results arrive in any order and are keyed by custom_id
            kind = item.result.type
            if kind == "succeeded":
                out.append(
                    BatchResult(
                        custom_id=cid,
                        completion=self.parse(
                            item.result.message, reqs[cid], latency_ms=0, batch=True
                        ),
                    )
                )
            else:
                err = getattr(item.result, "error", None)
                # SDK shape: result.error is an ErrorResponse whose .error carries the type.
                detail = getattr(getattr(err, "error", None), "type", None) or getattr(
                    err, "type", None
                )
                out.append(BatchResult(custom_id=cid, error=f"{kind}:{detail}" if detail else kind))
        for cid in sorted(set(reqs) - seen):
            out.append(BatchResult(custom_id=cid, error="missing_from_results"))
        return out
