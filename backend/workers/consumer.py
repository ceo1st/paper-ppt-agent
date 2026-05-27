"""Run the local Huey consumer."""

from __future__ import annotations

import logging

from huey.consumer import Consumer

from backend.config import settings
from backend.queue.broker import huey
import backend.queue.tasks  # noqa: F401 - task registration side effect


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    consumer = Consumer(
        huey,
        workers=1,
        worker_type="thread",
        periodic=False,
    )
    consumer.run()


if __name__ == "__main__":
    main()
