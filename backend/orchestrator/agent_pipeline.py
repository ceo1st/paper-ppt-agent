"""Independent Claude Code / Codex powered PPT generation pipeline.

This module intentionally does not use ``backend.llm.create_provider``.  Agent
generation is a separate orchestration path: the backend prepares a workspace
and an output contract, starts the selected coding agent runtime, streams any
SVG artifacts the agent writes, then reuses deterministic finalize/export code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal
from xml.etree import ElementTree as ET

from backend.config import CANVAS_FORMATS, PROJECT_ROOT, settings
from backend.generator.project_manager import get_notes, get_svg_files, init_project
from backend.generator.svg_finalize import finalize_project
from backend.generator.svg_to_pptx import create_pptx
from backend.generator.template_agent import _events_from_sdk_message
from backend.orchestrator.manuscript import auto_slide_range, page_type_budget, page_type_budget_guidance
from backend.runtime import aoffload, awrite_text
from backend.runtime.resource_gates import heavy_stage_slot

logger = logging.getLogger(__name__)


@dataclass
class AgentProgressEvent:
    stage: str
    status: Literal["started", "progress", "complete", "error"]
    message: str = ""
    progress: float = 0.0
    data: dict | None = None


@dataclass
class _AgentUsageTotals:
    runtime: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    seen_message_ids: set[str] | None = None
    seen_result_ids: set[str] | None = None
    pending_input_tokens: int = 0
    pending_output_tokens: int = 0
    pending_cache_read_input_tokens: int = 0
    pending_cache_creation_input_tokens: int = 0


@dataclass
class _ClaudeResponseOutcome:
    feedback: str | None = None
    hit_turn_limit: bool = False
    interrupted: bool = False


_RUNTIME_DEFAULT = "claude_code"
_AGENT_SKILL_NAME = "paper-ppt-generate"
_SVG_SCAN_SECONDS = 1.0

# Sentinel marking end of the persistent session message stream.
_SESSION_DONE = object()


async def agent_runtime_status(runtime: str) -> dict[str, Any]:
    """Return whether an Agent runtime is usable before a job is queued."""
    runtime = runtime or _RUNTIME_DEFAULT
    if runtime == "claude_code":
        from backend.generator.template_agent import claude_code_environment_status

        return {"runtime": runtime, **claude_code_environment_status()}
    if runtime != "codex":
        return {
            "runtime": runtime,
            "available": False,
            "message": f"Unsupported Agent runtime: {runtime}",
        }

    try:
        from openai_codex import AppServerConfig, AsyncCodex
    except ImportError as exc:
        return {
            "runtime": runtime,
            "available": False,
            "sdk_available": False,
            "message": (
                "Codex Agent runtime requires the `openai-codex` Python SDK. "
                "Run `uv sync` after updating dependencies, then start the backend again."
            ),
            "error": str(exc) or exc.__class__.__name__,
        }

    try:
        codex_config = AppServerConfig(
            cwd=str(PROJECT_ROOT),
            env={},
            codex_bin=str(settings.codex_bin) if settings.codex_bin else None,
        )
        async with AsyncCodex(config=codex_config) as codex:
            account = await codex.account(refresh_token=False)
            account_obj = getattr(account, "account", None)
            if account_obj is None:
                return {
                    "runtime": runtime,
                    "available": False,
                    "sdk_available": True,
                    "authenticated": False,
                    "message": (
                        "Codex is not logged in. Sign in with the Codex CLI, or start a "
                        "ChatGPT OAuth/device-code login through the Codex SDK before running Agent mode."
                    ),
                    "supports_chatgpt_oauth": True,
                }
            return {
                "runtime": runtime,
                "available": True,
                "sdk_available": True,
                "authenticated": True,
                "requires_openai_auth": bool(getattr(account, "requires_openai_auth", False)),
                "message": "Codex SDK is available and authenticated.",
            }
    except Exception as exc:  # noqa: BLE001 - surface runtime/auth issues
        return {
            "runtime": runtime,
            "available": False,
            "sdk_available": True,
            "authenticated": False,
            "message": f"Codex runtime is not ready: {exc}",
            "error": str(exc) or exc.__class__.__name__,
            "supports_chatgpt_oauth": True,
        }


class PersistentAgentSession:
    """Keeps a Claude Code or Codex agent client alive across multiple
    user interactions within a single generation job.

    The session broadcasts every SDK message through an ``on_message``
    callback.  During initial generation the callback pushes to a bridge
    queue so the pipeline generator can yield events.  After the pipeline
    returns the callback is switched to ``session_manager.record_event``
    so feedback turns are broadcast directly to WebSocket subscribers.
    """

    def __init__(
        self,
        project_dir: Path,
        runtime: str,
        agent_config: dict[str, Any],
        *,
        resume_session_id: str | None = None,
        job_id: str = "",
    ) -> None:
        self._project_dir = project_dir
        self._runtime = runtime
        self._agent_config = agent_config
        self._resume_session_id = resume_session_id
        self._job_id = job_id

        # Cross-thread prompt injection (main thread -> worker thread).
        self._prompt_queue: queue.Queue[str | None] = queue.Queue()
        # Message bridge (worker thread -> caller async loop).
        self._bridge: asyncio.Queue[Any] = asyncio.Queue(maxsize=128)
        self._cancel_event = threading.Event()
        self._interrupt_event = threading.Event()
        self._worker_loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
        self._ready = threading.Event()
        self._session_id: str | None = resume_session_id
        self._active = True
        self._worker_thread: threading.Thread | None = None
        self._caller_loop: asyncio.AbstractEventLoop | None = None
        # Callback invoked for every SDK message.  Can be swapped at
        # runtime (e.g. from bridge-queue mode to direct-record mode).
        self.on_message: Callable[[Any], None] | None = None
        # Usage tracking
        self._usage_totals = _AgentUsageTotals(runtime=runtime)
        configured_model = agent_config.get("model")
        if isinstance(configured_model, str) and configured_model.strip():
            self._usage_totals.model = configured_model.strip()

    # -- Public API (called from the main async loop) ----------------------

    async def start(self, initial_prompt: str) -> None:
        """Launch the worker thread and send the first prompt."""
        self._caller_loop = asyncio.get_running_loop()
        self._worker_thread = threading.Thread(
            target=self._worker,
            name=f"agent-session-{self._runtime}",
            daemon=True,
        )
        self._worker_thread.start()
        self._ready.wait(timeout=30)
        if self._usage_totals.model:
            self._emit(_agent_usage_progress_event(self._usage_totals))
        await self.send_message(initial_prompt)

    async def send_message(self, prompt: str) -> None:
        """Inject a new prompt.  Safe to call from any async context."""
        self._interrupt_event.clear()
        self._prompt_queue.put(prompt)

    async def interrupt(self) -> None:
        """Request a soft interruption of the current agent turn.

        Unlike ``close()``, this keeps the SDK session alive so a follow-up
        prompt can continue the conversation.
        """
        self._interrupt_event.set()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self._session_id = value

    def __aiter__(self) -> PersistentAgentSession:
        return self

    async def __anext__(self) -> Any:
        while True:
            item = await self._bridge.get()
            if item is _SESSION_DONE:
                raise StopAsyncIteration
            if isinstance(item, BaseException):
                raise item
            return item

    async def close(self) -> None:
        """Signal the worker to stop and wait for it to finish."""
        self._active = False
        self._cancel_event.set()
        self._prompt_queue.put(None)  # unblock the worker
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)

    def set_bridge_mode(self) -> None:
        """Route messages to the bridge queue (for pipeline generator)."""
        self.on_message = None  # default: use _put_threadsafe -> bridge

    def set_direct_mode(self) -> None:
        """Route messages directly via session_manager.record_event."""
        from backend.session.progress import payloads_from_progress_event

        def _record(message: Any) -> None:
            from backend.api.endpoints.generate import session_manager

            job = session_manager.get_job(self._job_id)
            if job is None:
                return
            if isinstance(message, AgentProgressEvent):
                for payload, updates in payloads_from_progress_event(
                    self._job_id, job, message
                ):
                    session_manager.record_event(self._job_id, payload, **updates)
                return
            usage_event = _update_agent_usage_totals(self._usage_totals, message)
            if usage_event is not None:
                for payload, updates in payloads_from_progress_event(
                    self._job_id, job, usage_event
                ):
                    session_manager.record_event(self._job_id, payload, **updates)
            # Also record agent events
            if self._runtime == "codex":
                item_event = _codex_item_progress_event(message, str(getattr(message, "method", "") or ""))
                if item_event is not None:
                    for payload, updates in payloads_from_progress_event(
                        self._job_id, job, item_event
                    ):
                        session_manager.record_event(self._job_id, payload, **updates)
            else:
                for sdk_event in _agent_sdk_events_from_message(message):
                    msg = str(sdk_event.get("message") or "").strip()
                    if msg:
                        evt = AgentProgressEvent(
                            "agent", "progress", msg, 0.12,
                            data={"runtime": "claude_code", "agent_event": sdk_event},
                        )
                        for payload, updates in payloads_from_progress_event(
                            self._job_id, job, evt
                        ):
                            session_manager.record_event(
                                self._job_id, payload, **updates
                            )

        self.on_message = _record

    # -- Internal worker thread --------------------------------------------

    def _emit(self, message: Any) -> None:
        """Route a message through the callback or bridge queue."""
        if self.on_message is not None:
            try:
                self.on_message(message)
            except Exception:
                pass
        else:
            # Default: push to bridge queue for pipeline generator.
            loop = self._caller_loop
            if loop is None or loop.is_closed():
                return

            async def _put() -> None:
                await self._bridge.put(message)

            try:
                asyncio.run_coroutine_threadsafe(_put(), loop).result()
            except RuntimeError:
                pass

    def _worker(self) -> None:
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_loop_holder["loop"] = loop
        self._ready.set()
        try:
            loop.run_until_complete(self._drive())
        finally:
            loop.close()

    async def _drive(self) -> None:
        if self._runtime == "codex":
            await self._drive_codex()
        else:
            await self._drive_claude()

    async def _drive_claude(self) -> None:
        # Let Claude run to natural completion by default. Live feedback is
        # still picked up after assistant/tool-result messages and interrupts
        # the current response at a safe boundary.
        try:
            from claude_agent_sdk import ClaudeSDKClient

            options = self._build_claude_options()
            async with ClaudeSDKClient(options=options) as client:
                first_prompt: str | None = None
                # Wait for the initial prompt.
                while self._active:
                    try:
                        first_prompt = self._prompt_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    break
                if not first_prompt or self._cancel_event.is_set():
                    return

                # -- First chunk: process the initial prompt.
                await client.query(first_prompt)
                outcome = await self._receive_claude_response(
                    client,
                    interrupt_on_feedback=True,
                )
                if self._cancel_event.is_set():
                    return
                live_feedback = outcome.feedback
                continue_work = outcome.hit_turn_limit
                paused = outcome.interrupted
                if paused:
                    self._emit(_agent_paused_event())

                # -- Continuation / live feedback loop --------------------
                while self._active:
                    if paused:
                        user_msg = await self._async_wait_for_user_message(timeout=1.0)
                        if self._cancel_event.is_set():
                            return
                        if user_msg is None:
                            continue
                        await client.query(_live_feedback_prompt(user_msg))
                        outcome = await self._receive_claude_response(
                            client,
                            interrupt_on_feedback=True,
                        )
                        if self._cancel_event.is_set():
                            return
                        live_feedback = outcome.feedback
                        continue_work = outcome.hit_turn_limit
                        paused = outcome.interrupted
                        if paused:
                            self._emit(_agent_paused_event())
                        continue

                    if live_feedback is not None:
                        await client.query(_live_feedback_prompt(live_feedback))
                        outcome = await self._receive_claude_response(
                            client,
                            interrupt_on_feedback=True,
                        )
                        if self._cancel_event.is_set():
                            return
                        live_feedback = outcome.feedback
                        continue_work = outcome.hit_turn_limit
                        paused = outcome.interrupted
                        if paused:
                            self._emit(_agent_paused_event())
                        continue

                    # 1. Check for queued user messages.
                    user_msg = self._drain_user_message()
                    if user_msg is not None:
                        # User sent feedback — process it as a new turn.
                        await client.query(_live_feedback_prompt(user_msg))
                        outcome = await self._receive_claude_response(
                            client,
                            interrupt_on_feedback=True,
                        )
                        if self._cancel_event.is_set():
                            return
                        live_feedback = outcome.feedback
                        continue_work = outcome.hit_turn_limit
                        paused = outcome.interrupted
                        if paused:
                            self._emit(_agent_paused_event())
                        continue

                    if not continue_work:
                        self._active = False
                        break

                    # 2. Claude hit a configured turn limit — send continuation prompt.
                    await client.query(
                        "Continue your work from where you left off. "
                        "Do not repeat completed steps. "
                        "After each major milestone (e.g. completing a slide), "
                        "briefly note what you just finished."
                    )
                    outcome = await self._receive_claude_response(
                        client,
                        interrupt_on_feedback=True,
                    )
                    if self._cancel_event.is_set():
                        return
                    live_feedback = outcome.feedback
                    continue_work = outcome.hit_turn_limit
                    paused = outcome.interrupted
                    if paused:
                        self._emit(_agent_paused_event())

        except BaseException as exc:
            if _is_false_success_error(exc):
                return
            self._emit(exc)
        finally:
            self._emit(_SESSION_DONE)

    async def _receive_claude_response(
        self,
        client: Any,
        *,
        interrupt_on_feedback: bool = False,
    ) -> _ClaudeResponseOutcome:
        """Drain one Claude response, optionally cutting it short for feedback.

        Claude's SDK requires the current response stream to be drained after
        ``interrupt()`` before a new ``query()`` can be sent.  We therefore
        interrupt as soon as feedback is observed after an emitted SDK message,
        keep draining the interrupted ResultMessage, and return the latest
        queued feedback for the caller to submit as the next turn.
        """
        pending_feedback: str | None = None
        interrupt_sent = False
        hit_turn_limit = False
        soft_interrupted = False
        async for message in client.receive_response():
            if self._cancel_event.is_set():
                return _ClaudeResponseOutcome(feedback=pending_feedback)

            if self._interrupt_event.is_set() and not interrupt_sent:
                pending_feedback = self._drain_user_message() or pending_feedback
                try:
                    await client.interrupt()
                    interrupt_sent = True
                    soft_interrupted = True
                    self._interrupt_event.clear()
                except Exception:
                    logger.exception("Failed to interrupt Claude session")

            self._capture_session_id(message)

            if pending_feedback is not None and _is_feedback_interrupt_result(message):
                continue

            self._emit(message)
            if _is_claude_turn_limit_result(message):
                hit_turn_limit = True

            if not interrupt_on_feedback or pending_feedback is not None:
                continue

            if not _claude_message_allows_live_feedback_interrupt(message):
                continue

            queued_feedback = self._drain_user_message()
            if queued_feedback is None:
                continue

            pending_feedback = queued_feedback
            if message.__class__.__name__ == "ResultMessage":
                continue
            if not interrupt_sent:
                try:
                    await client.interrupt()
                    interrupt_sent = True
                    soft_interrupted = True
                except Exception:
                    logger.exception("Failed to interrupt Claude session for live feedback")

        return _ClaudeResponseOutcome(
            feedback=pending_feedback,
            hit_turn_limit=hit_turn_limit and pending_feedback is None,
            interrupted=soft_interrupted and pending_feedback is None,
        )

    def _drain_user_message(self) -> str | None:
        """Return the most recent queued user message, or None."""
        msg: str | None = None
        while True:
            try:
                candidate = self._prompt_queue.get_nowait()
            except queue.Empty:
                break
            if candidate is None:
                self._active = False
                return None
            msg = candidate  # keep the latest
        return msg

    async def _async_wait_for_user_message(self, timeout: float) -> str | None:
        """Async wait up to *timeout* seconds for a user message."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while self._active:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            # Use run_in_executor to do a blocking queue.get without
            # blocking the event loop.
            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, self._prompt_queue.get, True, min(remaining, 1.0)
                )
            except queue.Empty:
                continue
            if msg is None:
                self._active = False
                return None
            return msg
        return None

    async def _drive_codex(self) -> None:
        try:
            try:
                from openai_codex import (
                    AppServerConfig,
                    AsyncCodex,
                    ApprovalMode,
                    SkillInput,
                    TextInput,
                )
                from openai_codex.generated.v2_all import (
                    DangerFullAccessSandboxPolicy,
                    ReasoningEffort,
                    SandboxPolicy,
                )
                from openai_codex.types import SandboxMode
            except ImportError as exc:
                raise RuntimeError(
                    "Codex Agent runtime requires the `openai-codex` Python SDK. "
                    "Run `uv sync` after updating dependencies, then start the backend again."
                ) from exc

            effort_value = str(self._agent_config.get("reasoning_effort") or "high")
            try:
                effort = ReasoningEffort(effort_value)
            except ValueError:
                effort = ReasoningEffort.high
            model = self._agent_config.get("model") or None
            self._usage_totals.model = model or "codex-default"
            self._emit(_agent_usage_progress_event(self._usage_totals))
            skill_path = str(
                (self._project_dir / "skills" / _AGENT_SKILL_NAME).resolve()
            )
            codex_config = AppServerConfig(
                cwd=str(self._project_dir),
                env=_agent_runtime_env(self._project_dir),
                codex_bin=str(settings.codex_bin) if settings.codex_bin else None,
            )
            sandbox_policy = SandboxPolicy(
                root=DangerFullAccessSandboxPolicy(type="dangerFullAccess")
            )
            async with AsyncCodex(config=codex_config) as codex:
                thread_kwargs = {
                    "approval_mode": ApprovalMode.auto_review,
                    "cwd": str(self._project_dir),
                    "model": model,
                    "sandbox": SandboxMode.danger_full_access,
                    "config": {"model_reasoning_effort": effort.value},
                }
                if self._resume_session_id:
                    thread = await codex.thread_resume(self._resume_session_id, **thread_kwargs)
                    self._resume_session_id = None
                else:
                    thread = await codex.thread_start(**thread_kwargs)
                self._session_id = thread.id
                turn_active = False
                while self._active:
                    try:
                        prompt = self._prompt_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if prompt is None:
                        break
                    if self._cancel_event.is_set():
                        break
                    if turn_active:
                        # Inject into running turn.
                        await turn.steer(TextInput(prompt))
                        continue
                    # Start a new turn.
                    turn = await thread.turn(
                        [SkillInput(name=_AGENT_SKILL_NAME, path=skill_path), TextInput(prompt)],
                        approval_mode=ApprovalMode.auto_review,
                        cwd=str(self._project_dir),
                        effort=effort,
                        model=model,
                        sandbox_policy=sandbox_policy,
                    )
                    turn_active = True
                    async for event in turn.stream():
                        if self._cancel_event.is_set():
                            await turn.interrupt()
                            break
                        if self._interrupt_event.is_set():
                            self._interrupt_event.clear()
                            await turn.interrupt()
                            self._emit(_agent_paused_event())
                            continue
                        self._emit(event)
                        queued_feedback = self._drain_user_message()
                        if queued_feedback is not None:
                            await turn.steer(TextInput(queued_feedback))
                    turn_active = False
        except BaseException as exc:
            self._emit(exc)
        finally:
            self._emit(_SESSION_DONE)

    # -- Helpers -----------------------------------------------------------

    def _build_claude_options(self, max_turns: int | None = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        agent_config = self._agent_config
        allowed_tools = [
            "Read", "Write", "Edit", "MultiEdit", "Grep", "Glob",
            "LS", "Bash", "TodoWrite", "Task", "Agent",
        ]
        session_opts: dict[str, Any] = {}
        if self._resume_session_id:
            session_opts["resume"] = self._resume_session_id
            # Only use resume for the first query; subsequent queries
            # are in the same client session.
            self._resume_session_id = None
        effective_max_turns = max_turns if max_turns is not None else (
            int(agent_config["max_turns"])
            if agent_config.get("max_turns")
            else None
        )
        return ClaudeAgentOptions(
            tools={"type": "preset", "preset": "claude_code"},
            system_prompt={"type": "preset", "preset": "claude_code"},
            cwd=str(self._project_dir),
            add_dirs=[
                str(self._project_dir),
                str(settings.icons_dir),
                str(settings.templates_dir),
            ],
            allowed_tools=allowed_tools,
            disallowed_tools=["Bash(rm *)", "Bash(del *)", "Bash(Remove-Item *)"],
            permission_mode="bypassPermissions",
            include_partial_messages=False,
            max_buffer_size=16 * 1024 * 1024,
            max_turns=effective_max_turns,
            setting_sources=(
                ["user", "project", "local"]
                if agent_config.get("load_project_settings", True)
                else ["user"]
            ),
            model=agent_config.get("model") or None,
            skills=[_AGENT_SKILL_NAME],
            env=_agent_runtime_env(self._project_dir),
            **session_opts,
        )

    def _capture_session_id(self, message: Any) -> None:
        kind = message.__class__.__name__
        if kind == "SystemMessage":
            subtype = getattr(message, "subtype", None)
            if subtype == "init":
                data = getattr(message, "data", None) or {}
                sid = data.get("session_id")
                if isinstance(sid, str) and sid:
                    self._session_id = sid


def _is_false_success_error(exc: BaseException) -> bool:
    """Detect the benign ``[WinError 6]`` that the Claude CLI raises on
    Windows when the subprocess handle is closed after a successful run."""
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 6:
        return True
    return False


def _is_claude_turn_limit_result(message: Any) -> bool:
    if message.__class__.__name__ != "ResultMessage":
        return False
    if not bool(getattr(message, "is_error", False)):
        return False
    subtype = str(getattr(message, "subtype", "") or "").lower()
    result = str(getattr(message, "result", "") or "").lower()
    return subtype == "error_max_turns" or "maximum number of turns" in result


def _agent_sdk_events_from_message(message: Any) -> list[dict[str, Any]]:
    events = _events_from_sdk_message(message)
    if not _is_claude_turn_limit_result(message):
        return events
    for event in events:
        if event.get("type") != "result":
            continue
        event["status"] = "running"
        event["message"] = "Agent reached the current turn limit; continuing..."
        data = event.get("data")
        if isinstance(data, dict):
            data["recoverable"] = True
    return events


def _is_feedback_interrupt_result(message: Any) -> bool:
    """Return True for the expected ResultMessage emitted after live feedback
    interrupts a running Claude Code turn."""
    if message.__class__.__name__ != "ResultMessage":
        return False
    subtype = str(getattr(message, "subtype", "") or "").lower()
    if subtype == "error_during_execution":
        return True
    result = str(getattr(message, "result", "") or "").lower()
    return "interrupt" in result or "cancel" in result


def _claude_message_allows_live_feedback_interrupt(message: Any) -> bool:
    """Only inject feedback at natural boundaries.

    Tool-use AssistantMessages mean a tool call is just starting; wait for the
    following tool-result UserMessage so feedback lands after the current tool
    batch and before Claude starts the next reasoning step.
    """
    kind = message.__class__.__name__
    if kind == "ResultMessage":
        return True
    if kind == "UserMessage":
        return any(
            block.__class__.__name__ == "ToolResultBlock"
            or hasattr(block, "tool_use_id")
            for block in (getattr(message, "content", []) or [])
        )
    if kind != "AssistantMessage":
        return False
    content = getattr(message, "content", []) or []
    has_tool_use = any(hasattr(block, "name") for block in content)
    has_text = any(str(getattr(block, "text", "") or "").strip() for block in content)
    return has_text and not has_tool_use


def _live_feedback_prompt(feedback: str) -> str:
    return (
        "The user sent live guidance while you were working.\n\n"
        "First, briefly reply to the user that you received it and summarize "
        "how you will apply it. Then continue the PPT generation task from "
        "the current workspace state without restarting completed work.\n\n"
        f"## Live user guidance\n{feedback.strip()}"
    )


# ---------------------------------------------------------------------------
# Session persistence (agent_session.json)
# ---------------------------------------------------------------------------

def _agent_session_path(project_dir: Path) -> Path:
    return project_dir / "agent_session.json"


def _save_agent_session(project_dir: Path, session_id: str) -> None:
    """Persist the SDK session id so post-completion feedback can resume."""
    path = _agent_session_path(project_dir)
    payload = {"session_id": session_id}
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("Failed to persist agent session id to %s", path)


def _load_agent_session_id(project_dir: Path) -> str | None:
    """Load a previously persisted SDK session id for resume."""
    path = _agent_session_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sid = data.get("session_id")
        return str(sid) if isinstance(sid, str) and sid else None
    except (OSError, json.JSONDecodeError):
        return None


async def run_agent_pipeline(request: Any):
    """Run the Agent-mode generation path.

    The selected runtime owns paper understanding, research, planning, and SVG
    authoring.  The backend owns workspace setup, artifact validation, live
    preview events, finalization, and PPTX export.
    """

    from backend.generator.template_manager import (
        copy_template_assets_for_project,
        load_template,
    )

    agent_config = dict(getattr(request, "agent_config", None) or {})
    runtime = str(agent_config.get("runtime") or _RUNTIME_DEFAULT)
    project_dir = init_project(
        name="paper_ppt_agent",
        canvas_format=request.canvas_format,
        base_dir=settings.workspaces_dir,
    )
    yield AgentProgressEvent(
        "agent",
        "started",
        "Preparing Agent workspace...",
        0.02,
        data={"project_dir": str(project_dir), "runtime": runtime},
    )

    await _prepare_agent_workspace(project_dir, request, agent_config)

    if getattr(request, "template_id", None):
        tmpl = load_template(request.template_id)
        if tmpl:
            _copy_template_reference_for_agent(tmpl, project_dir)
            copy_template_assets_for_project(tmpl, project_dir)
            yield AgentProgressEvent(
                "agent",
                "progress",
                f"Template '{request.template_id}' copied into Agent workspace.",
                0.05,
                data={"template_id": request.template_id},
            )

    total_slides_hint = int(getattr(request, "num_pages", None) or 0)
    yield AgentProgressEvent(
        "generation",
        "started",
        "Agent is generating the deck...",
        0.10,
        data={
            "total_slides": total_slides_hint,
            "generation_mode": "agent",
            "runtime": runtime,
        },
    )

    queue: asyncio.Queue[AgentProgressEvent] = asyncio.Queue()
    stop_scan = asyncio.Event()
    seen_hashes: dict[Path, str] = {}
    completed_pages: set[int] = set()

    async def _scanner() -> None:
        while not stop_scan.is_set():
            await _scan_svg_updates(
                project_dir,
                queue,
                seen_hashes,
                completed_pages,
                total_slides_hint=total_slides_hint,
            )
            try:
                await asyncio.wait_for(stop_scan.wait(), timeout=_SVG_SCAN_SECONDS)
            except asyncio.TimeoutError:
                continue
        await _scan_svg_updates(
            project_dir,
            queue,
            seen_hashes,
            completed_pages,
            total_slides_hint=total_slides_hint,
        )

    # -- Persistent session (replaces one-shot runner_task) --
    job_id = str(getattr(request, "job_id", None) or "")
    resume_session_id = _load_agent_session_id(project_dir)
    session = PersistentAgentSession(
        project_dir,
        runtime,
        agent_config,
        resume_session_id=resume_session_id,
        job_id=job_id,
    )
    if job_id:
        from backend.orchestrator.agent_session_registry import register as registry_register
        registry_register(job_id, session)

    async def _cleanup_live_session() -> None:
        if job_id:
            from backend.orchestrator.agent_session_registry import unregister as registry_unregister
            registry_unregister(job_id)
        await session.close()

    prompt = _build_runtime_prompt(project_dir, request, agent_config)

    # Phase 1: Bridge mode — session emits to bridge queue, drainer
    # converts messages to AgentProgressEvent and pushes to event queue.
    session.set_bridge_mode()

    async def _session_drainer() -> None:
        await session.start(prompt)
        async for message in session:
            if isinstance(message, AgentProgressEvent):
                await queue.put(message)
                continue
            usage_event = _update_agent_usage_totals(session._usage_totals, message)
            if usage_event is not None:
                await queue.put(usage_event)
            if runtime == "codex":
                item_event = _codex_item_progress_event(message, str(getattr(message, "method", "") or ""))
                if item_event is not None:
                    await queue.put(item_event)
            else:
                for sdk_event in _agent_sdk_events_from_message(message):
                    msg = str(sdk_event.get("message") or "").strip()
                    if msg:
                        await queue.put(
                            AgentProgressEvent(
                                "agent",
                                "progress",
                                msg,
                                0.12,
                                data={"runtime": "claude_code", "agent_event": sdk_event},
                            )
                        )
                if message.__class__.__name__ == "ResultMessage" and bool(
                    getattr(message, "is_error", False)
                ):
                    if _is_claude_turn_limit_result(message):
                        continue
                    result = str(getattr(message, "result", "") or "Agent failed.")
                    raise RuntimeError(result)
        # Drainer finished — persist session_id and switch to direct mode
        # so feedback turns broadcast directly to WebSocket subscribers.
        if session.session_id:
            _save_agent_session(project_dir, session.session_id)
        session.set_direct_mode()

    scanner_task = asyncio.create_task(_scanner(), name="agent-svg-scanner")
    drainer_task = asyncio.create_task(_session_drainer(), name=f"agent-session-{runtime}")

    # Yield events from the drainer until it finishes the first response.
    try:
        while True:
            if drainer_task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield event
    except asyncio.CancelledError:
        await _cleanup_live_session()
        drainer_task.cancel()
        raise
    finally:
        stop_scan.set()
        scanner_task.cancel()
        await asyncio.gather(scanner_task, return_exceptions=True)

    try:
        await drainer_task

        # Session is now in direct mode — feedback turns broadcast via
        # session_manager.record_event directly.  Proceed to finalization.

        await _scan_svg_updates(
            project_dir,
            queue,
            seen_hashes,
            completed_pages,
            total_slides_hint=total_slides_hint,
        )
        while not queue.empty():
            yield queue.get_nowait()

        svg_files = get_svg_files(project_dir, source="output")
        _validate_agent_artifacts(project_dir, svg_files, total_slides_hint)
        generated = len(svg_files)
        yield AgentProgressEvent(
            "generation",
            "complete",
            f"Agent generated {generated} slides.",
            0.75,
            data={"total_slides": generated, "generation_mode": "agent"},
        )

        yield AgentProgressEvent("postprocess", "started", "Finalizing SVGs...", 0.78)
        async with heavy_stage_slot():
            stats = await aoffload(finalize_project, project_dir)
        yield AgentProgressEvent(
            "postprocess",
            "complete",
            f"Processed {stats.get('total_files', 0)} files",
            0.88,
        )

        yield AgentProgressEvent("export", "started", "Exporting to PowerPoint...", 0.90)
        final_svg_files = get_svg_files(project_dir, source="final")
        notes = get_notes(project_dir, final_svg_files)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pptx_path = project_dir / "exports" / f"presentation_{timestamp}.pptx"
        async with heavy_stage_slot():
            await aoffload(
                create_pptx,
                final_svg_files,
                pptx_path,
                canvas_format=request.canvas_format,
                notes=notes,
            )

        yield AgentProgressEvent(
            "export",
            "complete",
            "PowerPoint generated!",
            1.0,
            data={"output_path": str(pptx_path)},
        )
    finally:
        await _cleanup_live_session()


async def run_agent_feedback_pipeline(
    *,
    project_dir: Path,
    feedback: str,
    runtime: str,
    total_slides_hint: int = 0,
    session_id: str | None = None,
    job_id: str = "",
) -> Any:
    """Continue an Agent-mode job in the same workspace using user feedback."""

    yield AgentProgressEvent(
        "agent",
        "started",
        "Agent is applying feedback...",
        0.05,
        data={"project_dir": str(project_dir), "runtime": runtime},
    )

    queue: asyncio.Queue[AgentProgressEvent] = asyncio.Queue()
    stop_scan = asyncio.Event()
    seen_hashes: dict[Path, str] = {}
    completed_pages: set[int] = set()
    total_hint = max(total_slides_hint, len(get_svg_files(project_dir, source="output")))

    async def _scanner() -> None:
        while not stop_scan.is_set():
            await _scan_svg_updates(
                project_dir,
                queue,
                seen_hashes,
                completed_pages,
                total_slides_hint=total_hint,
            )
            try:
                await asyncio.wait_for(stop_scan.wait(), timeout=_SVG_SCAN_SECONDS)
            except asyncio.TimeoutError:
                continue
        await _scan_svg_updates(
            project_dir,
            queue,
            seen_hashes,
            completed_pages,
            total_slides_hint=total_hint,
        )

    agent_config = _agent_config_from_existing_task(project_dir, runtime)
    # Use persistent session with resume for context continuity.
    effective_session_id = session_id or _load_agent_session_id(project_dir)
    session = PersistentAgentSession(
        project_dir,
        runtime,
        agent_config,
        resume_session_id=effective_session_id,
    )
    if job_id:
        from backend.orchestrator.agent_session_registry import register as registry_register
        registry_register(job_id, session)

    usage_totals = _AgentUsageTotals(runtime=runtime)
    prompt = _build_feedback_runtime_prompt(project_dir, feedback)

    async def _session_drainer() -> None:
        await session.start(prompt)
        async for message in session:
            if isinstance(message, AgentProgressEvent):
                await queue.put(message)
                continue
            usage_event = _update_agent_usage_totals(usage_totals, message)
            if usage_event is not None:
                await queue.put(usage_event)
            if runtime == "codex":
                item_event = _codex_item_progress_event(message, str(getattr(message, "method", "") or ""))
                if item_event is not None:
                    await queue.put(item_event)
            else:
                for sdk_event in _agent_sdk_events_from_message(message):
                    msg = str(sdk_event.get("message") or "").strip()
                    if msg:
                        await queue.put(
                            AgentProgressEvent(
                                "agent",
                                "progress",
                                msg,
                                0.12,
                                data={"runtime": "claude_code", "agent_event": sdk_event},
                            )
                        )
                if message.__class__.__name__ == "ResultMessage" and bool(
                    getattr(message, "is_error", False)
                ):
                    if _is_claude_turn_limit_result(message):
                        continue
                    result = str(getattr(message, "result", "") or "Agent failed.")
                    raise RuntimeError(result)
        if session.session_id:
            _save_agent_session(project_dir, session.session_id)

    scanner_task = asyncio.create_task(_scanner(), name="agent-feedback-svg-scanner")
    drainer_task = asyncio.create_task(_session_drainer(), name=f"agent-feedback-{runtime}")

    try:
        while True:
            if drainer_task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield event
        await drainer_task
    except asyncio.CancelledError:
        drainer_task.cancel()
        raise
    finally:
        stop_scan.set()
        scanner_task.cancel()
        await asyncio.gather(scanner_task, return_exceptions=True)
        if job_id:
            from backend.orchestrator.agent_session_registry import unregister as registry_unregister
            registry_unregister(job_id)
        await session.close()

    await _scan_svg_updates(
        project_dir,
        queue,
        seen_hashes,
        completed_pages,
        total_slides_hint=total_hint,
    )
    while not queue.empty():
        yield queue.get_nowait()

    svg_files = get_svg_files(project_dir, source="output")
    _validate_agent_artifacts(project_dir, svg_files, total_hint)
    yield AgentProgressEvent(
        "generation",
        "complete",
        f"Agent updated {len(svg_files)} slides.",
        0.75,
        data={"total_slides": len(svg_files), "generation_mode": "agent"},
    )

    yield AgentProgressEvent("postprocess", "started", "Finalizing updated SVGs...", 0.78)
    async with heavy_stage_slot():
        stats = await aoffload(finalize_project, project_dir)
    yield AgentProgressEvent(
        "postprocess",
        "complete",
        f"Processed {stats.get('total_files', 0)} files",
        0.88,
    )

    yield AgentProgressEvent("export", "started", "Exporting updated PowerPoint...", 0.90)
    final_svg_files = get_svg_files(project_dir, source="final")
    notes = get_notes(project_dir, final_svg_files)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pptx_path = project_dir / "exports" / f"presentation_agent_feedback_{timestamp}.pptx"
    async with heavy_stage_slot():
        await aoffload(
            create_pptx,
            final_svg_files,
            pptx_path,
            canvas_format=_canvas_format_from_task(project_dir),
            notes=notes,
        )

    yield AgentProgressEvent(
        "export",
        "complete",
        "Updated PowerPoint generated!",
        1.0,
        data={"output_path": str(pptx_path)},
    )


async def _prepare_agent_workspace(project_dir: Path, request: Any, agent_config: dict[str, Any]) -> None:
    sources_dir = project_dir / "sources"
    (project_dir / "agent_feedback").mkdir(parents=True, exist_ok=True)
    source_target = sources_dir / ("paper.pdf" if request.source_type == "pdf" else "latex_source")
    input_path = Path(request.file_path)
    if input_path.is_dir():
        if source_target.exists():
            shutil.rmtree(source_target)
        shutil.copytree(input_path, source_target)
    else:
        if request.source_type == "latex":
            source_target = sources_dir / input_path.name
        shutil.copy2(input_path, source_target)

    _copy_agent_skill(project_dir)
    task = _build_agent_task(project_dir, source_target, request, agent_config)
    await awrite_text(
        project_dir / "agent_task.json",
        json.dumps(task, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    await _prefetch_agent_research(project_dir, source_target, request, agent_config)
    await awrite_text(project_dir / "AGENT_README.md", _agent_readme(), encoding="utf-8")


def _copy_template_reference_for_agent(template: Any, project_dir: Path) -> None:
    """Copy template skeleton files into the Agent workspace.

    ``copy_template_assets_for_project`` only prepares asset files for generated
    SVGs. Agent mode also needs the actual template SVG skeletons and design
    spec so Claude Code/Codex can inspect and preserve the selected template.
    """

    template_dir = getattr(template, "template_dir", None)
    info = getattr(template, "info", None)
    template_id = getattr(info, "template_id", None)
    if not template_dir or not template_id:
        return

    src = Path(template_dir)
    if not src.is_dir():
        return

    dest = project_dir / "templates" / str(template_id)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    metadata = {
        "template_id": template_id,
        "label": getattr(info, "label", "") or template_id,
        "source": getattr(info, "source", ""),
        "import_mode": getattr(info, "import_mode", ""),
        "viewbox": getattr(template, "viewbox", ""),
        "content_area": getattr(template, "content_area", {}),
        "files": sorted(path.name for path in dest.iterdir() if path.is_file()),
    }
    (dest / "template_context.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (project_dir / "templates" / "README.md").write_text(
        "Template references are copied here by template_id. Read the selected "
        "template's design_spec.md and page SVG skeletons before drafting slides.\n",
        encoding="utf-8",
    )


async def _prefetch_agent_research(
    project_dir: Path,
    source_target: Path,
    request: Any,
    agent_config: dict[str, Any],
) -> None:
    if not bool(agent_config.get("allow_external_research")):
        return
    research_cfg = getattr(request, "research_config", None)
    if research_cfg is None or not (
        getattr(research_cfg, "arxiv_search_enabled", False)
        or getattr(research_cfg, "semantic_scholar_enabled", False)
        or getattr(research_cfg, "web_search_enabled", False)
    ):
        return

    try:
        from backend.orchestrator.research_agent import ResearchContext
        from backend.orchestrator.research_enrichment import enrich_context

        title, abstract = await aoffload(
            _extract_agent_research_seed,
            source_target,
            str(getattr(request, "source_type", "")),
        )
        if not title:
            title = Path(getattr(request, "file_path", "") or source_target).stem
        ctx = ResearchContext()
        stats = await enrich_context(
            ctx,
            paper_title=title,
            paper_abstract=abstract,
            config=research_cfg,
        )
        research_dir = project_dir / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        sources = [finding.to_dict() for finding in ctx.findings]
        (research_dir / "sources.json").write_text(
            json.dumps(
                {
                    "query_title": title,
                    "stats": stats.to_dict(),
                    "sources": sources,
                    "notes": [
                        "Web sources include opened_url=true only when the backend fetched and read page body text.",
                        "Agent must inspect this file and research/brief.md before using external research in slides.",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (research_dir / "brief.md").write_text(
            _format_agent_research_brief(title, ctx, stats),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - research is optional, record limitation
        research_dir = project_dir / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        (research_dir / "sources.json").write_text(
            json.dumps(
                {
                    "sources": [],
                    "error": str(exc) or exc.__class__.__name__,
                    "notes": ["External research prefetch failed before Agent runtime."],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (research_dir / "brief.md").write_text(
            f"# External Research Prefetch\n\nPrefetch failed: {exc}\n",
            encoding="utf-8",
        )


def _extract_agent_research_seed(source_target: Path, source_type: str) -> tuple[str, str]:
    if source_type == "pdf" and source_target.is_file():
        try:
            import fitz  # type: ignore[import-not-found]

            doc = fitz.open(source_target)
            if doc.needs_pass:
                return source_target.stem, ""
            first_pages = [
                doc.load_page(i).get_text("text")
                for i in range(min(len(doc), 2))
            ]
            text = "\n".join(first_pages)
            title = _guess_title_from_text(text) or str(doc.metadata.get("title") or "").strip()
            abstract = _guess_abstract_from_text(text)
            doc.close()
            return title, abstract
        except Exception:
            return source_target.stem, ""

    if source_target.is_file():
        try:
            text = source_target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return source_target.stem, ""
    elif source_target.is_dir():
        texts: list[str] = []
        for tex_path in sorted(source_target.rglob("*.tex"))[:5]:
            try:
                texts.append(tex_path.read_text(encoding="utf-8", errors="ignore")[:20000])
            except OSError:
                continue
        text = "\n".join(texts)
    else:
        return source_target.stem, ""

    title_match = re.search(r"\\title\{(.+?)\}", text, flags=re.DOTALL)
    title = _clean_latex_text(title_match.group(1)) if title_match else source_target.stem
    abstract_match = re.search(
        r"\\begin\{abstract\}(.+?)\\end\{abstract\}",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    abstract = _clean_latex_text(abstract_match.group(1)) if abstract_match else ""
    return title, abstract


def _guess_title_from_text(text: str) -> str:
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()[:40]
        if re.sub(r"\s+", " ", line).strip()
    ]
    for line in lines:
        if 8 <= len(line) <= 180 and not line.lower().startswith(("abstract", "keywords")):
            return line
    return ""


def _guess_abstract_from_text(text: str) -> str:
    match = re.search(
        r"\babstract\b[:\s]*(.+?)(?:\n\s*(?:keywords|introduction|1\s+introduction)\b)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()[:2000]


def _clean_latex_text(text: str) -> str:
    text = re.sub(r"%.*", "", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def _format_agent_research_brief(title: str, ctx: Any, stats: Any) -> str:
    lines = [
        "# External Research Prefetch",
        "",
        f"Query title: {title}",
        "",
        "The backend opened web results when possible and stored body excerpts below. Use only claims that are relevant to the paper and cite sources in `agent_report.json`.",
        "",
        "## Stats",
        "",
        "```json",
        json.dumps(stats.to_dict(), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Sources",
        "",
    ]
    if not ctx.findings:
        lines.append("No external sources were fetched.")
        return "\n".join(lines) + "\n"
    for index, finding in enumerate(ctx.findings, 1):
        opened = "opened" if getattr(finding, "opened_url", False) else "not opened"
        lines.append(f"### {index}. {finding.title or 'Untitled'}")
        lines.append(f"- Source: {finding.source}")
        if finding.url:
            lines.append(f"- URL: {finding.url}")
        lines.append(f"- Read depth: {opened}")
        body = str(finding.abstract or "").strip()
        if body:
            lines.append("")
            lines.append(body[:2500])
        lines.append("")
    return "\n".join(lines)


def _build_agent_task(project_dir: Path, source_target: Path, request: Any, agent_config: dict[str, Any]) -> dict[str, Any]:
    fmt = CANVAS_FORMATS.get(request.canvas_format, CANVAS_FORMATS["ppt169"])
    research_cfg = getattr(request, "research_config", None)
    research_payload = research_cfg.model_dump(exclude_none=True) if hasattr(research_cfg, "model_dump") else None
    detail_profile = _agent_detail_profile(getattr(request, "detail_level", "normal"), getattr(request, "num_pages", None))
    return {
        "task": "Generate an academic presentation from the uploaded paper using Agent mode.",
        "source": {
            "type": request.source_type,
            "path": _rel(project_dir, source_target),
        },
        "output_contract": {
            "manuscript": "manuscript.md",
            "design_spec": "design_spec.md",
            "slides_dir": "svg_output",
            "slide_filename": "slide_001.svg, slide_002.svg, ...",
            "notes_dir": "notes",
            "report": "agent_report.json",
        },
        "presentation": {
            "canvas_format": request.canvas_format,
            "viewbox": fmt["viewbox"],
            "width": fmt["width"],
            "height": fmt["height"],
            "language": request.language,
            "target_pages": request.num_pages,
            "detail_level": request.detail_level,
            "style": request.style,
            "template_id": request.template_id,
            "user_instruction": request.instruction,
            "style_overrides": request.style_overrides,
            "detail_profile": detail_profile,
        },
        "agent_policy": {
            "do_not_call_backend_provider_models": True,
            "icons_default_enabled": True,
            "generate_slides_in_parallel_when_useful": True,
            "allow_external_research": bool(agent_config.get("allow_external_research")),
            "allow_deep_research": bool(agent_config.get("allow_deep_research")),
            "enable_visual_qa": bool(agent_config.get("enable_visual_qa", True)),
            "icon_policy": _agent_icon_policy(),
            "subagent_policy": _agent_subagent_policy(request, agent_config, detail_profile),
            "research_policy": _agent_research_policy(research_payload),
        },
        "research_config": research_payload,
        "paths": {
            "workspace": str(project_dir),
            "skill": str(project_dir / "skills" / _AGENT_SKILL_NAME / "SKILL.md"),
            "python": str(_agent_python_path()),
            "icons": _rel(project_dir, settings.icons_dir),
            "templates": _rel(project_dir, project_dir / "templates"),
            "selected_template": f"templates/{request.template_id}" if request.template_id else None,
            "feedback_dir": "agent_feedback",
            "prefetched_research_brief": "research/brief.md",
            "prefetched_research_sources": "research/sources.json",
        },
    }


def _agent_runtime_env(project_dir: Path) -> dict[str, str]:
    python_path = _agent_python_path()
    python_dir = str(python_path.parent)
    existing_path = os.environ.get("PATH") or os.environ.get("Path") or ""
    runtime_path = f"{python_dir}{os.pathsep}{existing_path}" if existing_path else python_dir
    env = {
        "PAPER_PPT_AGENT_MODE": "1",
        "PAPER_PPT_WORKSPACE": str(project_dir),
        "PAPER_PPT_PYTHON": str(python_path),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PATH": runtime_path,
        "Path": runtime_path,
    }
    env.update(_agent_research_env(project_dir))
    return env


def _agent_research_env(project_dir: Path) -> dict[str, str]:
    task = _read_agent_task(project_dir)
    research = task.get("research_config") if isinstance(task.get("research_config"), dict) else {}
    env: dict[str, str] = {}
    key_map = {
        "semantic_scholar_api_key": "SEMANTIC_SCHOLAR_API_KEY",
        "tavily_api_key": "TAVILY_API_KEY",
        "serpapi_key": "SERPAPI_KEY",
    }
    for source_key, env_key in key_map.items():
        value = research.get(source_key)
        if isinstance(value, str) and value.strip():
            env[env_key] = value.strip()
    return env


def _agent_python_path() -> Path:
    local_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if local_python.exists():
        return local_python.resolve()
    return Path(sys.executable).resolve()


def _codex_usage_progress_event(
    *,
    model_label: str,
    usage: Any,
    status: str = "running",
) -> AgentProgressEvent:
    total = _event_attr(usage, ("payload", "token_usage", "total")) if usage is not None else None
    context_window = _event_attr(usage, ("payload", "token_usage", "model_context_window")) if usage is not None else None
    data = {
        "model": model_label,
        "input_tokens": _safe_int(_event_attr(total, ("input_tokens",))),
        "output_tokens": _safe_int(_event_attr(total, ("output_tokens",))),
        "cache_read_input_tokens": _safe_int(_event_attr(total, ("cached_input_tokens",))),
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": _safe_int(_event_attr(total, ("reasoning_output_tokens",))),
        "total_tokens": _safe_int(_event_attr(total, ("total_tokens",))),
        "model_context_window": _safe_int(context_window) if context_window is not None else None,
    }
    return AgentProgressEvent(
        "agent",
        "progress",
        "Usage update",
        0.12,
        data={
            "runtime": "codex",
            "agent_event": {
                "type": "usage",
                "stage": "agent",
                "status": status,
                "message": "Usage update",
                "data": data,
            },
        },
    )


def _codex_token_usage_values(message: Any) -> dict[str, int] | None:
    if str(getattr(message, "method", "") or "") != "thread/tokenUsage/updated":
        return None
    total = _event_attr(message, ("payload", "token_usage", "total"))
    if total is None:
        return None
    return {
        "input_tokens": _safe_int(_event_attr(total, ("input_tokens",))),
        "output_tokens": _safe_int(_event_attr(total, ("output_tokens",))),
        "cache_read_input_tokens": _safe_int(_event_attr(total, ("cached_input_tokens",))),
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": _safe_int(_event_attr(total, ("reasoning_output_tokens",))),
        "total_tokens": _safe_int(_event_attr(total, ("total_tokens",))),
        "model_context_window": _safe_int(
            _event_attr(message, ("payload", "token_usage", "model_context_window"))
        ),
    }


def _agent_usage_progress_event(totals: _AgentUsageTotals) -> AgentProgressEvent:
    data: dict[str, Any] = {
        "model": totals.model,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_input_tokens": totals.cache_read_input_tokens,
        "cache_creation_input_tokens": totals.cache_creation_input_tokens,
        "total_tokens": (
            totals.input_tokens
            + totals.output_tokens
            + totals.cache_read_input_tokens
            + totals.cache_creation_input_tokens
        ),
        "total_cost_usd": round(totals.total_cost_usd, 6),
        "num_turns": totals.num_turns,
        "duration_ms": totals.duration_ms,
    }
    return AgentProgressEvent(
        "agent",
        "progress",
        "Usage update",
        0.12,
        data={
            "runtime": totals.runtime,
            "agent_event": {
                "type": "usage",
                "stage": "agent",
                "status": "running",
                "message": "Usage update",
                "data": data,
            },
        },
    )


def _agent_paused_event() -> AgentProgressEvent:
    return AgentProgressEvent(
        "agent",
        "progress",
        "Agent paused. Send guidance to continue from the current workspace.",
        0.12,
        data={
            "agent_paused": True,
            "agent_event": {
                "type": "status",
                "status": "paused",
                "data": {"agent_paused": True},
            },
        },
    )


def _codex_item_progress_event(event: Any, method: str) -> AgentProgressEvent | None:
    if method not in {"item/started", "item/completed"}:
        return None
    item = _codex_event_item(event)
    if item is None:
        return None
    item_type = str(getattr(item, "type", "") or "")
    is_started = method == "item/started"
    if item_type == "agentMessage":
        if is_started:
            return None
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            return None
        return AgentProgressEvent(
            "agent",
            "progress",
            text[:2000],
            0.12,
            data={
                "runtime": "codex",
                "method": method,
                "agent_event": {
                    "type": "message",
                    "status": "complete",
                    "data": {},
                },
            },
        )

    tool_name = _codex_item_tool_name(item)
    if not tool_name:
        return None

    tool_use_id = str(getattr(item, "id", "") or "")
    item_status = _enum_value(getattr(item, "status", None))
    status = "running" if is_started else ("error" if item_status in {"failed", "declined"} else "complete")
    message = _codex_item_tool_label(item) if is_started else "Tool result"
    data = {
        "tool": tool_name,
        "tool_use_id": tool_use_id,
        "input": _codex_item_tool_input(item),
        "output": _codex_item_tool_output(item),
        "codex_item_type": item_type,
        "codex_status": item_status,
    }
    return AgentProgressEvent(
        "agent",
        "progress",
        message,
        0.12,
        data={
            "runtime": "codex",
            "method": method,
            "agent_event": {
                "type": "tool",
                "status": status,
                "data": data,
            },
        },
    )


def _codex_event_item(event: Any) -> Any | None:
    for path in (
        ("payload", "item", "root"),
        ("payload", "item"),
        ("params", "item", "root"),
        ("params", "item"),
    ):
        current = event
        try:
            for part in path:
                current = getattr(current, part)
        except AttributeError:
            continue
        return current
    return None


def _codex_item_tool_name(item: Any) -> str:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        return "Bash"
    if item_type == "mcpToolCall":
        server = str(getattr(item, "server", "") or "")
        tool = str(getattr(item, "tool", "") or "")
        return f"{server}:{tool}" if server else tool
    if item_type == "dynamicToolCall":
        namespace = str(getattr(item, "namespace", "") or "")
        tool = str(getattr(item, "tool", "") or "")
        return f"{namespace}:{tool}" if namespace else tool
    return ""


def _codex_item_tool_label(item: Any) -> str:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        command = str(getattr(item, "command", "") or "").strip()
        return f"Bash {command}".strip()
    return _codex_item_tool_name(item) or item_type


def _codex_item_tool_input(item: Any) -> dict[str, Any]:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        return {
            "command": str(getattr(item, "command", "") or ""),
            "cwd": str(getattr(item, "cwd", "") or ""),
        }
    arguments = getattr(item, "arguments", None)
    return {"arguments": arguments} if arguments is not None else {}


def _codex_item_tool_output(item: Any) -> dict[str, Any]:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        return {
            "exit_code": getattr(item, "exit_code", None),
            "duration_ms": getattr(item, "duration_ms", None),
            "aggregated_output": str(getattr(item, "aggregated_output", "") or "")[:2000],
        }
    result = getattr(item, "result", None)
    error = getattr(item, "error", None)
    return {"result": result, "error": error}


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _event_attr(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for part in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _claude_usage_values(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    values = {
        "input_tokens": _safe_int(usage.get("input_tokens")),
        "output_tokens": _safe_int(usage.get("output_tokens")),
        "cache_read_input_tokens": _safe_int(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _safe_int(usage.get("cache_creation_input_tokens")),
    }
    return values if any(values.values()) else None


def _claude_model_usage_values(
    model_usage: Any,
) -> tuple[str | None, dict[str, int] | None, float]:
    if not isinstance(model_usage, dict):
        return None, None, 0.0
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    selected_model: str | None = None
    selected_model_tokens = -1
    total_cost = 0.0
    for model_name, raw_usage in model_usage.items():
        if not isinstance(raw_usage, dict):
            continue
        values = {
            "input_tokens": _safe_int(raw_usage.get("inputTokens")),
            "output_tokens": _safe_int(raw_usage.get("outputTokens")),
            "cache_read_input_tokens": _safe_int(raw_usage.get("cacheReadInputTokens")),
            "cache_creation_input_tokens": _safe_int(raw_usage.get("cacheCreationInputTokens")),
        }
        model_tokens = sum(values.values())
        if isinstance(model_name, str) and model_tokens > selected_model_tokens:
            selected_model = model_name
            selected_model_tokens = model_tokens
        for key, value in values.items():
            totals[key] += value
        cost = raw_usage.get("costUSD")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
    return selected_model, (totals if any(totals.values()) else None), total_cost


def _add_claude_usage_values(
    totals: _AgentUsageTotals,
    values: dict[str, int],
    *,
    pending: bool,
) -> bool:
    changed = False
    for key, attr, pending_attr in (
        ("input_tokens", "input_tokens", "pending_input_tokens"),
        ("output_tokens", "output_tokens", "pending_output_tokens"),
        ("cache_read_input_tokens", "cache_read_input_tokens", "pending_cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens", "pending_cache_creation_input_tokens"),
    ):
        value = _safe_int(values.get(key))
        if value <= 0:
            continue
        setattr(totals, attr, getattr(totals, attr) + value)
        if pending:
            setattr(totals, pending_attr, getattr(totals, pending_attr) + value)
        changed = True
    return changed


def _pending_claude_usage_values(totals: _AgentUsageTotals) -> dict[str, int]:
    return {
        "input_tokens": totals.pending_input_tokens,
        "output_tokens": totals.pending_output_tokens,
        "cache_read_input_tokens": totals.pending_cache_read_input_tokens,
        "cache_creation_input_tokens": totals.pending_cache_creation_input_tokens,
    }


def _current_claude_usage_values(totals: _AgentUsageTotals) -> dict[str, int]:
    return {
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_input_tokens": totals.cache_read_input_tokens,
        "cache_creation_input_tokens": totals.cache_creation_input_tokens,
    }


def _reset_pending_claude_usage(totals: _AgentUsageTotals) -> None:
    totals.pending_input_tokens = 0
    totals.pending_output_tokens = 0
    totals.pending_cache_read_input_tokens = 0
    totals.pending_cache_creation_input_tokens = 0


def _usage_values_equal(left: dict[str, int], right: dict[str, int]) -> bool:
    return all(_safe_int(left.get(key)) == _safe_int(right.get(key)) for key in left)


async def _scan_svg_updates(
    project_dir: Path,
    queue: asyncio.Queue[AgentProgressEvent],
    seen_hashes: dict[Path, str],
    completed_pages: set[int],
    *,
    total_slides_hint: int,
) -> None:
    svg_files = get_svg_files(project_dir, source="output")
    for ordinal, svg_path in enumerate(svg_files, 1):
        try:
            content = svg_path.read_text(encoding="utf-8")
        except OSError:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if seen_hashes.get(svg_path) == digest:
            continue
        try:
            _validate_single_svg(svg_path, content)
            preview = _embed_svg_preview(content, project_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping invalid Agent SVG preview %s: %s", svg_path, exc)
            continue
        seen_hashes[svg_path] = digest
        page = _slide_number(svg_path, ordinal)
        completed_pages.add(page)
        total = max(total_slides_hint, len(svg_files), max(completed_pages or {0}))
        progress = 0.15 + (len(completed_pages) / max(total, 1)) * 0.55
        await queue.put(
            AgentProgressEvent(
                "generation",
                "progress",
                f"Agent updated slide {page}",
                min(progress, 0.72),
                data={
                    "page": page,
                    "svg": preview,
                    "completed_count": len(completed_pages),
                    "total_slides": total,
                    "generation_mode": "agent",
                },
            )
        )


def _validate_agent_artifacts(project_dir: Path, svg_files: list[Path], total_slides_hint: int) -> None:
    if not svg_files:
        raise RuntimeError(_empty_agent_output_message(project_dir))
    if total_slides_hint and len(svg_files) < total_slides_hint:
        raise RuntimeError(
            f"Agent produced {len(svg_files)} slides, expected at least {total_slides_hint}."
        )
    for svg_path in svg_files:
        _validate_single_svg(svg_path, svg_path.read_text(encoding="utf-8"))
    report_path = project_dir / "agent_report.json"
    if not report_path.exists():
        task = _read_agent_task(project_dir)
        policy = task.get("agent_policy") if isinstance(task.get("agent_policy"), dict) else {}
        if policy.get("allow_external_research") or policy.get("allow_deep_research"):
            raise RuntimeError(
                "Agent mode requires agent_report.json when external or deep research is enabled."
            )
        report_path.write_text(
            json.dumps(
                {
                    "status": "completed_without_report",
                    "slides": [path.name for path in svg_files],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return

    _validate_agent_report(project_dir, report_path)


def _validate_agent_report(project_dir: Path, report_path: Path) -> None:
    task = _read_agent_task(project_dir)
    policy = task.get("agent_policy") if isinstance(task.get("agent_policy"), dict) else {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("agent_report.json is not valid JSON.") from exc

    if not isinstance(report, dict):
        raise RuntimeError("agent_report.json must contain a JSON object.")

    if policy.get("allow_external_research"):
        sources_path = project_dir / "research" / "sources.json"
        external_used = bool(report.get("external_research_used"))
        has_sources = False
        has_recorded_limitation = False
        if sources_path.exists():
            try:
                sources_payload = json.loads(sources_path.read_text(encoding="utf-8"))
                sources = sources_payload.get("sources")
                has_sources = isinstance(sources, list) and bool(sources)
                has_recorded_limitation = bool(sources_payload.get("error"))
            except json.JSONDecodeError:
                has_recorded_limitation = True
        if has_sources and not external_used:
            raise RuntimeError(
                "External research sources were prefetched, but agent_report.json does not mark external_research_used=true."
            )
        if not external_used and not has_sources and not has_recorded_limitation:
            raise RuntimeError(
                "External research was enabled, but agent_report.json/research sources do not show used sources or a concrete limitation."
            )

    if policy.get("allow_deep_research"):
        subagents = report.get("subagents")
        if not isinstance(subagents, list) or not subagents:
            raise RuntimeError(
                "Deep research was enabled, but agent_report.json does not list SubAgent usage or skip reasons."
            )
        missing_reason = [
            item for item in subagents
            if isinstance(item, dict)
            and not bool(item.get("used"))
            and not str(item.get("skip_reason") or "").strip()
        ]
        if missing_reason:
            raise RuntimeError(
                "Deep research SubAgent entries must include skip_reason when not used."
            )


def _empty_agent_output_message(project_dir: Path) -> str:
    report_path = project_dir / "agent_report.json"
    report_status = ""
    if report_path.exists():
        try:
            raw_report = json.loads(report_path.read_text(encoding="utf-8"))
            report_status = str(
                raw_report.get("status")
                or raw_report.get("summary")
                or raw_report.get("error")
                or ""
            ).strip()
        except (OSError, json.JSONDecodeError):
            report_status = "agent_report.json exists but could not be parsed"

    known_files = [
        name
        for name in ("manuscript.md", "design_spec.md", "agent_report.json")
        if (project_dir / name).exists()
    ]
    stray_svgs = [
        _relative_path(project_dir, path)
        for path in sorted(project_dir.rglob("*.svg"))
        if "svg_output" not in path.parts
        and "svg_final" not in path.parts
        and not path.name.startswith(".__")
    ][:8]

    details: list[str] = []
    if known_files:
        details.append(f"existing files: {', '.join(known_files)}")
    if report_status:
        details.append(f"agent report: {report_status[:300]}")
    if stray_svgs:
        details.append(f"SVGs found outside svg_output/: {', '.join(stray_svgs)}")

    suffix = f" ({'; '.join(details)})" if details else ""
    return f"Agent mode completed without producing SVG slides in svg_output/.{suffix}"


def _update_agent_usage_totals(totals: _AgentUsageTotals, message: Any) -> AgentProgressEvent | None:
    if totals.runtime == "codex":
        changed = False
        if totals.model and totals.seen_message_ids is None:
            totals.seen_message_ids = set()
        if totals.model and totals.seen_message_ids is not None and "codex:model" not in totals.seen_message_ids:
            totals.seen_message_ids.add("codex:model")
            changed = True
        token_values = _codex_token_usage_values(message)
        if token_values is not None:
            totals.input_tokens = token_values["input_tokens"]
            totals.output_tokens = token_values["output_tokens"]
            totals.cache_read_input_tokens = token_values["cache_read_input_tokens"]
            totals.cache_creation_input_tokens = token_values["cache_creation_input_tokens"]
            changed = True
        if not changed:
            return None
        data: dict[str, Any] = {
            "model": totals.model,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cache_read_input_tokens": totals.cache_read_input_tokens,
            "cache_creation_input_tokens": totals.cache_creation_input_tokens,
            "total_cost_usd": 0.0,
            "num_turns": totals.num_turns,
            "duration_ms": totals.duration_ms,
        }
        if token_values is not None:
            data["reasoning_output_tokens"] = token_values["reasoning_output_tokens"]
            data["total_tokens"] = token_values["total_tokens"]
            data["model_context_window"] = token_values["model_context_window"]
        return AgentProgressEvent(
            "agent",
            "progress",
            "Usage update",
            0.12,
            data={
                "runtime": totals.runtime,
                "agent_event": {
                    "type": "usage",
                    "stage": "agent",
                    "status": "running",
                    "message": "Usage update",
                    "data": data,
                },
            },
        )

    kind = message.__class__.__name__
    changed = False
    if totals.seen_message_ids is None:
        totals.seen_message_ids = set()
    if kind == "AssistantMessage":
        model = getattr(message, "model", None)
        if isinstance(model, str) and model:
            totals.model = model
            changed = True
        msg_id = getattr(message, "message_id", None) or getattr(message, "uuid", None)
        if isinstance(msg_id, str) and msg_id in totals.seen_message_ids:
            return None
        if isinstance(msg_id, str):
            totals.seen_message_ids.add(msg_id)
        usage_values = _claude_usage_values(getattr(message, "usage", None))
        if usage_values is not None:
            changed = _add_claude_usage_values(totals, usage_values, pending=True) or changed
    elif kind == "ResultMessage":
        if totals.seen_result_ids is None:
            totals.seen_result_ids = set()
        result_id = getattr(message, "uuid", None)
        if not isinstance(result_id, str) or not result_id:
            result_id = ":".join(
                str(getattr(message, name, "") or "")
                for name in ("session_id", "duration_ms", "duration_api_ms", "num_turns", "stop_reason")
            )
        result_seen = result_id in totals.seen_result_ids
        totals.seen_result_ids.add(result_id)

        cost = getattr(message, "total_cost_usd", None)
        if isinstance(cost, (int, float)) and not result_seen:
            totals.total_cost_usd += float(cost)
            changed = True
        num_turns = getattr(message, "num_turns", None)
        if isinstance(num_turns, int) and not result_seen:
            totals.num_turns += num_turns
            changed = True
        duration_ms = getattr(message, "duration_ms", None)
        if isinstance(duration_ms, int) and not result_seen:
            totals.duration_ms += duration_ms
            changed = True
        model_usage = getattr(message, "model_usage", None)
        usage_model, model_usage_values, model_usage_cost = _claude_model_usage_values(model_usage)
        if usage_model and not totals.model:
            totals.model = usage_model
            changed = True
        if not result_seen:
            usage_values = _claude_usage_values(getattr(message, "usage", None)) or model_usage_values
            if usage_values is not None:
                current_values = _current_claude_usage_values(totals)
                if not _usage_values_equal(usage_values, current_values):
                    pending_values = _pending_claude_usage_values(totals)
                    missing_values = {
                        key: max(_safe_int(usage_values.get(key)) - _safe_int(pending_values.get(key)), 0)
                        for key in usage_values
                    }
                    changed = _add_claude_usage_values(totals, missing_values, pending=False) or changed
            if model_usage_cost and cost is None:
                totals.total_cost_usd += model_usage_cost
                changed = True
        _reset_pending_claude_usage(totals)
    if not changed:
        return None
    return AgentProgressEvent(
        "agent",
        "progress",
        "Usage update",
        0.12,
        data={
            "runtime": totals.runtime,
            "agent_event": {
                "type": "usage",
                "stage": "agent",
                "status": "running",
                "message": "Usage update",
                "data": {
                    "model": totals.model,
                    "input_tokens": totals.input_tokens,
                    "output_tokens": totals.output_tokens,
                    "cache_read_input_tokens": totals.cache_read_input_tokens,
                    "cache_creation_input_tokens": totals.cache_creation_input_tokens,
                    "total_tokens": (
                        totals.input_tokens
                        + totals.output_tokens
                        + totals.cache_read_input_tokens
                        + totals.cache_creation_input_tokens
                    ),
                    "total_cost_usd": round(totals.total_cost_usd, 6),
                    "num_turns": totals.num_turns,
                    "duration_ms": totals.duration_ms,
                },
            },
        },
    )


def _validate_single_svg(svg_path: Path, content: str) -> None:
    if "<svg" not in content:
        raise ValueError(f"{svg_path.name} is not an SVG document")
    # Agents sometimes emit HTML comments or whitespace before the <?xml
    # declaration.  Strip everything before the first '<svg' tag so the
    # XML parser does not choke on "XML declaration not at start".
    svg_start = content.find("<svg")
    if svg_start > 0:
        content = content[svg_start:]
    root = ET.fromstring(content)
    tag = root.tag.split("}", 1)[-1].lower()
    if tag != "svg":
        raise ValueError(f"{svg_path.name} root element is not svg")
    if not (root.get("viewBox") or (root.get("width") and root.get("height"))):
        raise ValueError(f"{svg_path.name} must define viewBox or width/height")


def _embed_svg_preview(svg_content: str, project_dir: Path) -> str:
    from backend.generator.svg_finalize.render_ready import prepare_svg_content_for_render

    # Strip leading comments / whitespace before <?xml or <svg so downstream
    # XML parsers do not fail with "declaration not at start of entity".
    svg_start = svg_content.find("<svg")
    if svg_start > 0:
        svg_content = svg_content[svg_start:]
    return prepare_svg_content_for_render(svg_content, project_dir / "svg_output")


def _copy_agent_skill(project_dir: Path) -> None:
    source = PROJECT_ROOT / "assets" / "agent_skills" / _AGENT_SKILL_NAME
    if not source.exists():
        raise FileNotFoundError(f"Agent skill not found: {source}")
    for target in (
        project_dir / "skills" / _AGENT_SKILL_NAME,
        project_dir / ".claude" / "skills" / _AGENT_SKILL_NAME,
    ):
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
    references = source / "references"
    if references.exists():
        root_references = project_dir / "references"
        if root_references.exists():
            shutil.rmtree(root_references)
        shutil.copytree(references, root_references)


def _build_runtime_prompt(project_dir: Path, request: Any, agent_config: dict[str, Any]) -> str:
    language = "Chinese" if str(getattr(request, "language", "zh")).lower().startswith("zh") else "English"
    deep = "enabled" if agent_config.get("allow_deep_research") else "disabled"
    external = "enabled" if agent_config.get("allow_external_research") else "disabled"
    review = "enabled" if agent_config.get("enable_visual_qa", True) else "disabled"
    detail_profile = _agent_detail_profile(getattr(request, "detail_level", "normal"), getattr(request, "num_pages", None))
    skill_path = str(project_dir / "skills" / _AGENT_SKILL_NAME / "SKILL.md")
    return f"""Use the `{_AGENT_SKILL_NAME}` skill to generate the full PowerPoint deck.

Workspace contract:
- First read `agent_task.json`.
- The current working directory is the generated workspace. If you need to open the skill file manually, read `{skill_path}` and follow its output contract.
- Do not call this application's legacy provider pipeline or ask for external model API keys.
- Use subagents/parallel work when the runtime supports it. When deep research is enabled, SubAgents are required unless the Task/SubAgent tool is unavailable or fails.
- External research: {external}. Deep research: {deep}. Agent post-generation review: {review}.
- Detail contract: {detail_profile["label"]}; {detail_profile["slide_count_guidance"]} Content slides should carry {detail_profile["evidence_points_per_content_slide"]} concrete evidence point(s), {detail_profile["bullets_per_content_slide"]} bullets/claims, and {detail_profile["visuals_per_content_slide"]} visual argument(s) each when the paper supports it.
- Icon contract: before using an icon, search the local icon path from `agent_task.json.paths.icons` and use an existing file. Never use letters, initials, ASCII/Unicode symbols, emoji, dingbats, or decorative characters as icon substitutes; omit the icon or use a non-icon shape if no matching asset exists.
- SubAgent contract: do not split work just to satisfy ordinary slide count or detail level. When deep research is enabled, launch focused research SubAgents if the Task tool is available; use separate readers for background/related work, method, experiments, and critique when the paper is complex. When Agent review is enabled, run one focused review SubAgent after the first full deck is drafted; it should review the whole deck for layout overflow, missing assets, icon-policy violations, and narrative gaps instead of checking pages one by one. If you skip a required deep-research or review SubAgent, write a concrete reason in `agent_report.json.subagents`; the main Agent must merge results and keep final narrative/design consistency.
- External research contract: if external research is enabled, use `agent_task.json.research_config` plus any environment credentials named in `agent_task.json.agent_policy.research_policy.credential_env`. You may use other accessible search methods too. For web results, open/read relevant pages deeply enough to understand the source; do not cite titles/snippets alone.
- If `research/brief.md` or `research/sources.json` exists, read them before drafting the manuscript. Treat `opened_url=true` or "Read depth: opened" as evidence that the backend fetched page body text; if a web item was not opened, do not use it as evidence unless you open and read it yourself.
- When external research is enabled, your first user-visible progress reply after reading the paper seed and prefetched research must summarize what you actually learned: identify usable sources, give 2-4 slide-relevant findings when available, state failed/limited sources, and say how this changes the planned deck. Do not merely echo source counts or API status. Send this reply before writing `manuscript.md` or slide SVGs.
- Reply language for status/reporting: {language}.
- During generation, periodically check `agent_feedback/` for user guidance files. Treat newer guidance as higher priority unless it conflicts with the output contract.
- Work in stages. After completing each major milestone (e.g. finishing a slide, completing the manuscript, or finishing a batch of slides), briefly note what you accomplished. The system will automatically pause and resume your work, allowing the user to provide guidance between stages. When you resume, do NOT repeat completed work — pick up exactly where you left off.
- If the source is a PDF, do not use the Read tool directly on the PDF binary. Use `agent_task.json.paths.python` or `PAPER_PPT_PYTHON` to run Python/PyMuPDF (`fitz`) first, and only report password protection when `doc.needs_pass` is true.

Required final files:
- `manuscript.md`
- `design_spec.md`
- `svg_output/slide_001.svg`, `svg_output/slide_002.svg`, ...
- optional `notes/slide_001.md`, ...
- `agent_report.json`

When a slide SVG is complete, write it immediately to `svg_output/` so the app can preview it live.
Finish only after all required SVGs validate and `agent_report.json` summarizes the work.
"""


def _build_feedback_runtime_prompt(project_dir: Path, feedback: str) -> str:
    skill_path = str(project_dir / "skills" / _AGENT_SKILL_NAME / "SKILL.md")
    return f"""Use the `{_AGENT_SKILL_NAME}` skill to revise the existing PowerPoint deck in this same workspace.

Workspace contract:
- First read `agent_task.json`, then read `{skill_path}` if needed.
- Read the existing `manuscript.md`, `design_spec.md`, current `svg_output/*.svg`, and the newest files in `agent_feedback/`.
- Apply this latest user feedback: {feedback.strip()}
- Do not call this application's legacy provider pipeline or ask for external model API keys.
- Do not regenerate the whole deck from scratch unless the feedback truly requires structural changes.
- Preserve existing slide order and style unless the feedback asks to change them.
- When a slide SVG is revised, write it immediately to `svg_output/` so the app can preview it live.
- Keep following `agent_task.json.presentation.detail_profile`, `agent_task.json.agent_policy.icon_policy`, and `agent_task.json.agent_policy.subagent_policy`.
- Continue to search local icons before icon use. Never use letters, symbols, emoji, or decorative glyphs as icon substitutes.
- Use `agent_task.json.paths.python` or `PAPER_PPT_PYTHON` for Python/PyMuPDF commands instead of system Python.
- Use SubAgents/Task for deep research or final whole-deck review when those modes are enabled and supported; otherwise use them only when genuinely useful.
- Update `manuscript.md`, `design_spec.md`, notes, and `agent_report.json` to reflect the revision.

Finish only after the revised SVGs validate and `agent_report.json` summarizes what changed.
"""


def _agent_config_from_existing_task(project_dir: Path, runtime: str) -> dict[str, Any]:
    task = _read_agent_task(project_dir)
    policy = task.get("agent_policy") if isinstance(task.get("agent_policy"), dict) else {}
    return {
        "runtime": runtime,
        "allow_external_research": bool(policy.get("allow_external_research")),
        "allow_deep_research": bool(policy.get("allow_deep_research")),
        "enable_visual_qa": bool(policy.get("enable_visual_qa", True)),
    }


def _canvas_format_from_task(project_dir: Path) -> str:
    task = _read_agent_task(project_dir)
    presentation = task.get("presentation") if isinstance(task.get("presentation"), dict) else {}
    value = presentation.get("canvas_format")
    return str(value or "ppt169")


def _read_agent_task(project_dir: Path) -> dict[str, Any]:
    task_path = project_dir / "agent_task.json"
    try:
        return json.loads(task_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _agent_detail_profile(detail_level: str | None, num_pages: int | None) -> dict[str, Any]:
    normalized = str(detail_level or "normal").strip().lower()
    if normalized not in {"normal", "high", "very_high"}:
        normalized = "normal"
    budget = page_type_budget(num_pages, normalized)
    min_pages, max_pages = auto_slide_range(normalized)
    slide_count_guidance = page_type_budget_guidance(num_pages, normalized)
    base: dict[str, dict[str, Any]] = {
        "normal": {
            "label": "normal detail",
            "bullets_per_content_slide": "3-4",
            "evidence_points_per_content_slide": "1-2",
            "visuals_per_content_slide": "1",
            "speaker_notes": "optional, 2-4 short sentences per content slide",
            "paper_coverage": [
                "problem and motivation",
                "core method or architecture",
                "main experiments and key metrics",
                "limitations or implications when relevant",
            ],
            "depth_checks": [
                "Each content slide must include at least one concrete paper fact, metric, equation, figure/table reference, or mechanism.",
                "Avoid dumping full paragraphs; compress into slide-ready claims.",
            ],
        },
        "high": {
            "label": "high detail",
            "bullets_per_content_slide": "4-5",
            "evidence_points_per_content_slide": "2-3",
            "visuals_per_content_slide": "1-2",
            "speaker_notes": "recommended, 4-6 sentences per content slide",
            "paper_coverage": [
                "problem framing and research gap",
                "method internals with mechanism-level explanation",
                "training/data/evaluation setup when present",
                "main, ablation, and comparison results",
                "limitations, assumptions, and practical implications",
            ],
            "depth_checks": [
                "Method/result slides must include numbers, named components, datasets, equations, or figure/table anchors when available.",
                "Use enough slides to separate architecture, evidence, and takeaway instead of overcrowding one page.",
            ],
        },
        "very_high": {
            "label": "very high detail",
            "bullets_per_content_slide": "5-6",
            "evidence_points_per_content_slide": "3-4",
            "visuals_per_content_slide": "1-2",
            "speaker_notes": "expected, 5-8 sentences per content slide with technical explanation",
            "paper_coverage": [
                "research context, gap, and thesis",
                "architecture/method flow with component-level detail",
                "math/objectives/training or algorithm steps when present",
                "datasets, baselines, metrics, ablations, and error/limitation analysis",
                "what changes for future work or deployment",
            ],
            "depth_checks": [
                "Do not collapse multiple major paper sections into a single generic summary slide.",
                "Prefer separate slides for mechanism, experimental setup, headline results, ablations, and limitations when the target slide count allows.",
                "Include enough technical anchors that a reader can trace claims back to the paper.",
            ],
        },
    }
    profile = dict(base[normalized])
    profile.update(
        {
            "requested_detail_level": normalized,
            "target_pages": num_pages,
            "auto_slide_range": [min_pages, max_pages],
            "page_type_budget": budget,
            "slide_count_guidance": slide_count_guidance,
        }
    )
    return profile


def _agent_icon_policy() -> dict[str, Any]:
    return {
        "must_search_local_icons_before_use": True,
        "icon_search_paths": ["agent_task.json.paths.icons", "assets/icons if exposed by add_dirs"],
        "recommended_search_commands": [
            "rg --files <icons_path>",
            "rg -i \"semantic-keyword|tag|concept\" <icons_path>",
            "Get-ChildItem -Recurse <icons_path> -Filter *.svg",
        ],
        "allowed": [
            "existing SVG/icon files found in the configured local icon path",
            "template-provided icons or vector assets that actually exist",
            "simple geometric shapes only when they are decorative shapes, not fake icons",
        ],
            "forbidden_substitutes": [
                "single letters or initials such as P, F, A, Q",
                "ASCII punctuation or symbols such as !, ?, +, *, #, >, <, /",
                "Unicode symbols, dingbats, stars, checkmarks, arrows, warning marks, and pictographs used as icons",
                "emoji or colored emoji glyphs",
                "text glyph arrows such as →, ←, ↑, ↓; use SVG line/path geometry instead",
                "any single-character text used as a visual badge, status mark, icon, or connector",
                "made-up icon filenames or remote icon URLs",
            ],
        "fallback_when_missing": "Omit the icon or use a neutral non-icon shape/card accent; do not replace it with letters, punctuation, symbols, arrows, dingbats, or emoji.",
        "reporting": "agent_report.json must mention whether local icons were searched and list any icon assets used.",
    }


def _agent_subagent_policy(request: Any, agent_config: dict[str, Any], detail_profile: dict[str, Any]) -> dict[str, Any]:
    target_pages = int(getattr(request, "num_pages", None) or 0)
    detail = str(detail_profile.get("requested_detail_level") or "normal")
    review_enabled = bool(agent_config.get("enable_visual_qa", True))
    return {
        "enabled_when_runtime_supports_task_tool": True,
        "required_when": [
            "allow_deep_research is true",
            "agent_review is enabled",
        ],
        "skip_requires_reason": True,
        "main_agent_responsibility": [
            "own final narrative arc, design_spec.md, consistency, and final SVG integration",
            "merge SubAgent findings instead of pasting independent styles into the deck",
            "resolve user feedback from agent_feedback/ before final export",
        ],
        "use_subagents_for": [
            {
                "scenario": "paper extraction",
                "when": "useful for long/figure-heavy papers, but not required unless deep research is enabled",
                "output": "section-level facts, figures/tables, equations, datasets, metrics, limitations",
            },
            {
                "scenario": "external research",
                "when": "allow_external_research is true",
                "output": "research/brief.md and research/sources.json with only slide-relevant context",
            },
            {
                "scenario": "deep research",
                "when": "allow_deep_research is true",
                "output": "parallel reader notes for background, method, experiments, related work, and critique",
            },
            {
                "scenario": "icon and asset inventory",
                "when": "slides need visual labels, comparison cards, process diagrams, or repeated semantic icons",
                "output": "a checked list of existing local icon filenames mapped to slide concepts",
            },
            {
                "scenario": "slide batch drafting",
                "when": "target_pages >= 8 or detail_level is high/very_high",
                "output": "draft SVGs for separate slide ranges that follow design_spec.md",
            },
            {
                "scenario": "QA",
                "when": "agent_review is enabled; after the first complete deck draft, not per page during generation",
                "output": "whole-deck review findings for layout overflow, missing assets, icon-policy violations, narrative gaps, and slides needing repair",
            },
        ],
        "recommended_parallelism": {
            "target_pages": target_pages,
            "detail_level": detail,
            "paper_extraction_subagents": 1 if agent_config.get("allow_deep_research") else 0,
            "research_subagents": 3 if agent_config.get("allow_deep_research") else (1 if agent_config.get("allow_external_research") else 0),
            "qa_subagents": 1 if review_enabled else 0,
        },
        "agent_review": {
            "enabled": review_enabled,
            "max_attempts": None,
            "scope": "whole deck after generation",
            "replaces_provider_visual_qa": True,
        },
        "reporting": "agent_report.json must list deep-research and review SubAgent scenarios with used=true/false and a specific reason when skipped.",
    }


def _agent_research_policy(research_payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = research_payload or {}
    web_provider = str(payload.get("web_search_provider") or "tavily")
    return {
        "enabled_sources": {
            "arxiv": bool(payload.get("arxiv_search_enabled")),
            "semantic_scholar": bool(payload.get("semantic_scholar_enabled")),
            "web_search": bool(payload.get("web_search_enabled")),
        },
        "web_search_provider": web_provider,
        "max_results_per_source": int(payload.get("max_results_per_source") or 20),
        "relevance_filter": bool(payload.get("relevance_filter", True)),
        "credential_env": {
            "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY" if payload.get("semantic_scholar_api_key") else None,
            "tavily": "TAVILY_API_KEY" if payload.get("tavily_api_key") else None,
            "serpapi": "SERPAPI_KEY" if payload.get("serpapi_key") else None,
        },
        "agent_may_use_other_sources": True,
        "must_open_and_read_sources": True,
        "minimum_source_depth": (
            "For web results, open the relevant pages and read enough body content "
            "to understand the claim, method, evidence, date, and reliability. "
            "Do not rely on result titles/snippets alone."
        ),
    }


def _agent_readme() -> str:
    return """# Agent PPT Generation Workspace

This workspace is owned by Claude Code or Codex Agent mode.  The backend does
not call provider models here; it watches `svg_output/` for live preview, then
finalizes and exports the deck after the agent completes.
"""


def _slide_number(path: Path, fallback: int) -> int:
    match = re.search(r"(\d+)", path.stem)
    if not match:
        return fallback
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return fallback


def _rel(base: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(base.resolve()).as_posix()
    except (OSError, ValueError):
        return str(target)


def _codex_completed_item_text(event: Any) -> str:
    item = _codex_event_item(event)
    if item is None:
        return ""
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "agentMessage":
        return str(getattr(item, "text", "") or "").strip()[:2000]
    if item_type:
        return f"Codex item completed: {item_type}"
    return ""
