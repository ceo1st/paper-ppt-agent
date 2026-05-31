from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api.endpoints import generate
from backend.orchestrator import agent_session_registry


class _SessionManager:
    def __init__(self, job: SimpleNamespace) -> None:
        self.job = job
        self.cancelled = False
        self.events: list[dict] = []

    def get_job(self, _job_id: str) -> SimpleNamespace:
        return self.job

    def record_event(self, _job_id: str, payload: dict, **updates: object) -> None:
        self.events.append(payload)
        for key, value in updates.items():
            setattr(self.job, key, value)

    def cancel_job(self, _job_id: str) -> bool:
        self.cancelled = True
        return True

    def mark_job_cancelled(self, _job_id: str, message: str = "Job cancelled") -> None:
        self.job.status = "cancelled"
        self.job.message = message


class _LiveSession:
    def __init__(self) -> None:
        self.interrupt_calls = 0
        self.closed = False
        self.messages: list[tuple[str, bool]] = []
        self.can_accept_feedback = True

    async def interrupt(self) -> None:
        self.interrupt_calls += 1

    async def close(self) -> None:
        self.closed = True

    async def send_message(self, text: str, *, interrupt_current: bool = False) -> None:
        self.messages.append((text, interrupt_current))


def _job(tmp_path, status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="job-1",
        status=status,
        provider="agent:claude_code",
        project_dir=str(tmp_path),
        progress=0.12,
        slides_completed=0,
        total_slides=0,
        error=None,
    )


@pytest.mark.asyncio
async def test_agent_interrupt_cancels_running_agent_job(monkeypatch, tmp_path) -> None:
    manager = _SessionManager(_job(tmp_path, "generation"))
    live = _LiveSession()
    monkeypatch.setattr(generate, "session_manager", manager)
    monkeypatch.setattr(agent_session_registry, "get", lambda _job_id: live)

    response = await generate.interrupt_agent_generation("job-1")

    assert response.status == "cancelled"
    assert manager.job.status == "cancelled"
    assert live.closed
    assert manager.cancelled


@pytest.mark.asyncio
async def test_second_stop_while_agent_paused_cancels_job(monkeypatch, tmp_path) -> None:
    manager = _SessionManager(_job(tmp_path, "paused"))
    live = _LiveSession()
    monkeypatch.setattr(generate, "session_manager", manager)
    monkeypatch.setattr(agent_session_registry, "get", lambda _job_id: live)

    response = await generate.interrupt_agent_generation("job-1")

    assert response.status == "cancelled"
    assert manager.job.status == "cancelled"
    assert manager.cancelled


@pytest.mark.asyncio
async def test_feedback_resumes_paused_agent(monkeypatch, tmp_path) -> None:
    manager = _SessionManager(_job(tmp_path, "paused"))
    live = _LiveSession()
    monkeypatch.setattr(generate, "session_manager", manager)
    monkeypatch.setattr(agent_session_registry, "get", lambda _job_id: live)

    response = await generate.send_agent_generation_feedback(
        "job-1",
        generate.AgentFeedbackRequest(message="continue with the research"),
    )

    assert response.status == "injected"
    assert manager.job.status == "generation"
    assert live.messages == [("continue with the research", False)]
    assert any(event.get("data", {}).get("agent_resumed") for event in manager.events)


@pytest.mark.asyncio
async def test_feedback_rejects_live_session_that_no_longer_accepts_messages(monkeypatch, tmp_path) -> None:
    manager = _SessionManager(_job(tmp_path, "export"))
    live = _LiveSession()
    live.can_accept_feedback = False
    monkeypatch.setattr(generate, "session_manager", manager)
    monkeypatch.setattr(agent_session_registry, "get", lambda _job_id: live)

    with pytest.raises(generate.HTTPException) as exc:
        await generate.send_agent_generation_feedback(
            "job-1",
            generate.AgentFeedbackRequest(message="change the ending"),
        )

    assert exc.value.status_code == 409
    assert not manager.events
