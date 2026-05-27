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

    async def fake_aoffload(_fn, _job_id, _offset):
        return 10, [{"type": "worker_stdout", "payload": json.dumps(worker_payload)}]

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

    async def fake_aoffload(_fn, _job_id, _offset):
        return 10, [{"type": "worker_exit", "returncode": 7}]

    monkeypatch.setattr(job_event_monitor, "session_manager", manager)
    monkeypatch.setattr(job_event_monitor, "aoffload", fake_aoffload)
    job_event_monitor._offsets.pop("job-2", None)

    await job_event_monitor.sync_job_events_once("job-2")

    assert job.status == "error"
    assert "exited with code 7" in job.error
