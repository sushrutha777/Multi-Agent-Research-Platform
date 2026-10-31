import asyncio
import pytest

from app.streaming import (
    emit_event,
    get_job_events,
    stream_job_events,
    _format_event,
)


@pytest.mark.asyncio
async def test_format_and_emit_event_in_memory():
    job_id = "test-job-123"
    event = await emit_event(
        redis=None,
        job_id=job_id,
        event_type="status",
        step="planning",
        title="Formulating strategy",
        details={"tools": ["tavily", "wikipedia"]},
    )

    assert event["job_id"] == job_id
    assert event["type"] == "status"
    assert event["step"] == "planning"
    assert event["title"] == "Formulating strategy"
    assert event["details"]["tools"] == ["tavily", "wikipedia"]
    assert "timestamp" in event

    # Check history retrieval
    history = await get_job_events(redis=None, job_id=job_id)
    assert len(history) >= 1
    assert any(e["title"] == "Formulating strategy" for e in history)


@pytest.mark.asyncio
async def test_stream_job_events_generator():
    job_id = "stream-job-456"

    # Pre-emit an event
    await emit_event(
        redis=None,
        job_id=job_id,
        event_type="status",
        step="queued",
        title="Job initialized",
    )

    # Emit terminal event in background
    async def emit_done():
        await asyncio.sleep(0.05)
        await emit_event(
            redis=None,
            job_id=job_id,
            event_type="completed",
            step="finalizing",
            title="Completed",
            details={"report": "Done"},
        )

    task = asyncio.create_task(emit_done())

    received = []
    async for ev in stream_job_events(redis=None, job_id=job_id):
        received.append(ev)
        if ev.get("type") == "completed":
            break

    await task
    assert len(received) >= 2
    assert received[0]["type"] == "status"
    assert received[-1]["type"] == "completed"
