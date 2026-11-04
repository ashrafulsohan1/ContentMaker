"""SSE live progress stream (BUILD_PLAN 1.11) — ``GET /api/jobs/{id}/events``.

Server-Sent Events per spec §6.6: the job monitor page attaches to the running
job, and every event carries the FULL job payload (the same JSON as
``/api/jobs/{id}`` plus a progress summary), so any client that connects —
first open, page reload, or EventSource auto-reconnect — renders
truth-as-of-now and continues from there. No client-side catch-up logic.

Event names:
- ``snapshot`` — the first event on every connection;
- ``update``   — every persisted change (job status, asset rows, spend, progress);
- ``final``    — the job reached a terminal status; the stream closes after it.
  A job that is already terminal at connect time yields ``snapshot`` then
  ``final`` immediately, so finished jobs never hold connections open.

The stream polls the job store (interval from ``app.state.sse_poll_s``, 0.5 s
by default — injectable for tests) and only emits when the payload changed; a
``: keep-alive`` comment every ~15 s of silence keeps intermediaries from
buffering the connection away. The job is resolved before the stream opens, so
an unknown id is a plain 404 rather than a broken event stream.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.jobs import engine_of, job_payload
from app.core.jobs import TERMINAL_JOB_STATUSES, JobEngine, JobNotFound

router = APIRouter(prefix="/api")

DEFAULT_POLL_S = 0.5
HEARTBEAT_S = 15.0
RETRY_MS = 3000  # EventSource reconnect hint (only used while a job is live)


def poll_s_of(request: Request) -> float:
    """The app-configured poll interval; falls back to the default on junk."""
    raw = getattr(request.app.state, "sse_poll_s", DEFAULT_POLL_S)
    try:
        poll_s = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_S
    return poll_s if poll_s > 0 else DEFAULT_POLL_S


def _event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def job_event_stream(
    engine: JobEngine, job_id: str, poll_s: float
) -> AsyncIterator[str]:
    """Full-snapshot SSE stream; ends after the job reaches a terminal status."""
    last_payload: dict[str, Any] | None = None
    last_yield = time.monotonic()
    heartbeat_after = max(poll_s, HEARTBEAT_S)
    while True:
        job = engine.job(job_id)  # JobNotFound impossible: resolved pre-stream
        payload = job_payload(job)
        if payload != last_payload:
            if job.status in TERMINAL_JOB_STATUSES:
                if last_payload is None:
                    yield f"retry: {RETRY_MS}\n\n" + _event("snapshot", payload)
                yield _event("final", payload)
                return
            prefix = f"retry: {RETRY_MS}\n\n" if last_payload is None else ""
            yield prefix + _event("snapshot" if last_payload is None else "update", payload)
            last_payload = payload
            last_yield = time.monotonic()
        elif time.monotonic() - last_yield >= heartbeat_after:
            yield ": keep-alive\n\n"
            last_yield = time.monotonic()
        await asyncio.sleep(poll_s)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    engine = engine_of(request)
    try:
        engine.job(job_id)  # resolve before the stream opens: unknown id → 404
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    return StreamingResponse(
        job_event_stream(engine, job_id, poll_s_of(request)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
