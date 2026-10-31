from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# In-memory fallback for local/test environments or when Redis pubsub is unavailable
_memory_history: dict[str, list[dict[str, Any]]] = {}
_memory_subscribers: dict[str, set[asyncio.Queue]] = {}
_memory_lock = asyncio.Lock()


def _format_event(
    job_id: str,
    event_type: str,
    step: str,
    title: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "type": event_type,
        "step": step,
        "title": title,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def emit_event(
    redis: aioredis.Redis | None,
    job_id: str,
    event_type: str,
    step: str,
    title: str,
    details: dict[str, Any] | None = None,
    ttl: int = 3600,
) -> dict[str, Any]:
    """Publish an agent execution event to Redis PubSub and cache in history."""
    event = _format_event(job_id, event_type, step, title, details)
    payload = json.dumps(event)

    # 1. Update in-memory history and active subscribers
    async with _memory_lock:
        if job_id not in _memory_history:
            _memory_history[job_id] = []
        _memory_history[job_id].append(event)
        # Cap in-memory history to last 100 events per job
        if len(_memory_history[job_id]) > 100:
            _memory_history[job_id] = _memory_history[job_id][-100:]

        if job_id in _memory_subscribers:
            for queue in list(_memory_subscribers[job_id]):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    # 2. Publish to Redis if connected
    if redis is not None:
        try:
            channel = f"job:stream:{job_id}"
            history_key = f"job:events:{job_id}"
            pipe = redis.pipeline()
            pipe.rpush(history_key, payload)
            pipe.expire(history_key, ttl)
            pipe.publish(channel, payload)
            await pipe.execute()
        except Exception as exc:
            logger.warning("Failed to publish event to Redis for job %s: %s", job_id, exc)

    return event


async def get_job_events(redis: aioredis.Redis | None, job_id: str) -> list[dict[str, Any]]:
    """Retrieve recorded events for a job."""
    if redis is not None:
        try:
            history_key = f"job:events:{job_id}"
            raw_events = await redis.lrange(history_key, 0, -1)
            if raw_events:
                return [json.loads(e) for e in raw_events]
        except Exception as exc:
            logger.warning("Failed to read events from Redis for job %s: %s", job_id, exc)

    async with _memory_lock:
        return list(_memory_history.get(job_id, []))


async def stream_job_events(
    redis: aioredis.Redis | None,
    job_id: str,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield past events and then live events until job finishes."""
    # 1. Yield already recorded events
    past_events = await get_job_events(redis, job_id)
    seen_ids = set()
    for ev in past_events:
        ev_key = (ev.get("type"), ev.get("step"), ev.get("title"), ev.get("timestamp"))
        seen_ids.add(ev_key)
        yield ev
        if ev.get("type") in ("completed", "error", "blocked"):
            return

    # 2. Setup subscription for live events
    local_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    async with _memory_lock:
        if job_id not in _memory_subscribers:
            _memory_subscribers[job_id] = set()
        _memory_subscribers[job_id].add(local_queue)

    pubsub = None
    pubsub_task = None
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(f"job:stream:{job_id}")

            async def _reader():
                try:
                    async for message in pubsub.listen():
                        if message and message.get("type") == "message":
                            try:
                                data = json.loads(message["data"])
                                await local_queue.put(data)
                            except Exception:
                                pass
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug("Pubsub reader ended: %s", e)

            pubsub_task = asyncio.create_task(_reader())
        except Exception as exc:
            logger.warning("Could not subscribe to Redis pubsub for job %s: %s", job_id, exc)

    try:
        while True:
            try:
                event = await asyncio.wait_for(local_queue.get(), timeout=35.0)
                ev_key = (event.get("type"), event.get("step"), event.get("title"), event.get("timestamp"))
                if ev_key not in seen_ids:
                    seen_ids.add(ev_key)
                    yield event

                if event.get("type") in ("completed", "error", "blocked"):
                    break
            except asyncio.TimeoutError:
                # Send ping/heartbeat to keep connection alive
                yield {"job_id": job_id, "type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}
    finally:
        async with _memory_lock:
            if job_id in _memory_subscribers:
                _memory_subscribers[job_id].discard(local_queue)
                if not _memory_subscribers[job_id]:
                    del _memory_subscribers[job_id]

        if pubsub_task:
            pubsub_task.cancel()
        if pubsub:
            try:
                await pubsub.unsubscribe(f"job:stream:{job_id}")
                await pubsub.aclose()
            except Exception:
                pass


async def handle_websocket_stream(websocket: WebSocket, job_id: str, redis: aioredis.Redis | None) -> None:
    """Handle a WebSocket client connection for a specific job."""
    await websocket.accept()
    logger.info("WebSocket connected for job %s", job_id)
    try:
        async for event in stream_job_events(redis, job_id):
            await websocket.send_text(json.dumps(event))
            if event.get("type") in ("completed", "error", "blocked"):
                # Job has completed or failed, close smoothly
                await asyncio.sleep(0.5)
                break
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected for job %s", job_id)
    except Exception as exc:
        logger.warning("WebSocket error for job %s: %s", job_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
