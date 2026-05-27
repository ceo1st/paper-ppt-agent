"""Huey tasks for background generation."""

from __future__ import annotations

import json
import logging

from backend.config import settings
from backend.queue.broker import huey
from backend.workers.subprocess_runner import run_generation_subprocess


logger = logging.getLogger(__name__)


def _persisted_job_status(job_id: str) -> str | None:
    """Read current API-owned state, avoiding stale consumer process memory."""
    try:
        payload = json.loads(
            (settings.runtime_dir / "session_state.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    for job in payload.get("jobs", []):
        if isinstance(job, dict) and str(job.get("id")) == job_id:
            return str(job.get("status") or "")
    return None


@huey.task()
def run_generation_job(job_id: str, request_file: str, timeout: float | None = None) -> int:
    current_status = _persisted_job_status(job_id)
    if current_status in {"complete", "error", "cancelled"}:
        logger.info("skipping terminal generation job %s with status %s", job_id, current_status)
        return 0
    logger.info("dequeued generation job %s", job_id)
    return run_generation_subprocess(job_id, request_file, timeout=timeout)
