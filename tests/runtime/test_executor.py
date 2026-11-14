"""Executor primitives used by batch nodes."""

import asyncio

from evalkit.runtime import Executor, Store


def test_named_lock_survives_a_new_event_loop(tmp_path):
    ex = Executor(Store(tmp_path))

    async def contend():
        lock = ex.named_lock("judge:x")

        async def hold():
            async with lock:
                await asyncio.sleep(0)

        await asyncio.gather(hold(), hold())  # contended: binds the lock to this loop

    asyncio.run(contend())
    asyncio.run(contend())  # a fresh loop gets its own lock instead of raising
