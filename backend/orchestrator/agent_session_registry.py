"""Registry of active PersistentAgentSession instances.

The feedback endpoint uses this to route messages to a live session
instead of creating a new pipeline run.  This is intentionally
in-process only — no persistence; it only holds references to sessions
that are currently alive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.orchestrator.agent_pipeline import PersistentAgentSession

_active_sessions: dict[str, PersistentAgentSession] = {}


def register(job_id: str, session: PersistentAgentSession) -> None:
    _active_sessions[job_id] = session


def get(job_id: str) -> PersistentAgentSession | None:
    return _active_sessions.get(job_id)


def unregister(job_id: str) -> None:
    _active_sessions.pop(job_id, None)
