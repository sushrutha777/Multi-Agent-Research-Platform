"""Backend-controlled live research tools.

The LLM may choose a provider, but this module owns URL validation, provider
limits, timeouts, and the structured evidence contract returned to the graph.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import Config


logger = logging.getLogger(__name__)
_tool_call_times: dict[str, deque[float]] = defaultdict(deque)
_tool_rate_lock = asyncio.Lock()


class ToolError(RuntimeError):
    """A recoverable research-tool failure."""


async def _acquire_tool_slot(config: Config, provider: str) -> None:
    """Apply a per-worker provider rate limit in addition to per-job call limits."""
    now = time.monotonic()
    async with _tool_rate_lock:
        calls = _tool_call_times[provider]
        while calls and now - calls[0] >= 60:
            calls.popleft()
        if len(calls) >= config.tool_rate_limit_per_minute:
            raise ToolError(f"{provider} tool rate limit exceeded")
        calls.append(now)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 12000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _domain_matches(hostname: str, patterns: list[str]) -> bool:
    host = hostname.casefold().rstrip(".")
    for pattern in patterns:
        candidate = pattern.casefold().lstrip("*.").rstrip(".")
        if host == candidate or host.endswith(f".{candidate}"):
            return True
    return False


def _is_private_host(hostname: str) -> bool:
    lowered = hostname.casefold().rstrip(".")
    if lowered in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        return True
    try:
        address = ipaddress.ip_address(lowered)
        return (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    except ValueError:
        return False


async def _resolves_to_private_address(hostname: str) -> bool:
    """Reject obvious DNS-to-private-network targets before Firecrawl access."""
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return any(_is_private_host(record[4][0]) for record in records)


async def validate_public_url(url: str, config: Config | None = None) -> str:
    if not url or len(url) > 2048:
        raise ToolError("URL is empty or too long")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("Only absolute HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise ToolError("URLs containing credentials are not allowed")
    hostname = parsed.hostname.casefold().rstrip(".")
    if _is_private_host(hostname) or await _resolves_to_private_address(hostname):
        raise ToolError("Private or internal network URLs are not allowed")
    return parsed.geturl()


def _evidence(
    *,
    title: str,
    url: str,
    provider: str,
    content: str,
    relevance_score: float | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    return {
        "title": _text(title, 500),
        "url": url,
        "source_type": "web",
        "provider": provider,
        "content": _text(content),
        "retrieved_at": _now(),
        "published_at": published_at,
        "relevance_score": relevance_score,
    }


async def tavily_search(config: Config, query: str) -> list[dict[str, Any]]:
    if not config.tavily_api_key:
        raise ToolError("Tavily is not configured")
    await _acquire_tool_slot(config, "tavily")
    async with httpx.AsyncClient(timeout=config.tool_timeout) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": config.tavily_api_key,
                "query": query,
                "search_depth": "advanced",
                "max_results": min(config.search_max_results, 10),
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        response.raise_for_status()
        payload = response.json()

    results: list[dict[str, Any]] = []
    for item in payload.get("results", [])[: config.search_max_results]:
        try:
            url = await validate_public_url(str(item.get("url", "")), config)
        except ToolError:
            continue
        results.append(
            _evidence(
                title=str(item.get("title", "Untitled source")),
                url=url,
                provider="tavily",
                content=str(item.get("content", "")),
                relevance_score=float(item["score"]) if item.get("score") is not None else None,
                published_at=item.get("published_date"),
            )
        )
    return results


async def wikipedia_search(config: Config, query: str) -> list[dict[str, Any]]:
    if not config.wikipedia_enabled:
        raise ToolError("Wikipedia is disabled")
    await _acquire_tool_slot(config, "wikipedia")
    async with httpx.AsyncClient(timeout=config.tool_timeout) as client:
        search_response = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": min(config.search_max_results, 5),
                "format": "json",
            },
        )
        search_response.raise_for_status()
        matches = search_response.json().get("query", {}).get("search", [])

        async def fetch_summary(match: dict[str, Any]) -> dict[str, Any] | None:
            title = str(match.get("title", ""))
            response = await client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}"
            )
            if response.status_code >= 400:
                return None
            data = response.json()
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page")
            if not page_url:
                return None
            try:
                page_url = await validate_public_url(page_url, config)
            except ToolError:
                return None
            return _evidence(
                title=data.get("title", title),
                url=page_url,
                provider="wikipedia",
                content=data.get("extract", ""),
                published_at=None,
            )

        fetched = await asyncio.gather(*(fetch_summary(match) for match in matches), return_exceptions=True)
    return [item for item in fetched if isinstance(item, dict)]
