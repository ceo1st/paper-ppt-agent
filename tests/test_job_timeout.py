import asyncio

import pytest

from backend.runtime.job_timeout import run_with_optional_job_timeout


def test_internal_timeout_error_is_not_treated_as_job_timeout():
    async def raises_timeout() -> None:
        raise TimeoutError("inner agent timeout")

    with pytest.raises(TimeoutError, match="inner agent timeout"):
        asyncio.run(run_with_optional_job_timeout(raises_timeout(), timeout=30))


def test_whole_job_timeout_returns_true():
    async def hangs() -> None:
        await asyncio.sleep(10)

    assert asyncio.run(run_with_optional_job_timeout(hangs(), timeout=0.01)) is True


def test_disabled_job_timeout_waits_normally():
    completed = False

    async def finishes() -> None:
        nonlocal completed
        await asyncio.sleep(0)
        completed = True

    assert asyncio.run(run_with_optional_job_timeout(finishes(), timeout=None)) is False
    assert completed is True
