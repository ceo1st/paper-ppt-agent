"""Durable per-job event log shared by the API and Huey worker."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from backend.config import settings


def event_log_dir() -> Path:
    path = settings.runtime_dir / "job_events"
    path.mkdir(parents=True, exist_ok=True)
    return path


def event_log_path(job_id: str) -> Path:
    return event_log_dir() / f"{job_id}.jsonl"


def append_job_message(job_id: str, message: dict[str, Any]) -> None:
    payload = dict(message)
    payload.setdefault("job_id", job_id)
    payload.setdefault("logged_at", time.time())
    path = event_log_path(job_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        handle.flush()


def read_messages_after(job_id: str, offset: int) -> tuple[int, list[dict[str, Any]]]:
    path = event_log_path(job_id)
    if not path.exists():
        return offset, []
    messages: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                messages.append(message)
        return handle.tell(), messages
