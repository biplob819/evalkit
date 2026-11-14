"""In-memory stand-ins for the Anthropic and OpenAI SDK clients (no network, no spend)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any


def anthropic_message(
    text='{"label":"yes"}',
    *,
    input_tokens=600,
    output_tokens=300,
    thinking=100,
    cache_read=0,
    cache_write=0,
    stop="end_turn",
    model="claude-sonnet-5-5",
):
    return NS(
        id="msg_1",
        _request_id="req_1",
        model=model,
        stop_reason=stop,
        content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
        usage=NS(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
            output_tokens_details=NS(thinking_tokens=thinking),
        ),
    )


class _AsyncIter:
    def __init__(self, items: list[Any]):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class FakeAnthropic:
    def __init__(self, responses: list[Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [anthropic_message()])
        self.batch_status = "in_progress"
        self.batch_items: list[Any] = []
        self.created_batches: list[list[dict[str, Any]]] = []
        outer = self

        class Batches:
            async def create(self, requests):
                outer.created_batches.append(requests)
                return NS(id="msgbatch_1")

            async def retrieve(self, batch_id):
                return NS(id=batch_id, processing_status=outer.batch_status)

            async def results(self, batch_id):
                return _AsyncIter(outer.batch_items)

        class Messages:
            batches = Batches()

            async def create(self, **params):
                outer.calls.append(params)
                r = outer._responses.pop(0) if len(outer._responses) > 1 else outer._responses[0]
                if isinstance(r, BaseException):
                    raise r
                return r

        self.messages = Messages()


def openai_completion(
    text: str | None = '{"label":"no"}',
    *,
    prompt=600,
    completion=500,
    reasoning=300,
    cached=0,
    refusal=None,
    finish="stop",
    model="gpt-6.1-sol-2026-10-01",
):
    return {
        "id": "chatcmpl_1",
        "model": model,
        "choices": [{"message": {"content": text, "refusal": refusal}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


class FakeOpenAI:
    def __init__(self, responses: list[Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.uploads: list[bytes] = []
        self._responses = list(responses or [openai_completion()])
        self.batch_status = "in_progress"
        self.output_lines: list[dict[str, Any]] = []
        self.batch_args: tuple[str, str, str] | None = None
        outer = self

        class Completions:
            async def create(self, **params):
                outer.calls.append(params)
                r = outer._responses.pop(0) if len(outer._responses) > 1 else outer._responses[0]
                if isinstance(r, BaseException):
                    raise r
                return r

        class Files:
            async def create(self, file, purpose):
                assert purpose == "batch"
                outer.uploads.append(file[1])
                return NS(id="file_in")

            async def content(self, file_id):
                return NS(text="\n".join(json.dumps(x) for x in outer.output_lines))

        class Batches:
            async def create(self, input_file_id, endpoint, completion_window):
                outer.batch_args = (input_file_id, endpoint, completion_window)
                return NS(id="batch_1")

            async def retrieve(self, batch_id):
                return NS(
                    id=batch_id,
                    status=outer.batch_status,
                    output_file_id="file_out",
                    error_file_id=None,
                )

        self.chat = NS(completions=Completions())
        self.files = Files()
        self.batches = Batches()


class StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
