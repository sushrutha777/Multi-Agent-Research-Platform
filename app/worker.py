"""Standalone worker entry point used by Docker Compose and production workers."""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from app import main as runtime
from app.agents import build_graph
from app.memory import db_migrate
from app.pool import close_pool, init_pool


async def run() -> None:
    runtime.redis_client = aioredis.from_url(runtime.config.redis_url, decode_responses=True)
    await init_pool(runtime.config)
    await db_migrate(runtime.config)
    runtime.graph = build_graph(runtime.config)
    runtime.stop_event.clear()
    try:
        await runtime._worker_loop()
    finally:
        for task in list(runtime.worker_tasks):
            task.cancel()
        if runtime.worker_tasks:
            await asyncio.gather(*runtime.worker_tasks, return_exceptions=True)
        await runtime.redis_client.aclose()
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
