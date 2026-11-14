"""Single-writer JSONL output (RUN-06): many async producers, one task touching the file."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel

_STOP = object()


class JsonlWriter:
    """`async with JsonlWriter(path) as w: await w.write(obj)`; lines are whole and in order
    of arrival. Producers never open the file, so concurrent writes cannot interleave."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.lines_written = 0

    async def __aenter__(self) -> JsonlWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain())
        return self

    async def write(self, obj: BaseModel | dict[str, Any]) -> None:
        if self._task is None or self._closed:
            raise RuntimeError("JsonlWriter used outside `async with`")
        line = (
            obj.model_dump_json()
            if isinstance(obj, BaseModel)
            else json.dumps(obj, allow_nan=False, ensure_ascii=False)
        )
        if "\n" in line:
            raise ValueError("a JSONL record cannot contain a raw newline")
        await self._queue.put(line)

    async def _drain(self) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            while True:
                line = await self._queue.get()
                if line is _STOP:
                    break
                fh.write(line + "\n")
                fh.flush()
                self.lines_written += 1

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._closed = True
        await self._queue.put(_STOP)
        if self._task is not None:
            await self._task
