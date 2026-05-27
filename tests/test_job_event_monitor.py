from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.runtime import job_event_monitor


class _Manager:
    def __init__(self, job: SimpleNamespace) -> None:
        self.job = job
        self.events: list[dict] = []

    def get_job(self, _job_id: str) -> SimpleNamespace:
        return self.job

    def record_event(self, _job_id: str, payload: dict, **updates: object) -> None:
        self.events.append(payload)
        for key, value in updates.items():
            setattr(self.job, key, value)


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        status="pending",
        progress=0.0,
        message="",
        slides_completed=0,
        total_slides=0,
        output_path=None,
        project_dir=None,
        worker_backend="huey-sqlite",
    )


@pytest.mark.asyncio
async def test_sync_job_events_applies_worker_progress(monkeypatch) -> None:
    job = _job()
    manager = _Manager(job)
    worker_payload = {
        "type": "event",
        "event": {
            "stage": "parsing",
            "status": "started",
            "message": "Parsing paper...",
            "progress": 0.1,
            "data": {"project_dir": "workspace-a"},
        },
    }

    async def fake_aoffload(fn, *args):
        if getattr(fn, "__name__", "") == "read_messages_after":
            return 10, [{"type": "worker_stdout", "payload": json.dumps(worker_payload)}]
        if getattr(fn, "__name__", "") == "has_job_process_record":
            return True
        return False

    monkeypatch.setattr(job_event_monitor, "session_manager", manager)
    monkeypatch.setattr(job_event_monitor, "aoffload", fake_aoffload)
    job_event_monitor._offsets.pop("job-1", None)

    await job_event_monitor.sync_job_events_once("job-1")

    assert job.status == "parsing"
    assert job.progress == 0.1
    assert job.project_dir == "workspace-a"
    assert manager.events[0]["message"] == "Parsing paper..."


@pytest.mark.asyncio
async def test_sync_job_events_marks_worker_exit_error(monkeypatch) -> None:
    job = _job()
    manager = _Manager(job)

    async def fake_aoffload(fn, *args):
        if getattr(fn, "__name__", "") == "read_messages_after":
            return 10, [{"type": "worker_exit", "returncode": 7}]
        return False

    monkeypatch.setattr(job_event_monitor, "session_manager", manager)
    monkeypatch.setattr(job_event_monitor, "aoffload", fake_aoffload)
    job_event_monitor._offsets.pop("job-2", None)

    await job_event_monitor.sync_job_events_once("job-2")

    assert job.status == "error"
    assert "exited with code 7" in job.error


@pytest.mark.asyncio
async def test_sync_job_events_marks_lost_worker_error(monkeypatch) -> None:
    job = _job()
    job.status = "research"
    job.progress = 0.24
    manager = _Manager(job)

    async def fake_aoffload(fn, *args):
        if getattr(fn, "__name__", "") == "read_messages_after":
            return 10, []
        if getattr(fn, "__name__", "") == "is_job_process_lost":
            return True
        return None

    monkeypatch.setattr(job_event_monitor, "session_manager", manager)
    monkeypatch.setattr(job_event_monitor, "aoffload", fake_aoffload)
    job_event_monitor._offsets.pop("job-3", None)

    await job_event_monitor.sync_job_events_once("job-3")

    assert job.status == "error"
    assert "worker process" in job.error


@pytest.mark.asyncio
async def test_sync_job_events_marks_missing_worker_record_error(monkeypatch) -> None:
    job = _job()
    job.status = "research"
    manager = _Manager(job)

    async def fake_aoffload(fn, *args):
        if getattr(fn, "__name__", "") == "read_messages_after":
            return 10, []
        if getattr(fn, "__name__", "") == "has_job_process_record":
            return False
        if getattr(fn, "__name__", "") == "is_job_process_lost":
            return False
        return None

    monkeypatch.setattr(job_event_monitor, "session_manager", manager)
    monkeypatch.setattr(job_event_monitor, "aoffload", fake_aoffload)
    job_event_monitor._offsets.pop("job-4", None)

    await job_event_monitor.sync_job_events_once("job-4")

    assert job.status == "error"
    assert "worker process" in job.error
