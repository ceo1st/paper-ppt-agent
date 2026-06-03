"""Helpers for applying an optional whole-job timeout."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def run_with_optional_job_timeout(awaitable: Awaitable[None], timeout: float | None) -> bool:
    """Run *awaitable* and return True only when the whole-job timeout elapsed.

    ``asyncio.wait_for`` cannot distinguish its own timeout from a
    ``TimeoutError`` raised by the awaited job. Waiting on an explicit task
    keeps internal timeout failures visible to the caller's normal exception
    path instead of reporting them as whole-job timeouts.
    """
    if not timeout or timeout <= 0:
        await awaitable
        return False

    task = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            await task
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
