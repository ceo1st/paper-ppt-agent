"""Serialize generation requests for isolated workers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def serialize_generation_request(request: Any) -> dict[str, Any]:
    """Return a JSON-serializable GenerationRequest payload."""
    if is_dataclass(request):
        raw = asdict(request)
    else:
        raw = dict(vars(request))

    for key, value in list(raw.items()):
        if isinstance(value, Path):
            raw[key] = str(value)
        elif hasattr(value, "model_dump"):
            raw[key] = value.model_dump(exclude_none=True)
    return raw
