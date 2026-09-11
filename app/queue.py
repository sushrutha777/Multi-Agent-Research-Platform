from __future__ import annotations

import json
import uuid

import redis.asyncio as aioredis

from app.config import Config


async def push_job(
    redis: aioredis.Redis,
    config: Config,
    topic: str,
    session_id: str,
    output_format: str,
    *,
    job_id: str | None = None,
    attempt: int = 0,
) -> str:
    job_id = job_id or str(uuid.uuid4())
    await redis.xadd(
        config.stream_key,
        {
            "job_id": job_id,
            "topic": topic,
            "session_id": session_id,
            "output_format": output_format,
            "attempt": str(attempt),
        },
    )
    await set_result(redis, config, job_id, {"status": "queued", "job_id": job_id})
    return job_id


async def get_result(redis: aioredis.Redis, config: Config, job_id: str) -> dict | None:
    data = await redis.get(f"result:{job_id}")
    return json.loads(data) if data else None


async def set_result(redis: aioredis.Redis, config: Config, job_id: str, result: dict) -> None:
    await redis.setex(f"result:{job_id}", config.result_ttl, json.dumps(result))


async def ensure_group(redis: aioredis.Redis, config: Config) -> None:
    try:
        await redis.xgroup_create(config.stream_key, config.consumer_group, id="0", mkstream=True)
    except Exception:
        # BUSYGROUP is expected when another task created the group first.
        pass


async def consume_jobs(redis: aioredis.Redis, config: Config, count: int = 1) -> list[dict]:
    messages = await redis.xreadgroup(
        config.consumer_group,
        config.consumer_name,
        {config.stream_key: ">"},
        count=count,
        block=5000,
    )
    if not messages:
        return []
    jobs = []
    for _, entries in messages:
        for msg_id, data in entries:
            jobs.append({"msg_id": msg_id, "data": data})
    return jobs


async def recover_abandoned(redis: aioredis.Redis, config: Config, count: int = 10) -> list[dict]:
    """Claim jobs left pending by a crashed worker."""
    try:
        pending = await redis.xpending_range(
            config.stream_key,
            config.consumer_group,
            min="-",
            max="+",
            count=count,
            idle=config.job_claim_idle_ms,
        )
        ids = [item["message_id"] for item in pending]
        if not ids:
            return []
        claimed = await redis.xclaim(
            config.stream_key,
            config.consumer_group,
            config.consumer_name,
            config.job_claim_idle_ms,
            ids,
        )
        return [{"msg_id": msg_id, "data": data} for msg_id, data in claimed]
    except Exception:
        return []


async def dead_letter(redis: aioredis.Redis, config: Config, data: dict, error: str) -> None:
    payload = dict(data)
    payload["error"] = error[:2000]
    await redis.xadd(config.dead_letter_stream, payload)


async def ack_job(redis: aioredis.Redis, config: Config, msg_id: str) -> None:
    await redis.xack(config.stream_key, config.consumer_group, msg_id)
