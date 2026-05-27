"""Huey broker configuration.

The default broker is SQLite so source checkouts work on Windows/Linux without
Docker or Redis. The worker is still a separate process from FastAPI.
"""

from __future__ import annotations

from huey import SqliteHuey

from backend.config import settings


settings.runtime_dir.mkdir(parents=True, exist_ok=True)

huey = SqliteHuey(
    "paper-ppt-agent",
    filename=str(settings.runtime_dir / "jobs.sqlite"),
    results=False,
    immediate=False,
)
