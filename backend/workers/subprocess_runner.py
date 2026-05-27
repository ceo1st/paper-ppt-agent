"""Synchronous runner used by the Huey consumer to supervise job children."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from backend.runtime.job_event_log import append_job_message
from backend.runtime.worker_process_registry import (
    register_job_process,
    terminate_job_process_tree,
    unregister_job_process,
)


logger = logging.getLogger(__name__)
_IS_WINDOWS = sys.platform.startswith("win")


def run_generation_subprocess(job_id: str, request_file: str, timeout: float | None = None) -> int:
    """Run the isolated generation worker and copy its stdout to the event log."""
    argv = [
        sys.executable,
        "-m",
        "backend.workers.generation_worker",
        "--request",
        request_file,
    ]
    creationflags = 0
    popen_kwargs: dict = {}
    if _IS_WINDOWS:
        creationflags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        argv,
        cwd=str(Path.cwd()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        env=env,
        **popen_kwargs,
    )
    register_job_process(job_id, proc.pid)
    append_job_message(job_id, {"type": "worker_started", "pid": proc.pid})
    stdout_thread = threading.Thread(target=_copy_stdout, args=(job_id, proc), daemon=True)
    stderr_thread = threading.Thread(target=_copy_stderr, args=(job_id, proc), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
    try:
        while True:
            returncode = proc.poll()
            if returncode is not None:
                stdout_thread.join(timeout=2.0)
                stderr_thread.join(timeout=2.0)
                append_job_message(job_id, {"type": "worker_exit", "returncode": returncode})
                return returncode
            if deadline is not None and time.monotonic() >= deadline:
                terminate_job_process_tree(job_id)
                append_job_message(
                    job_id,
                    {
                        "type": "worker_timeout",
                        "message": f"Job exceeded timeout of {timeout}s",
                    },
                )
                return -1
            time.sleep(0.25)
    finally:
        unregister_job_process(job_id)


def _copy_stdout(job_id: str, proc: subprocess.Popen[str]) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        text = line.strip()
        if text:
            append_job_message(job_id, {"type": "worker_stdout", "payload": text})


def _copy_stderr(job_id: str, proc: subprocess.Popen[str]) -> None:
    if proc.stderr is None:
        return
    for line in proc.stderr:
        text = line.rstrip()
        if text:
            logger.info("job %s worker: %s", job_id, text)
