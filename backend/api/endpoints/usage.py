"""Token usage endpoints.

Exposes aggregated views over :mod:`backend.usage.tracker` for the
frontend Logs page, plus a WebSocket that streams every new usage record
as it is recorded.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from backend.usage.tracker import UsageEvent, usage_tracker

router = APIRouter(prefix="/usage")


def _summary_dict(s: Any) -> dict[str, Any]:
    return {
        "total_calls": s.total_calls,
        "total_prompt": s.total_prompt,
        "total_completion": s.total_completion,
        "total_tokens": s.total_tokens,
    }


@router.get("/summary")
async def usage_summary() -> dict[str, Any]:
    """Return global totals across all recorded LLM calls."""
    return _summary_dict(usage_tracker.summary())


@router.get("/daily")
async def usage_daily(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Return per-day rollups, most recent first."""
    return {"rows": usage_tracker.daily_series(days=days)}


@router.get("/by-model")
async def usage_by_model() -> dict[str, Any]:
    """Return token usage grouped by model, sorted by total tokens."""
    return {"rows": usage_tracker.per_model()}


@router.get("/by-job")
async def usage_by_job(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Return token usage grouped by job_id."""
    return {"rows": usage_tracker.per_job(limit=limit)}


@router.get("/by-stage")
async def usage_by_stage() -> dict[str, Any]:
    return {"rows": usage_tracker.per_stage()}


@router.get("/records")
async def usage_records(
    job_id: str | None = None,
    stage: str | None = None,
    model: str | None = None,
    page: int | None = Query(None, ge=1),
    start: str | None = None,
    end: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Return a filtered page of raw per-call records, newest first."""
    start_ts = _normalize_filter_timestamp(start, end_of_range=False)
    end_ts = _normalize_filter_timestamp(end, end_of_range=True)
    if start_ts and end_ts and start_ts > end_ts:
        raise HTTPException(status_code=400, detail="start must not be after end")
    recs, total = usage_tracker.query_records(
        job_id_prefix=(job_id or "").strip() or None,
        stage=(stage or "").strip() or None,
        model=(model or "").strip() or None,
        page=page,
        start_ts=start_ts,
        end_ts=end_ts,
        offset=offset,
        limit=limit,
    )
    return {
        "rows": [record.to_dict() for record in recs],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _normalize_filter_timestamp(value: str | None, *, end_of_range: bool) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if end_of_range and len(normalized) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed.isoformat(timespec="seconds")


@router.websocket("/stream")
async def usage_stream(websocket: WebSocket) -> None:
    """Push each new usage record to the client in real time."""
    await websocket.accept()
    queue: asyncio.Queue[UsageEvent] = usage_tracker.subscribe()
    try:
        # Initial snapshot so clients don't start empty.
        await websocket.send_json({
            "type": "snapshot",
            "summary": _summary_dict(usage_tracker.summary()),
            "by_model": usage_tracker.per_model(),
            "by_stage": usage_tracker.per_stage(),
            "daily": usage_tracker.daily_series(days=30),
            "recent": [
                r.to_dict() for r in usage_tracker.all_records(limit=50)
            ],
        })
        while True:
            event = await queue.get()
            await websocket.send_json({
                "type": event.type,
                "record": event.record.to_dict(),
            })
    except WebSocketDisconnect:
        pass
    finally:
        usage_tracker.unsubscribe(queue)
