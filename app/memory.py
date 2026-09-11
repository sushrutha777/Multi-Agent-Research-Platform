"""Application memory without vector retrieval.

Redis stores short-lived session state. PostgreSQL stores ordinary report records,
metadata, and exact-topic history used for diffs. Live research evidence comes from
the web tools in ``app.tools`` rather than from this module.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import Config
from app.pool import get_pool


async def session_add(redis: aioredis.Redis, config: Config, session_id: str, role: str, content: str) -> None:
    key = f"session:{session_id}"
    await redis.rpush(key, json.dumps({"role": role, "content": content}))
    await redis.ltrim(key, -config.session_max_messages, -1)
    await redis.expire(key, config.session_ttl)


async def session_get(redis: aioredis.Redis, session_id: str) -> list[dict]:
    messages = await redis.lrange(f"session:{session_id}", 0, -1)
    return [json.loads(message) for message in messages]


async def db_migrate(config: Config) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id          TEXT PRIMARY KEY,
                topic       TEXT NOT NULL,
                report      TEXT NOT NULL,
                metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Existing deployments may have the previous reports table. Add only the
        # ordinary metadata column; legacy embedding data is left untouched but is
        # never read or written by the new workflow.
        await conn.execute(
            "ALTER TABLE reports ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS reports_topic_idx ON reports (topic)")
        await conn.execute("CREATE INDEX IF NOT EXISTS reports_created_idx ON reports (created_at DESC)")


async def ltm_store(
    config: Config,
    topic: str,
    report: str,
    report_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO reports (id, topic, report, metadata, created_at)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (id) DO NOTHING
            """,
            report_id,
            topic,
            report,
            json.dumps(metadata or {}),
            datetime.now(timezone.utc),
        )


async def ltm_search(config: Config, topic: str) -> dict | None:
    """Return a recent exact-topic record for history/diff purposes only."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, topic, report, metadata, created_at
            FROM reports
            WHERE topic = $1
              AND created_at > NOW() - ($2 || ' days')::INTERVAL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            topic,
            str(config.report_history_days),
        )
        return dict(row) if row else None


async def ltm_diff(config: Config, topic: str) -> str | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT report, created_at
            FROM reports
            WHERE topic = $1
            ORDER BY created_at DESC
            LIMIT 2
            """,
            topic,
        )
        if len(rows) < 2:
            return None
        old_lines = rows[1]["report"].splitlines(keepends=True)
        new_lines = rows[0]["report"].splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"previous ({rows[1]['created_at'].date()})",
            tofile=f"latest ({rows[0]['created_at'].date()})",
            lineterm="",
        ))
        return "\n".join(diff_lines[: config.ltm_diff_limit * 10]) or "No significant changes detected."
