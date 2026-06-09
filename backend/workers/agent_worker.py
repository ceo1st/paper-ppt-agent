"""Isolated agent pipeline worker.

Similar to generation_worker.py but for agent-mode generation.
Runs as a separate process so agent tasks do not block the API event loop.
Progress is streamed back as JSON Lines on stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.runtime.offload import init_offload, shutdown_offload


logger = logging.getLogger(__name__)


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "stage": event.stage,
        "status": event.status,
        "message": event.message,
        "progress": event.progress,
        "data": event.data,
    }


async def _run_agent(payload_path: Path) -> int:
    from backend.orchestrator.agent_pipeline import run_agent_pipeline
    from backend.orchestrator.pipeline import ProgressEvent
    from backend.session.progress import describe_exception
    from backend.usage.tracker import set_usage_context

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    job_id = str(payload["job_id"])
    set_usage_context(job_id=job_id)

    project_dir = Path(payload["project_dir"])
    total_slides_hint = int(payload.get("total_slides_hint", 0))
    session_id = payload.get("session_id")
    agent_config = payload.get("agent_config") or {}

    try:
        async for event in run_agent_pipeline(
            project_dir=project_dir,
            total_slides_hint=total_slides_hint,
            session_id=session_id,
            job_id=job_id,
            **agent_config,
        ):
            _emit({"type": "event", "event": _event_payload(event)})
        _emit({"type": "done"})
        return 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("agent worker failed for job %s", job_id)
        _emit(
            {
                "type": "event",
                "event": _event_payload(
                    ProgressEvent(
                        "error",
                        "error",
                        describe_exception(exc, "Agent generation failed"),
                        0.0,
                    )
                ),
            }
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stderr,
    )
    init_offload(settings.io_pool_workers)
    try:
        return asyncio.run(_run_agent(args.request))
    finally:
        shutdown_offload(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
