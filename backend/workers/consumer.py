"""Run the local Huey consumer."""

from __future__ import annotations

import logging
import os

from huey.consumer import Consumer

from backend.config import settings
from backend.queue.broker import huey
import backend.queue.tasks  # noqa: F401 - task registration side effect


def _auto_worker_count() -> int:
    cpu_count = os.cpu_count() or 2
    cpu_budget = 1 if cpu_count < 8 else 2 if cpu_count < 16 else 3
    memory_budget = 2
    try:
        import psutil

        total_gb = psutil.virtual_memory().total / (1024 ** 3)
        if total_gb < 12:
            memory_budget = 1
        elif total_gb >= 32:
            memory_budget = 3
    except Exception:
        memory_budget = 1
    return max(1, min(cpu_budget, memory_budget, 4))


def worker_count() -> int:
    configured = int(settings.generation_worker_concurrency or 0)
    if configured > 0:
        return max(1, min(configured, 8))
    return _auto_worker_count()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    workers = worker_count()
    logging.info("starting Huey consumer with %s worker slot(s)", workers)
    consumer = Consumer(
        huey,
        workers=workers,
        worker_type="thread",
        periodic=False,
    )
    consumer.run()


if __name__ == "__main__":
    main()
