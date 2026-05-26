from __future__ import annotations

import asyncio
import sys
import types
from enum import Enum

import pytest

from backend.orchestrator import agent_pipeline


class _WaitingClaudeClient:
    def __init__(self, *, emit_interrupt_result: bool = False) -> None:
        self.interrupted = asyncio.Event()
        self.interrupt_calls = 0
        self.emit_interrupt_result = emit_interrupt_result

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self.interrupted.set()

    async def receive_response(self):
        await self.interrupted.wait()
        if self.emit_interrupt_result:
            class ResultMessage:
                subtype = "error_during_execution"
                is_error = True
                result = "interrupted"

            yield ResultMessage()


@pytest.mark.asyncio
async def test_claude_live_feedback_interrupts_a_silent_response(tmp_path) -> None:
    session = agent_pipeline.PersistentAgentSession(tmp_path, "claude_code", {})
    emitted: list[object] = []
    session.on_message = emitted.append
    client = _WaitingClaudeClient(emit_interrupt_result=True)

    await session.send_message("continue now", interrupt_current=True)
    outcome = await asyncio.wait_for(
        session._receive_claude_response(client, interrupt_on_feedback=True),
        timeout=1,
    )

    assert client.interrupt_calls == 1
    assert outcome.feedback == "continue now"
    assert not outcome.interrupted
    assert not emitted


@pytest.mark.asyncio
async def test_claude_cancel_interrupts_a_silent_response(tmp_path) -> None:
    session = agent_pipeline.PersistentAgentSession(tmp_path, "claude_code", {})
    emitted: list[object] = []
    session.on_message = emitted.append
    client = _WaitingClaudeClient(emit_interrupt_result=True)
    response = asyncio.create_task(
        session._receive_claude_response(client, interrupt_on_feedback=True)
    )

    await asyncio.sleep(0)
    await session.close()
    await asyncio.wait_for(response, timeout=1)

    assert client.interrupt_calls == 1
    assert not emitted


@pytest.mark.asyncio
async def test_claude_pause_does_not_surface_interrupt_as_an_error(tmp_path) -> None:
    session = agent_pipeline.PersistentAgentSession(tmp_path, "claude_code", {})
    emitted: list[object] = []
    session.on_message = emitted.append
    client = _WaitingClaudeClient(emit_interrupt_result=True)
    response = asyncio.create_task(
        session._receive_claude_response(client, interrupt_on_feedback=True)
    )

    await asyncio.sleep(0)
    await session.interrupt()
    outcome = await asyncio.wait_for(response, timeout=1)

    assert client.interrupt_calls == 1
    assert outcome.interrupted
    assert not emitted


@pytest.mark.asyncio
async def test_codex_completed_turn_ends_initial_generation(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    class ReasoningEffort(str, Enum):
        high = "high"

    class _Turn:
        async def stream(self):
            if False:
                yield None

    class _Thread:
        id = "thread-1"

        async def turn(self, *args, **kwargs):
            calls.append("turn")
            return _Turn()

    class _Codex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def thread_start(self, **kwargs):
            return _Thread()

    openai_codex = types.ModuleType("openai_codex")
    openai_codex.AppServerConfig = lambda **kwargs: kwargs
    openai_codex.AsyncCodex = lambda **kwargs: _Codex()
    openai_codex.ApprovalMode = types.SimpleNamespace(auto_review="auto_review")
    openai_codex.SkillInput = lambda **kwargs: kwargs
    openai_codex.TextInput = lambda value: value
    codex_generated = types.ModuleType("openai_codex.generated")
    codex_v2 = types.ModuleType("openai_codex.generated.v2_all")
    codex_v2.DangerFullAccessSandboxPolicy = lambda **kwargs: kwargs
    codex_v2.ReasoningEffort = ReasoningEffort
    codex_v2.SandboxPolicy = lambda **kwargs: kwargs
    codex_types = types.ModuleType("openai_codex.types")
    codex_types.SandboxMode = types.SimpleNamespace(danger_full_access="danger_full_access")
    monkeypatch.setitem(sys.modules, "openai_codex", openai_codex)
    monkeypatch.setitem(sys.modules, "openai_codex.generated", codex_generated)
    monkeypatch.setitem(sys.modules, "openai_codex.generated.v2_all", codex_v2)
    monkeypatch.setitem(sys.modules, "openai_codex.types", codex_types)
    monkeypatch.setattr(agent_pipeline, "_install_codex_jsonrpc_noise_filter", lambda: None)

    messages: list[object] = []
    session = agent_pipeline.PersistentAgentSession(
        tmp_path,
        "codex",
        {"reasoning_effort": "high"},
    )
    session.on_message = messages.append
    session._prompt_queue.put("generate")

    await asyncio.wait_for(session._drive_codex(), timeout=1)

    assert calls == ["turn"]
    assert not session._active
    assert messages[-1] is agent_pipeline._SESSION_DONE
