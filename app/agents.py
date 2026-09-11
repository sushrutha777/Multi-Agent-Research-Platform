from __future__ import annotations

import asyncio
import json
import logging
import operator
import re
from typing import Annotated, Any, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from langsmith import traceable

from app.config import Config
from app.retry import with_retry
from app.tools import ToolError, firecrawl_scrape, tavily_search, wikipedia_search


logger = logging.getLogger(__name__)


class ResearchState(TypedDict, total=False):
    user_query: str
    session_id: str
    research_plan: dict[str, Any]
    source_deltas: Annotated[list[dict[str, Any]], operator.add]
    sources: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    summaries: list[dict[str, Any]]
    draft: str
    report: str
    critique: dict[str, Any]
    citations: list[dict[str, Any]]
    status: str
    errors: Annotated[list[str], operator.add]
    iteration_count: int
    metadata: dict[str, Any]
    session_history: list[dict]


def _json_from_text(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start : end + 1])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


async def _tz_call(config: Config, function_name: str, message: str) -> str:
    return await with_retry(
        lambda: _tz_call_once(config, function_name, message),
        max_retries=config.llm_max_retries,
        delay=config.llm_retry_delay,
    )


async def _tz_call_once(config: Config, function_name: str, message: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{config.tensorzero_url}/inference",
            json={
                "function_name": function_name,
                "input": {"messages": [{"role": "user", "content": message}]},
            },
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]


def _heuristic_plan(query: str, config: Config) -> dict[str, Any]:
    urls = re.findall(r"https?://[^\s)]+", query)
    if urls:
        return {"tools": ["firecrawl"], "queries": [query], "urls": urls, "firecrawl_after_search": False}
    lowered = query.casefold()
    current = any(word in lowered for word in ("latest", "current", "today", "recent", "2026", "now"))
    broad = len(query.split()) > 8 or any(word in lowered for word in ("compare", "impact", "market", "trend"))
    tools: list[str] = []
    if config.tavily_api_key and (current or broad):
        tools.append("tavily")
    if config.wikipedia_enabled and (not current or not config.tavily_api_key):
        tools.append("wikipedia")
    if not tools:
        tools = ["tavily"]
    return {
        "tools": tools,
        "queries": [query],
        "urls": [],
        "firecrawl_after_search": bool(config.firecrawl_api_key and config.firecrawl_allowed_domains and broad),
    }


class SearchAgent:
    """Chooses and invokes live research tools under backend policy."""

    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="chain", name="agent:research-plan")
    async def plan(self, query: str) -> dict[str, Any]:
        fallback = _heuristic_plan(query, self.config)
        try:
            response = await _tz_call(
                self.config,
                "research_summarize",
                (
                    "Create a research tool plan. Return JSON only with this shape: "
                    '{"tools":["tavily"|"wikipedia"|"firecrawl"],'
                    '"queries":["..."],"urls":["..."],"firecrawl_after_search":true|false}. '
                    "Choose only the tools needed. Use Wikipedia for background, Tavily for current or broad web research, "
                    "and Firecrawl only for explicit URLs or important pages discovered by search. "
                    f"User question: {query}"
                ),
            )
            planned = _json_from_text(response) or fallback
        except Exception as exc:
            logger.warning("Research planning failed; using deterministic fallback: %s", exc)
            planned = fallback

        allowed = {"tavily", "wikipedia", "firecrawl"}
        tools = [tool for tool in planned.get("tools", []) if tool in allowed]
        if not self.config.tavily_api_key and "tavily" in tools:
            tools.remove("tavily")
        if not self.config.wikipedia_enabled and "wikipedia" in tools:
            tools.remove("wikipedia")
        if not self.config.firecrawl_api_key or not self.config.firecrawl_allowed_domains:
            tools = [tool for tool in tools if tool != "firecrawl"]
        if "firecrawl" in tools and not planned.get("urls"):
            tools.remove("firecrawl")
            planned["firecrawl_after_search"] = True
        if not self.config.firecrawl_api_key or not self.config.firecrawl_allowed_domains:
            planned["firecrawl_after_search"] = False
        if not tools:
            tools = [
                tool
                for tool in fallback["tools"]
                if (tool != "tavily" or bool(self.config.tavily_api_key))
                and (tool != "wikipedia" or self.config.wikipedia_enabled)
                and (tool != "firecrawl" or bool(self.config.firecrawl_api_key and self.config.firecrawl_allowed_domains))
            ]
        if not tools:
            tools = ["wikipedia"] if self.config.wikipedia_enabled else ["tavily"]
        return {
            "tools": tools,
            "queries": [str(query) for query in planned.get("queries", [query])][:3] or [query],
            "urls": [str(url) for url in planned.get("urls", [])][: self.config.firecrawl_max_pages],
            "firecrawl_after_search": bool(planned.get("firecrawl_after_search", False)),
        }

    @traceable(run_type="tool", name="tool:tavily")
    async def tavily(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for query in plan.get("queries", [])[: self.config.max_tool_calls]:
            results.extend(await tavily_search(self.config, query))
        return results

    @traceable(run_type="tool", name="tool:wikipedia")
    async def wikipedia(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for query in plan.get("queries", [])[: self.config.max_tool_calls]:
            results.extend(await wikipedia_search(self.config, query))
        return results

    @traceable(run_type="tool", name="tool:firecrawl")
    async def firecrawl(self, plan: dict[str, Any], sources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        urls = list(plan.get("urls", []))
        if plan.get("firecrawl_after_search"):
            urls.extend(source.get("url", "") for source in (sources or []))
        return await firecrawl_scrape(self.config, urls)


class SummarizerAgent:
    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="tool", name="agent:summarize")
    async def run(self, query: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
        if not sources:
            return {
                "summary": "No usable web evidence was returned. The report must state this limitation.",
                "source_ids": [],
            }
        evidence = "\n\n".join(
            f"[{source.get('citation_id')}] {source.get('title')} ({source.get('url')})\n{source.get('content', '')[:6000]}"
            for source in sources
        )
        summary = await _tz_call(
            self.config,
            "research_summarize",
            (
                f"Summarize the supplied evidence for the question: {query}\n"
                "Remove duplicates, preserve uncertainty, and do not add facts not present in the evidence. "
                "Keep the citation IDs attached to each claim.\n\n"
                f"Evidence:\n{evidence}"
            ),
        )
        return {"summary": summary, "source_ids": [source.get("citation_id") for source in sources]}


class WriterAgent:
    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="tool", name="agent:writer")
    async def run(
        self,
        query: str,
        sources: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        critique: dict[str, Any] | None = None,
    ) -> str:
        allowed_sources = "\n".join(
            f"[{source.get('citation_id')}] {source.get('title')} — {source.get('url')}"
            for source in sources
        ) or "No sources were retrieved."
        summary_text = "\n\n".join(summary.get("summary", "") for summary in summaries)
        revision = ""
        if critique:
            revision = f"\n\nCRITIC FEEDBACK TO ADDRESS:\n{json.dumps(critique, ensure_ascii=False)}"
        report = await _tz_call(
            self.config,
            "report_write",
            (
                f"Write a rigorous research report answering: {query}\n\n"
                "Use only the supplied evidence. Every factual claim that depends on a source must cite one or more "
                "retrieved IDs in the exact form [S1], [S2], etc. Never invent a source or citation. Clearly mark "
                "uncertainty and missing evidence. Include Executive Summary, Key Findings, Analysis, and Conclusion.\n\n"
                f"Allowed sources:\n{allowed_sources}\n\nSummaries:\n{summary_text}{revision}"
            ),
        )
        allowed_ids = {source.get("citation_id") for source in sources}
        return re.sub(
            r"\[(S\d+)\]",
            lambda match: match.group(0) if match.group(1) in allowed_ids else "",
            report,
        )


class CriticAgent:
    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="tool", name="agent:critic")
    async def run(self, query: str, report: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
        allowed_ids = {source.get("citation_id") for source in sources}
        cited_ids = set(re.findall(r"\[(S\d+)\]", report))
        citation_issues = [f"Unknown citation: {citation}" for citation in cited_ids - allowed_ids]
        evidence = "\n".join(
            f"[{source.get('citation_id')}] {source.get('title')} — {source.get('url')}: {source.get('content', '')[:2000]}"
            for source in sources
        )
        prompt = (
            f"Critique this report for the question: {query}\n"
            "Return JSON only with keys passed, issues, unsupported_claims, citation_issues, source_quality, "
            "missing_information, contradictions. passed must be false for unsupported claims or citation errors.\n\n"
            f"Evidence:\n{evidence}\n\nReport:\n{report[: self.config.agent_report_truncate]}"
        )
        try:
            response = await _tz_call(self.config, "research_summarize", prompt)
            critique = _json_from_text(response)
            if not critique:
                raise ValueError("Critic returned malformed JSON")
        except Exception as exc:
            critique = {
                "passed": False,
                "issues": [f"Critic failure: {exc}"],
                "unsupported_claims": [],
                "citation_issues": [],
                "source_quality": [],
                "missing_information": [],
                "contradictions": [],
            }
        critique.setdefault("citation_issues", [])
        critique["citation_issues"] = list(critique["citation_issues"]) + citation_issues
        critique["passed"] = bool(critique.get("passed")) and not citation_issues
        return critique


class OrchestratorAgent:
    def __init__(self, config: Config):
        self.config = config
        self.search_agent = SearchAgent(config)
        self.summarize_agent = SummarizerAgent(config)
        self.writer_agent = WriterAgent(config)
        self.critic_agent = CriticAgent(config)

    @traceable(run_type="chain", name="orchestrator:plan")
    async def plan_node(self, state: ResearchState) -> dict[str, Any]:
        return {"research_plan": await self.search_agent.plan(state["user_query"]), "status": "planning"}

    def route_initial_tools(self, state: ResearchState) -> list[str]:
        tools = state.get("research_plan", {}).get("tools", [])
        return tools or ["tavily"]

    @traceable(run_type="chain", name="orchestrator:tavily")
    async def tavily_node(self, state: ResearchState) -> dict[str, Any]:
        try:
            return {"source_deltas": await self.search_agent.tavily(state["research_plan"])}
        except Exception as exc:
            return {"errors": [f"Tavily failed: {exc}"]}

    @traceable(run_type="chain", name="orchestrator:wikipedia")
    async def wikipedia_node(self, state: ResearchState) -> dict[str, Any]:
        try:
            return {"source_deltas": await self.search_agent.wikipedia(state["research_plan"])}
        except Exception as exc:
            return {"errors": [f"Wikipedia failed: {exc}"]}

    @traceable(run_type="chain", name="orchestrator:firecrawl")
    async def firecrawl_node(self, state: ResearchState) -> dict[str, Any]:
        try:
            sources = state.get("sources") or state.get("source_deltas", [])
            return {
                "source_deltas": await self.search_agent.firecrawl(state["research_plan"], sources),
                "status": "scraping",
            }
        except Exception as exc:
            return {"errors": [f"Firecrawl failed: {exc}"], "status": "scraping"}

    def route_after_search(self, state: ResearchState) -> str:
        plan = state.get("research_plan", {})
        if plan.get("firecrawl_after_search") and state.get("source_deltas"):
            return "firecrawl"
        return "finalize_sources"

    @traceable(run_type="chain", name="orchestrator:merge-sources")
    async def merge_sources_node(self, state: ResearchState) -> dict[str, Any]:
        return {"status": "evidence_collected"}

    @traceable(run_type="chain", name="orchestrator:finalize-sources")
    async def finalize_sources_node(self, state: ResearchState) -> dict[str, Any]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in state.get("source_deltas", []):
            url = source.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            item = dict(source)
            item["citation_id"] = f"S{len(unique) + 1}"
            unique.append(item)
        return {
            "sources": unique,
            "evidence": unique,
            "citations": [
                {key: source.get(key) for key in ("citation_id", "title", "url", "provider", "retrieved_at", "published_at")}
                for source in unique
            ],
            "status": "evidence_ready",
        }

    @traceable(run_type="chain", name="orchestrator:summarize")
    async def summarize_node(self, state: ResearchState) -> dict[str, Any]:
        try:
            summary = await self.summarize_agent.run(state["user_query"], state.get("sources", []))
            return {"summaries": [summary], "status": "summarized"}
        except Exception as exc:
            return {
                "summaries": [{"summary": "Summary unavailable because the summarizer failed.", "source_ids": []}],
                "errors": [f"Summarizer failed: {exc}"],
                "status": "summarized",
            }

    @traceable(run_type="chain", name="orchestrator:write")
    async def write_node(self, state: ResearchState) -> dict[str, Any]:
        try:
            draft = await self.writer_agent.run(
                state["user_query"],
                state.get("sources", []),
                state.get("summaries", []),
                state.get("critique"),
            )
            return {
                "draft": draft,
                "report": draft,
                "iteration_count": state.get("iteration_count", 0) + 1,
                "status": "drafted",
            }
        except Exception as exc:
            return {
                "draft": "The report could not be generated because the writer failed.",
                "report": "The report could not be generated because the writer failed.",
                "errors": [f"Writer failed: {exc}"],
                "iteration_count": state.get("iteration_count", 0) + 1,
                "status": "drafted",
            }

    @traceable(run_type="chain", name="orchestrator:critic")
    async def critic_node(self, state: ResearchState) -> dict[str, Any]:
        critique = await self.critic_agent.run(
            state["user_query"], state.get("draft", ""), state.get("sources", [])
        )
        return {"critique": critique, "status": "criticized"}

    def route_after_critic(self, state: ResearchState) -> str:
        critique = state.get("critique", {})
        if critique.get("passed") or state.get("iteration_count", 0) >= self.config.agent_max_iterations:
            return "finalize"
        return "write"

    @traceable(run_type="chain", name="orchestrator:finalize")
    async def finalize_node(self, state: ResearchState) -> dict[str, Any]:
        critique = state.get("critique", {})
        status = "completed" if critique.get("passed") else "completed_with_warnings"
        return {"report": state.get("draft", ""), "status": status}


def build_graph(config: Config):
    orchestrator = OrchestratorAgent(config)
    workflow = StateGraph(ResearchState)

    workflow.add_node("plan", orchestrator.plan_node)
    workflow.add_node("search_tavily", orchestrator.tavily_node)
    workflow.add_node("search_wikipedia", orchestrator.wikipedia_node)
    workflow.add_node("search_firecrawl", orchestrator.firecrawl_node)
    workflow.add_node("merge_sources", orchestrator.merge_sources_node)
    workflow.add_node("finalize_sources", orchestrator.finalize_sources_node)
    workflow.add_node("summarize", orchestrator.summarize_node)
    workflow.add_node("write", orchestrator.write_node)
    workflow.add_node("critic", orchestrator.critic_node)
    workflow.add_node("finalize", orchestrator.finalize_node)

    workflow.add_edge(START, "plan")
    workflow.add_conditional_edges(
        "plan",
        orchestrator.route_initial_tools,
        {"tavily": "search_tavily", "wikipedia": "search_wikipedia", "firecrawl": "search_firecrawl"},
    )
    workflow.add_edge("search_tavily", "merge_sources")
    workflow.add_edge("search_wikipedia", "merge_sources")
    workflow.add_edge("search_firecrawl", "finalize_sources")
    workflow.add_conditional_edges(
        "merge_sources",
        orchestrator.route_after_search,
        {"firecrawl": "search_firecrawl", "finalize_sources": "finalize_sources"},
    )
    workflow.add_edge("finalize_sources", "summarize")
    workflow.add_edge("summarize", "write")
    workflow.add_edge("write", "critic")
    workflow.add_conditional_edges(
        "critic",
        orchestrator.route_after_critic,
        {"write": "write", "finalize": "finalize"},
    )
    workflow.add_edge("finalize", END)
    return workflow.compile()
