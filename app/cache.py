"""Short-lived exact-query report cache.

This intentionally does not embed or semantically retrieve research. Live research
remains the default; the cache is an optional optimization for explicitly repeated
requests and is still validated by the output guardrail before use.
"""

from __future__ import annotations

import hashlib

import redis.asyncio as aioredis

from app.config import Config


def _cache_key(query: str) -> str:
    normalized = " ".join(query.casefold().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"report-cache:{digest}"


async def cache_get(redis: aioredis.Redis, config: Config, query: str) -> str | None:
    return await redis.get(_cache_key(query))


async def cache_set(redis: aioredis.Redis, config: Config, query: str, result: str) -> None:
    await redis.setex(_cache_key(query), config.cache_ttl, result)
