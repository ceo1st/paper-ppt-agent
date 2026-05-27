"""Huey tasks for background generation."""

from __future__ import annotations

import logging

from backend.queue.broker import huey
from backend.workers.subprocess_runner import run_generation_subprocess


logger = logging.getLogger(__name__)


@huey.task()
def run_generation_job(job_id: str, request_file: str, timeout: float | None = None) -> int:
    logger.info("dequeued generation job %s", job_id)
    return run_generation_subprocess(job_id, request_file, timeout=timeout)
