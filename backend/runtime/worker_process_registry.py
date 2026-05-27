"""Small file-backed registry for active isolated job processes."""

from __future__ import annotations

import json
import time
from pathlib import Path

import psutil

from backend.config import settings


def _registry_dir() -> Path:
    path = settings.runtime_dir / "job_processes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def process_file(job_id: str) -> Path:
    return _registry_dir() / f"{job_id}.json"


def register_job_process(job_id: str, pid: int) -> None:
    process_file(job_id).write_text(
        json.dumps({"job_id": job_id, "pid": pid, "started_at": time.time()}, indent=2),
        encoding="utf-8",
    )


def unregister_job_process(job_id: str) -> None:
    try:
        process_file(job_id).unlink()
    except FileNotFoundError:
        pass


def get_job_process_pid(job_id: str) -> int | None:
    try:
        payload = json.loads(process_file(job_id).read_text(encoding="utf-8"))
        return int(payload["pid"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def is_job_process_alive(job_id: str) -> bool:
    pid = get_job_process_pid(job_id)
    return bool(pid and psutil.pid_exists(pid))


def terminate_job_process_tree(job_id: str) -> bool:
    pid = get_job_process_pid(job_id)
    if not pid:
        return False
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        unregister_job_process(job_id)
        return False

    children = parent.children(recursive=True)
    for proc in children:
        _terminate(proc)
    _terminate(parent)
    _, alive = psutil.wait_procs([parent, *children], timeout=5.0)
    for proc in alive:
        _kill(proc)
    if alive:
        psutil.wait_procs(alive, timeout=2.0)
    unregister_job_process(job_id)
    return True


def _terminate(proc: psutil.Process) -> None:
    try:
        proc.terminate()
    except psutil.Error:
        pass


def _kill(proc: psutil.Process) -> None:
    try:
        proc.kill()
    except psutil.Error:
        pass
