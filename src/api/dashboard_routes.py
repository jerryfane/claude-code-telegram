"""Dashboard API endpoints for the web terminal.

Provides REST endpoints for historical data and an SSE endpoint
for live agent activity streaming.
"""

import asyncio
import json
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..config.settings import Settings
from ..events.bus import Event, EventBus
from ..events.types import DashboardStreamEvent
from ..storage.database import DatabaseManager

logger = structlog.get_logger()

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Module-level references injected at mount time
_settings: Optional[Settings] = None
_event_bus: Optional[EventBus] = None
_db_manager: Optional[DatabaseManager] = None


def configure(
    settings: Settings,
    event_bus: EventBus,
    db_manager: Optional[DatabaseManager] = None,
) -> None:
    """Inject dependencies into the dashboard module."""
    global _settings, _event_bus, _db_manager
    _settings = settings
    _event_bus = event_bus
    _db_manager = db_manager


def _verify_token(authorization: Optional[str] = Header(None)) -> None:
    """Verify dashboard Bearer token."""
    if not _settings:
        raise HTTPException(status_code=500, detail="Dashboard not configured")

    secret = _settings.resolved_dashboard_secret
    if not secret:
        # No secret configured — allow open access (dev mode)
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization[len("Bearer "):]
    if token != secret:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(
    user_id: Optional[int] = Query(None),
    active: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _auth: None = Depends(_verify_token),
) -> List[Dict[str, Any]]:
    """List sessions with optional filters."""
    if not _db_manager:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with _db_manager.get_connection() as conn:
        query = "SELECT * FROM sessions WHERE 1=1"
        params: List[Any] = []

        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if active is not None:
            query += " AND is_active = ?"
            params.append(active)

        query += " ORDER BY last_used DESC LIMIT ?"
        params.append(limit)

        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            for key in ("created_at", "last_used"):
                val = d.get(key)
                if isinstance(val, datetime):
                    d[key] = val.isoformat()
            results.append(d)
        return results


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(100, ge=1, le=1000),
    _auth: None = Depends(_verify_token),
) -> List[Dict[str, Any]]:
    """Get messages for a specific session."""
    if not _db_manager:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with _db_manager.get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM messages
            WHERE session_id = ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            ts = d.get("timestamp")
            if isinstance(ts, datetime):
                d["timestamp"] = ts.isoformat()
            results.append(d)
        return results


@router.get("/tool-usage")
async def get_tool_usage(
    session_id: Optional[str] = Query(None),
    tool_name: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _auth: None = Depends(_verify_token),
) -> List[Dict[str, Any]]:
    """Get tool usage records with optional filters."""
    if not _db_manager:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with _db_manager.get_connection() as conn:
        query = "SELECT * FROM tool_usage WHERE 1=1"
        params: List[Any] = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if tool_name:
            query += " AND tool_name = ?"
            params.append(tool_name)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            ts = d.get("timestamp")
            if isinstance(ts, datetime):
                d["timestamp"] = ts.isoformat()
            # Parse tool_input JSON string
            ti = d.get("tool_input")
            if ti and isinstance(ti, str):
                try:
                    d["tool_input"] = json.loads(ti)
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results


@router.get("/stats")
async def get_stats(
    days: int = Query(30, ge=1, le=365),
    _auth: None = Depends(_verify_token),
) -> Dict[str, Any]:
    """Get dashboard statistics."""
    if not _db_manager:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with _db_manager.get_connection() as conn:
        # Daily stats from view
        cursor = await conn.execute(
            """
            SELECT * FROM daily_stats
            WHERE date >= date('now', '-' || ? || ' days')
            ORDER BY date DESC
            """,
            (days,),
        )
        daily = [dict(row) for row in await cursor.fetchall()]

        # Overall summary
        cursor = await conn.execute(
            """
            SELECT
                COUNT(DISTINCT user_id) as total_users,
                COUNT(DISTINCT session_id) as total_sessions,
                COUNT(*) as total_messages,
                COALESCE(SUM(cost), 0) as total_cost
            FROM messages
            """
        )
        summary = dict(await cursor.fetchone())

        # Active sessions
        cursor = await conn.execute(
            "SELECT COUNT(*) as count FROM sessions WHERE is_active = TRUE"
        )
        summary["active_sessions"] = (await cursor.fetchone())[0]

        # Tool breakdown
        cursor = await conn.execute(
            """
            SELECT tool_name, COUNT(*) as count
            FROM tool_usage
            WHERE timestamp >= datetime('now', '-' || ? || ' days')
            GROUP BY tool_name
            ORDER BY count DESC
            LIMIT 20
            """,
            (days,),
        )
        tool_stats = [dict(row) for row in await cursor.fetchall()]

        return {
            "summary": summary,
            "daily": daily,
            "tool_stats": tool_stats,
        }


# ---------------------------------------------------------------------------
# SSE live stream
# ---------------------------------------------------------------------------


@router.get("/stream")
async def stream_events(
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
) -> StreamingResponse:
    """Server-Sent Events stream of live agent activity.

    Accepts auth via Bearer header or ?token= query param (for EventSource).
    """
    # Verify auth: query param takes precedence for SSE (EventSource can't set headers)
    if _settings:
        secret = _settings.resolved_dashboard_secret
        if secret:
            effective_token = token or (
                authorization[len("Bearer "):] if authorization and authorization.startswith("Bearer ") else None
            )
            if effective_token != secret:
                raise HTTPException(status_code=401, detail="Invalid token")

    if not _event_bus:
        raise HTTPException(status_code=503, detail="Event bus unavailable")

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _sse_generator() -> AsyncGenerator[str, None]:
    """Yield SSE-formatted DashboardStreamEvents from the EventBus."""
    queue: asyncio.Queue[DashboardStreamEvent] = asyncio.Queue(maxsize=256)

    async def _enqueue(event: Event) -> None:
        if isinstance(event, DashboardStreamEvent):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop oldest if consumer is too slow

    # Subscribe to dashboard events
    if _event_bus:
        _event_bus.subscribe(DashboardStreamEvent, _enqueue)

    try:
        # Send initial keepalive
        yield ": connected\n\n"

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                data = {
                    "id": event.id,
                    "timestamp": event.timestamp.isoformat(),
                    "kind": event.event_kind,
                    "session_id": event.session_id,
                    "user_id": event.user_id,
                    "content": event.content,
                    "tool_name": event.tool_name,
                    "tool_input": event.tool_input,
                }
                yield f"data: {json.dumps(data)}\n\n"
            except asyncio.TimeoutError:
                # Send keepalive comment every 15s
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break
    finally:
        # Unsubscribe — remove our handler from the bus
        if _event_bus and DashboardStreamEvent in _event_bus._handlers:
            try:
                _event_bus._handlers[DashboardStreamEvent].remove(_enqueue)
            except ValueError:
                pass
