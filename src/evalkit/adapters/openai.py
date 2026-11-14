"""OpenAI Chat Completions and Batch adapter (ADP-02, ADP-03), plus OpenAI-compatible
servers such as Ollama, vLLM or LM Studio (ADP-08).

- Effort maps to `reasoning_effort` (D-24); no sampling parameters (D-22).
- JSON via `response_format` json_schema (strict). `completion_tokens` includes reasoning;
  reasoning is reported separately as tokens_reasoning.
"""

from __future__ import annotations

import json
import os
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

FAILED_BATCH = frozenset({"failed", "expired", "cancelled"})


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


@register("openai")
class OpenAIAdapter(ModelAdapter):
    provider = "openai"
    free_if_unknown = False

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

    def _make_client(self) -> Any:
        import openai  # optional dependency, imported only when used

        return openai.AsyncOpenAI(max_retries=0)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def build_params(self, req: CompletionRequest) -> dict[str, Any]:
        check_params(req, require_effort=self.require_effort)
        messages: list[dict[str, str]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": req.prompt})
        params: dict[str, Any] = {
            "model": req.model_id,
            "messages": messages,
            "max_completion_tokens": req.max_output_tokens,
        }
        if "effort" in req.params:
            params["reasoning_effort"] = req.params["effort"]
        if req.json_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "schema": to_jsonable(req.json_schema),
                    "strict": True,
                },
            }
        return params

    def parse(
        self, resp: Any, req: CompletionRequest, latency_ms: int, *, batch: bool = False
    ) -> RawCompletion:
        choice = _get(resp, "choices")[0]
        msg = _get(choice, "message")
        text = _get(msg, "content") or ""
        usage = _get(resp, "usage")
        prompt = _get(usage, "prompt_tokens", 0) or 0
        completion = _get(usage, "completion_tokens", 0) or 0
        reasoning = _get(_get(usage, "completion_tokens_details") or {}, "reasoning_tokens", 0) or 0
        cached = _get(_get(usage, "prompt_tokens_details") or {}, "cached_tokens", 0) or 0
        tokens_out = max(completion - reasoning, 0)
        price = price_for(self._prices, req.model_id, free_if_unknown=self.free_if_unknown)
        cost = call_cost(
            price,
            tokens_in=prompt,
            tokens_out=tokens_out,
            tokens_reasoning=reasoning,
            tokens_cached=cached,
            batch=batch,
        )
        refusal = bool(_get(msg, "refusal"))
        stop = _get(choice, "finish_reason") or ""
        return RawCompletion(
            model=req.model,
            model_snapshot=_get(resp, "model") or "",
            params=dict(req.params),
            raw_text=text,
            tokens_in=prompt,
            tokens_out=tokens_out,
            tokens_reasoning=reasoning,
            tokens_cached=cached,
            latency_ms=latency_ms,
            cost_usd=cost,
            request_id=_get(resp, "_request_id") or _get(resp, "id"),
            stop_reason="refusal" if refusal else stop,
            refusal=refusal,
            batch=batch,
        )

    async def complete(self, req: CompletionRequest) -> RawCompletion:
        params = self.build_params(req)
        t0 = time.perf_counter()
        resp = await self._paced(lambda: self.client.chat.completions.create(**params))
        return self.parse(resp, req, int((time.perf_counter() - t0) * 1000))

    async def submit_batch(self, reqs: list[BatchRequest]) -> str:
        check_unique_ids(reqs)
        lines = [
            json.dumps(
                {
                    "custom_id": r.custom_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": self.build_params(r.request),
                },
                ensure_ascii=False,
            )
            for r in reqs
        ]
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        upload = await with_retries(
            lambda: self.client.files.create(
                file=("evalkit-batch.jsonl", payload), purpose="batch"
            ),
            **self._retry,
        )
        batch = await self._paced(
            lambda: self.client.batches.create(
                input_file_id=upload.id, endpoint="/v1/chat/completions", completion_window="24h"
            ),
        )
        return str(batch.id)

    async def collect_batch(
        self, batch_id: str, reqs: Mapping[str, CompletionRequest]
    ) -> list[BatchResult] | None:
        batch = await with_retries(lambda: self.client.batches.retrieve(batch_id), **self._retry)
        if batch.status not in FAILED_BATCH and batch.status != "completed":
            return None  # validating, in_progress, finalizing, cancelling
        # Expired or cancelled batches can still hold finished, billed results: read them.
        missing_error = (
            "missing_from_results" if batch.status == "completed" else f"batch_{batch.status}"
        )
        lines: list[str] = []
        for file_id in (batch.output_file_id, getattr(batch, "error_file_id", None)):
            if file_id:
                content = await with_retries(
                    lambda f=file_id: self.client.files.content(f), **self._retry
                )
                lines += [ln for ln in content.text.splitlines() if ln.strip()]
        out: list[BatchResult] = []
        seen: set[str] = set()
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a damaged line; its request is reported missing below
            cid = row.get("custom_id")
            if cid not in reqs or cid in seen:
                continue
            seen.add(cid)
            response = row.get("response") or {}
            if row.get("error") or response.get("status_code") != 200:
                err = (row.get("error") or {}).get("code") or f"http_{response.get('status_code')}"
                out.append(BatchResult(custom_id=cid, error=str(err)))
            else:
                out.append(
                    BatchResult(
                        custom_id=cid,
                        completion=self.parse(
                            response["body"], reqs[cid], latency_ms=0, batch=True
                        ),
                    )
                )
        for cid in sorted(set(reqs) - seen):
            out.append(BatchResult(custom_id=cid, error=missing_error))
        return out


@register("openai_compatible")
class OpenAICompatibleAdapter(OpenAIAdapter):
    """Any server that speaks the OpenAI Chat Completions API (ADP-08). Models missing from
    prices.yaml are costed at $0 with a warning, since local models are usually free."""

    provider = "openai_compatible"
    free_if_unknown = True
    require_effort = False  # local models often have no reasoning effort setting

    def __init__(
        self,
        base_url: str,
        api_key_env: str | None = None,
        client: Any = None,
        prices: dict[str, Price] | None = None,
        **retry: Any,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must start with http:// or https://, got {base_url!r}")
        super().__init__(client=client, prices=prices, **retry)
        self.base_url = base_url
        self.api_key_env = api_key_env

    def _make_client(self) -> Any:
        import openai

        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ValueError(f"environment variable {self.api_key_env} is not set")
        else:
            key = "not-needed"  # servers such as Ollama ignore the key
        return openai.AsyncOpenAI(base_url=self.base_url, api_key=key, max_retries=0)
