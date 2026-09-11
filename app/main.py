from __future__ import annotations

import asyncio
import base64
import logging
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agents import ResearchState, build_graph
from app.auth import assert_session_access, bind_session_access, require_api_key
from app.cache import cache_get, cache_set
from app.config import Config
from app.eval import evaluate_report, fetch_recent_topics, run_batch_evaluation
from app.guardrails import validate_input, validate_output
from app.memory import db_migrate, ltm_diff, ltm_store, session_add, session_get
from app.output import generate_json_report, generate_pdf, get_report_diff
from app.pool import close_pool, init_pool
from app.queue import (
    ack_job,
    consume_jobs,
    dead_letter,
    ensure_group,
    recover_abandoned,
    set_result,
    push_job,
    get_result,
)


logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger(__name__)

config = Config()
redis_client: aioredis.Redis | None = None
graph = None
stop_event = asyncio.Event()
worker_task: asyncio.Task | None = None
worker_tasks: set[asyncio.Task] = set()


async def _rate_limit(request: Request) -> None:
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Queue is not ready")
    client_ip = request.client.host if request.client else "unknown"
    key = f"ratelimit:{client_ip}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, config.rate_limit_window)
    if count > config.rate_limit_requests:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")


async def _bounded_process(job: dict) -> None:
    semaphore = _bounded_process.semaphore
    await semaphore.acquire()
    try:
        await asyncio.wait_for(_process_job(job["data"], job["msg_id"]), timeout=config.job_timeout)
    except asyncio.TimeoutError:
        logger.error("Job timed out: %s", job["data"].get("job_id"))
        await _handle_job_failure(job["data"], job["msg_id"], "Job processing timed out")
    finally:
        semaphore.release()


_bounded_process.semaphore = asyncio.Semaphore(config.worker_concurrency)


async def _worker_loop() -> None:
    if redis_client is None:
        raise RuntimeError("Redis is not initialized")
    await ensure_group(redis_client, config)
    while not stop_event.is_set():
        try:
            jobs = await recover_abandoned(redis_client, config)
            if not jobs and not _bounded_process.semaphore.locked():
                jobs = await consume_jobs(redis_client, config, count=config.worker_concurrency)
            for job in jobs:
                if _bounded_process.semaphore.locked():
                    break
                task = asyncio.create_task(_bounded_process(job))
                worker_tasks.add(task)
                task.add_done_callback(worker_tasks.discard)
            if not jobs:
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Worker loop failed: %s", traceback.format_exc())
            await asyncio.sleep(1)


async def _handle_job_failure(data: dict, msg_id: str, error: str) -> None:
    if redis_client is None:
        return
    job_id = data["job_id"]
    attempt = int(data.get("attempt", 0))
    if attempt < config.job_max_retries:
        await push_job(
            redis_client,
            config,
            data["topic"],
            data["session_id"],
            data.get("output_format", "text"),
            job_id=job_id,
            attempt=attempt + 1,
        )
        await set_result(
            redis_client,
            config,
            job_id,
            {"status": "retrying", "job_id": job_id, "attempt": attempt + 1, "error": error},
        )
    else:
        await dead_letter(redis_client, config, data, error)
        await set_result(redis_client, config, job_id, {"status": "error", "job_id": job_id, "error": error})
    await ack_job(redis_client, config, msg_id)


async def _process_job(data: dict, msg_id: str) -> None:
    if redis_client is None or graph is None:
        raise RuntimeError("Application worker is not initialized")
    job_id = data["job_id"]
    topic = data["topic"]
    session_id = data["session_id"]
    output_format = data.get("output_format", "text")
    log = logging.getLogger(f"job.{job_id[:8]}")
    try:
        log.info("Starting live research job for topic: %s", topic)
        session_history = await session_get(redis_client, session_id)

        report_text: str | None = None
        citations: list[dict] = []
        if config.cache_reports:
            cached = await cache_get(redis_client, config, topic)
            if cached:
                ok, reason = await validate_output(config, cached)
                if ok:
                    report_text = cached
                else:
                    log.warning("Cached report rejected by output guardrail: %s", reason)

        if report_text is None:
            initial_state: ResearchState = {
                "user_query": topic,
                "session_id": session_id,
                "research_plan": {},
                "source_deltas": [],
                "sources": [],
                "evidence": [],
                "summaries": [],
                "draft": "",
                "report": "",
                "critique": {},
                "citations": [],
                "status": "queued",
                "errors": [],
                "iteration_count": 0,
                "metadata": {},
                "session_history": session_history,
            }
            final_state = await graph.ainvoke(initial_state)
            report_text = final_state.get("report", "")
            citations = final_state.get("citations", [])
            if not report_text:
                raise RuntimeError("Research graph returned an empty report")

            ok, reason = await validate_output(config, report_text)
            if not ok:
                await set_result(redis_client, config, job_id, {"status": "blocked", "error": reason})
                await ack_job(redis_client, config, msg_id)
                return
            if config.cache_reports:
                await cache_set(redis_client, config, topic, report_text)

            await ltm_store(
                config,
                topic,
                report_text,
                str(uuid.uuid4()),
                metadata={
                    "citations": final_state.get("citations", []),
                    "critique": final_state.get("critique", {}),
                    "status": final_state.get("status"),
                    "errors": final_state.get("errors", []),
                },
            )

        await session_add(redis_client, config, session_id, "assistant", report_text[: config.session_content_truncate])
        diff = await ltm_diff(config, topic)
        result: dict = {
            "status": "done",
            "job_id": job_id,
            "session_id": session_id,
            "topic": topic,
            "report": report_text,
            "diff": diff,
            "citations": citations,
        }

        asyncio.create_task(evaluate_report(config, job_id, topic, report_text))
        if output_format == "pdf":
            result["pdf_base64"] = base64.b64encode(generate_pdf(topic, report_text)).decode()
        elif output_format == "json":
            result["structured"] = generate_json_report(
                topic, report_text, job_id, datetime.now(timezone.utc), citations=citations
            )

        await set_result(redis_client, config, job_id, result)
        log.info("Job completed successfully")
    except asyncio.CancelledError:
        # Leave the stream message pending so another worker can recover it.
        raise
    except Exception as exc:
        log.error("Job failed: %s", traceback.format_exc())
        await _handle_job_failure(data, msg_id, str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, graph, worker_task
    redis_client = aioredis.from_url(config.redis_url, decode_responses=True)
    await init_pool(config)
    await db_migrate(config)
    graph = build_graph(config)
    app.state.config = config
    stop_event.clear()
    if config.environment != "test" and config.start_worker:
        worker_task = asyncio.create_task(_worker_loop())
    yield
    stop_event.set()
    if worker_task:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
    for task in list(worker_tasks):
        task.cancel()
    if worker_tasks:
        await asyncio.gather(*worker_tasks, return_exceptions=True)
    if redis_client:
        await redis_client.aclose()
    await close_pool()


app = FastAPI(title="Research Agent API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)

project_root = Path(__file__).resolve().parent.parent
frontend_build_dirs = [
    project_root / "frontend-dist",
    project_root / "frontend" / "dist",
]
frontend_build_dir = next((path for path in frontend_build_dirs if path.exists()), None)
if frontend_build_dir and (frontend_build_dir / "assets").exists():
    app.mount("/assets", StaticFiles(directory=frontend_build_dir / "assets"), name="frontend-assets")


class ResearchRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=5000)
    session_id: str = Field(default="", max_length=200)
    output_format: Literal["text", "pdf", "json"] = "text"


@app.get("/")
async def frontend():
    built_frontend = next(
        (path / "index.html" for path in frontend_build_dirs if (path / "index.html").exists()),
        None,
    )
    fallback_frontend = project_root / "index.html"
    return FileResponse(built_frontend or fallback_frontend)


@app.get("/health")
async def health():
    try:
        if redis_client is None:
            raise RuntimeError("Redis is not initialized")
        await redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis": "ok" if redis_ok else "error"}


@app.post("/research", dependencies=[Depends(require_api_key), Depends(_rate_limit)])
async def start_research(request: Request, req: ResearchRequest):
    ok, reason = await validate_input(config, req.topic)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Queue is not ready")
    session_id = req.session_id or str(uuid.uuid4())
    await assert_session_access(redis_client, request, session_id)
    await bind_session_access(redis_client, config, request, session_id)
    await session_add(redis_client, config, session_id, "user", req.topic)
    job_id = await push_job(redis_client, config, req.topic, session_id, req.output_format)
    return {"job_id": job_id, "session_id": session_id}


@app.get("/result/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job_result(job_id: str):
    return await get_result(redis_client, config, job_id) or {"status": "pending"}


@app.get("/session/{session_id}", dependencies=[Depends(require_api_key)])
async def get_session(request: Request, session_id: str):
    await assert_session_access(redis_client, request, session_id)
    return {"session_id": session_id, "messages": await session_get(redis_client, session_id)}


@app.get("/diff/{topic}", dependencies=[Depends(require_api_key)])
async def report_diff(topic: str):
    diff = await get_report_diff(config, topic)
    return {"topic": topic, "diff": diff or "No previous report found."}


@app.get("/result/{job_id}/pdf", dependencies=[Depends(require_api_key)])
async def download_pdf(job_id: str):
    result = await get_result(redis_client, config, job_id)
    if not result or result.get("status") != "done":
        raise HTTPException(status_code=404, detail="Report not ready")
    return Response(
        content=generate_pdf(result.get("topic", "Report"), result["report"]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={job_id}.pdf"},
    )


@app.get("/stats", dependencies=[Depends(require_api_key)])
async def stats():
    info = await redis_client.info()
    keys = await redis_client.dbsize()
    cache_keys = len([key async for key in redis_client.scan_iter("report-cache:*")])
    session_keys = len([key async for key in redis_client.scan_iter("session:*")])
    return {
        "redis": {
            "total_keys": keys,
            "cache_entries": cache_keys,
            "active_sessions": session_keys,
            "memory_used_mb": round(info["used_memory"] / 1024 / 1024, 2),
            "connected_clients": info["connected_clients"],
            "uptime_hours": round(info["uptime_in_seconds"] / 3600, 1),
        }
    }


@app.get("/evaluate/{job_id}", dependencies=[Depends(require_api_key)])
async def evaluate_job(job_id: str):
    result = await get_result(redis_client, config, job_id)
    if not result or result.get("status") != "done":
        raise HTTPException(status_code=404, detail="Job not done yet")
    scores = await evaluate_report(config, job_id, result["topic"], result["report"])
    return {"job_id": job_id, "topic": result["topic"], "scores": scores}


class BatchEvalRequest(BaseModel):
    topics: list[str] = []


@app.post("/run-evaluation", dependencies=[Depends(require_api_key)])
async def trigger_batch_evaluation(req: BatchEvalRequest):
    topics = req.topics if req.topics else await fetch_recent_topics()
    if not topics:
        raise HTTPException(status_code=400, detail="No topics found. Submit at least one research job first.")
    asyncio.create_task(run_batch_evaluation(config, graph, topics))
    return {"message": "Batch evaluation started in background", "topics": len(topics)}
