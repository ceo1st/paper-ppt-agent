from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend.llm.provider_openai import OpenAIProvider
from backend.llm.types import LLMMessage


class _StructuredResult(BaseModel):
    name: str


class _RetryableError(RuntimeError):
    status_code = 500


class _AsyncStream:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCompletions:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _provider(completions: _FakeCompletions) -> OpenAIProvider:
    provider = object.__new__(OpenAIProvider)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    provider._api_key = "test"
    provider._provider_name = "openai"
    provider._base_url = ""
    provider._deepseek_settings = None
    provider._openai_settings = None
    provider._streaming_unsupported = False
    return provider


def _chunk(
    content: str | None = None,
    *,
    usage: SimpleNamespace | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    choices = []
    if content is not None or finish_reason is not None:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(
        id="resp_1",
        model="gpt-test",
        usage=usage,
        choices=choices,
    )


@pytest.mark.asyncio
async def test_response_format_uses_streaming_and_attaches_parsed() -> None:
    stream = _AsyncStream([
        _chunk('{"name":'),
        _chunk('"deck"}', finish_reason="stop"),
        _chunk(
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        ),
    ])
    completions = _FakeCompletions([stream])
    provider = _provider(completions)

    response = await provider.chat(
        [LLMMessage.user("return json")],
        "gpt-test",
        response_format=_StructuredResult,
    )

    assert response.content == '{"name":"deck"}'
    assert response.usage is not None
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 7
    assert response.raw.choices[0].message.parsed == _StructuredResult(name="deck")
    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["stream_options"] == {"include_usage": True}
    assert isinstance(completions.calls[0]["response_format"], dict)


@pytest.mark.asyncio
async def test_chat_stream_creation_uses_shared_retry() -> None:
    stream = _AsyncStream([
        _chunk("hello", finish_reason="stop"),
    ])
    completions = _FakeCompletions([
        _RetryableError("upstream unavailable"),
        stream,
    ])
    provider = _provider(completions)

    chunks = [
        chunk
        async for chunk in provider.chat_stream(
            [LLMMessage.user("hi")],
            "gpt-test",
        )
    ]

    assert [chunk.delta for chunk in chunks] == ["hello"]
    assert len(completions.calls) == 2
