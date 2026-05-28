from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api.endpoints import status
from backend.runtime import job_event_monitor


class _EventsManager:
    def __init__(self) -> None:
        self.job = SimpleNamespace(last_seq=3)
        self.events = [
            {"seq": 1, "stage": "agent", "message": "first"},
            {"seq": 2, "stage": "agent", "message": "second"},
            {"seq": 3, "stage": "export", "message": "done"},
        ]

    def get_job(self, job_id: str):
        return self.job if job_id == "job-1" else None

    def get_events_after(self, _job_id: str, since_seq: int) -> list[dict]:
        return [event for event in self.events if int(event["seq"]) > since_seq]


@pytest.mark.asyncio
async def test_job_events_endpoint_returns_retained_events(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop_sync(_job_id: str) -> None:
        return None

    monkeypatch.setattr(status, "session_manager", _EventsManager())
    monkeypatch.setattr(job_event_monitor, "sync_job_events_once", _noop_sync)

    payload = await status.get_job_events("job-1", since_seq=1)

    assert payload["job_id"] == "job-1"
    assert payload["last_seq"] == 3
    assert [event["seq"] for event in payload["events"]] == [2, 3]
