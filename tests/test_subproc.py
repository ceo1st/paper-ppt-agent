from __future__ import annotations

import asyncio
import sys

import pytest

from backend.runtime.subproc import SubprocessError, arun


@pytest.mark.asyncio
async def test_arun_falls_back_when_asyncio_subprocess_is_unavailable(monkeypatch) -> None:
    async def unsupported_subprocess(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsupported_subprocess)

    result = await arun(
        [sys.executable, "-c", "print('fallback ok')"],
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "fallback ok"


@pytest.mark.asyncio
async def test_arun_threaded_fallback_preserves_check(monkeypatch) -> None:
    async def unsupported_subprocess(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsupported_subprocess)

    with pytest.raises(SubprocessError) as exc:
        await arun(
            [sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"],
            timeout=10,
        )

    assert exc.value.returncode == 7
    assert exc.value.stdout.strip() == "bad"
