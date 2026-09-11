from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.queue import get_result, push_job


class FakeRedis:
    def __init__(self):
        self.stream = []
        self.values = {}
        self.acked = []

    async def xadd(self, stream, data):
        self.stream.append((stream, data))
        return "1-0"

    async def setex(self, key, ttl, value):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def xack(self, stream, group, message_id):
        self.acked.append(message_id)


@pytest.mark.asyncio
async def test_job_submission_and_polling_contract():
    redis = FakeRedis()
    config = SimpleNamespace(stream_key="jobs", result_ttl=60)
    job_id = await push_job(redis, config, "topic", "session", "text")
    assert redis.stream[0][1]["topic"] == "topic"
    result = await get_result(redis, config, job_id)
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_polling_returns_none_for_unknown_job():
    redis = FakeRedis()
    config = SimpleNamespace(result_ttl=60)
    assert await get_result(redis, config, "missing") is None


@pytest.mark.asyncio
async def test_worker_failure_requeues_before_dead_letter(monkeypatch):
    import app.main as runtime

    redis = FakeRedis()
    config = SimpleNamespace(
        stream_key="jobs",
        consumer_group="workers",
        result_ttl=60,
        dead_letter_stream="dead",
        job_max_retries=2,
    )
    monkeypatch.setattr(runtime, "redis_client", redis)
    monkeypatch.setattr(runtime, "config", config)
    await runtime._handle_job_failure(
        {"job_id": "job", "topic": "topic", "session_id": "session", "output_format": "text", "attempt": "0"},
        "message",
        "temporary failure",
    )
    assert redis.stream[0][0] == "jobs"
    assert json.loads(redis.values["result:job"])["status"] == "retrying"
    assert redis.acked == ["message"]
