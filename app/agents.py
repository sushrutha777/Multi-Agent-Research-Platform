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
from app.streaming import emit_event
from app.tools import ToolError, tavily_search, wikipedia_search


logger = logging.getLogger(__name__)


class ResearchState(TypedDict, total=False):
    job_id: str
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
    lowered = query.casefold()
    current = any(word in lowered for word in ("latest", "current", "today", "recent", "2026", "now"))
    broad = len(query.split()) > 8 or any(word in lowered for word in ("compare", "impact", "market", "trend"))
    tools: list[str] = []
    if config.tavily_api_key and (current or broad):
        tools.append("tavily")
    if config.wikipedia_enabled and (not current or not config.tavily_api_key):
        tools.append("wikipedia")
    if not tools:
        tools = ["tavily"] if config.tavily_api_key else ["wikipedia"]
    return {
        "tools": tools,
        "queries": [query],
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
                    '{"tools":["tavily"|"wikipedia"],"queries":["..."]}. '
                    "Choose only the tools needed. Use Wikipedia for foundational background, and Tavily for current web research. "
                    f"User question: {query}"
                ),
            )
            planned = _json_from_text(response) or fallback
        except Exception as exc:
            logger.warning("Research planning failed; using deterministic fallback: %s", exc)
            planned = fallback

        allowed = {"tavily", "wikipedia"}
        tools = [tool for tool in planned.get("tools", []) if tool in allowed]
        if not self.config.tavily_api_key and "tavily" in tools:
            tools.remove("tavily")
        if not self.config.wikipedia_enabled and "wikipedia" in tools:
            tools.remove("wikipedia")
        if not tools:
            tools = [
                tool
                for tool in fallback["tools"]
                if (tool != "tavily" or bool(self.config.tavily_api_key))
                and (tool != "wikipedia" or self.config.wikipedia_enabled)
            ]
        if not tools:
            tools = ["tavily"] if self.config.tavily_api_key else ["wikipedia"]
        return {
            "tools": tools,
            "queries": [str(q) for q in planned.get("queries", [query])][:3] or [query],
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
    def __init__(self, config: Config, redis: Any = None):
        self.config = config
        self.redis = redis
        self.search_agent = SearchAgent(config)
        self.summarize_agent = SummarizerAgent(config)
        self.writer_agent = WriterAgent(config)
        self.critic_agent = CriticAgent(config)

    async def _emit(
        self,
        state: ResearchState,
        event_type: str,
        step: str,
        title: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        job_id = state.get("job_id")
        if job_id:
            try:
                await emit_event(self.redis, job_id, event_type, step, title, details)
            except Exception as exc:
                logger.debug("Failed to emit event: %s", exc)

    @traceable(run_type="chain", name="orchestrator:plan")
    async def plan_node(self, state: ResearchState) -> dict[str, Any]:
        await self._emit(state, "status", "planning", "Formulating research strategy and selecting optimal search tools...")
        plan = await self.search_agent.plan(state["user_query"])
        tools = plan.get("tools", [])
        queries = plan.get("queries", [])
        await self._emit(
            state,
            "plan",
            "planning",
            f"Strategy ready: {len(queries)} query(ies) across {', '.join(tools) if tools else 'search'}",
            {"tools": tools, "queries": queries},
        )
        return {"research_plan": plan, "status": "planning"}

    def route_initial_tools(self, state: ResearchState) -> list[str]:
        tools = [t for t in state.get("research_plan", {}).get("tools", []) if t in {"tavily", "wikipedia"}]
        return tools or ["tavily"]

    @traceable(run_type="chain", name="orchestrator:tavily")
    async def tavily_node(self, state: ResearchState) -> dict[str, Any]:
        queries = state.get("research_plan", {}).get("queries", [])
        await self._emit(
            state,
            "tool_start",
            "searching",
            f"Querying Tavily Web Search ({len(queries)} queries)...",
            {"provider": "tavily", "queries": queries},
        )
        try:
            sources = await self.search_agent.tavily(state["research_plan"])
            await self._emit(
                state,
                "tool_result",
                "searching",
                f"Retrieved {len(sources)} web sources from Tavily",
                {
                    "provider": "tavily",
                    "count": len(sources),
                    "sources": [{"title": s.get("title"), "url": s.get("url")} for s in sources[:5]],
                },
            )
            return {"source_deltas": sources}
        except Exception as exc:
            await self._emit(
                state,
                "tool_error",
                "searching",
                f"Tavily search encountered an issue: {exc}",
                {"provider": "tavily", "error": str(exc)},
            )
            return {"errors": [f"Tavily failed: {exc}"]}

    @traceable(run_type="chain", name="orchestrator:wikipedia")
    async def wikipedia_node(self, state: ResearchState) -> dict[str, Any]:
        await self._emit(
            state,
            "tool_start",
            "searching",
            "Searching Wikipedia reference encyclopedia...",
            {"provider": "wikipedia"},
        )
        try:
            sources = await self.search_agent.wikipedia(state["research_plan"])
            await self._emit(
                state,
                "tool_result",
                "searching",
                f"Retrieved {len(sources)} Wikipedia articles",
                {
                    "provider": "wikipedia",
                    "count": len(sources),
                    "sources": [{"title": s.get("title"), "url": s.get("url")} for s in sources[:5]],
                },
            )
            return {"source_deltas": sources}
        except Exception as exc:
            await self._emit(
                state,
                "tool_error",
                "searching",
                f"Wikipedia search failed: {exc}",
                {"provider": "wikipedia", "error": str(exc)},
            )
            return {"errors": [f"Wikipedia failed: {exc}"]}

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

        citations = [
            {key: source.get(key) for key in ("citation_id", "title", "url", "provider", "retrieved_at", "published_at")}
            for source in unique
        ]

        await self._emit(
            state,
            "sources",
            "searching",
            f"Assembled and verified {len(unique)} unique evidence sources",
            {"count": len(unique), "citations": citations},
        )

        return {
            "sources": unique,
            "evidence": unique,
            "citations": citations,
            "status": "evidence_ready",
        }

    @traceable(run_type="chain", name="orchestrator:summarize")
    async def summarize_node(self, state: ResearchState) -> dict[str, Any]:
        await self._emit(
            state,
            "status",
            "summarizing",
            "Synthesizing key findings and evidence across all sources...",
        )
        try:
            summary = await self.summarize_agent.run(state["user_query"], state.get("sources", []))
            summary_text = summary.get("summary", "")
            await self._emit(
                state,
                "summary",
                "summarizing",
                "Evidence synthesis complete",
                {"preview": summary_text[:300]},
            )
            return {"summaries": [summary], "status": "summarized"}
        except Exception as exc:
            return {
                "summaries": [{"summary": "Summary unavailable because the summarizer failed.", "source_ids": []}],
                "errors": [f"Summarizer failed: {exc}"],
                "status": "summarized",
            }

    @traceable(run_type="chain", name="orchestrator:write")
    async def write_node(self, state: ResearchState) -> dict[str, Any]:
        iteration = state.get("iteration_count", 0) + 1
        await self._emit(
            state,
            "status",
            "writing",
            f"Drafting comprehensive report (Iteration {iteration})...",
            {"iteration": iteration},
        )
        try:
            draft = await self.writer_agent.run(
                state["user_query"],
                state.get("sources", []),
                state.get("summaries", []),
                state.get("critique"),
            )
            await self._emit(
                state,
                "draft",
                "writing",
                f"Report draft {iteration} prepared ({len(draft)} characters)",
                {"draft": draft, "iteration": iteration},
            )
            return {
                "draft": draft,
                "report": draft,
                "iteration_count": iteration,
                "status": "drafted",
            }
        except Exception as exc:
            return {
                "draft": "The report could not be generated because the writer failed.",
                "report": "The report could not be generated because the writer failed.",
                "errors": [f"Writer failed: {exc}"],
                "iteration_count": iteration,
                "status": "drafted",
            }

    @traceable(run_type="chain", name="orchestrator:critic")
    async def critic_node(self, state: ResearchState) -> dict[str, Any]:
        await self._emit(
            state,
            "status",
            "critiquing",
            "Critic reviewing draft for factual accuracy, hallucinations & citation alignment...",
        )
        critique = await self.critic_agent.run(
            state["user_query"], state.get("draft", ""), state.get("sources", [])
        )
        passed = critique.get("passed", False)
        verdict = "passed all checks" if passed else "identified areas for revision"
        await self._emit(
            state,
            "critique",
            "critiquing",
            f"Review completed: {verdict}",
            {"critique": critique, "passed": passed},
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
        await self._emit(
            state,
            "status",
            "finalizing",
            "Finalizing report and formatting citations...",
            {"status": status},
        )
        return {"report": state.get("draft", ""), "status": status}


def build_graph(config: Config, redis: Any = None):
    orchestrator = OrchestratorAgent(config, redis=redis)
    workflow = StateGraph(ResearchState)

    workflow.add_node("plan", orchestrator.plan_node)
    workflow.add_node("search_tavily", orchestrator.tavily_node)
    workflow.add_node("search_wikipedia", orchestrator.wikipedia_node)
    workflow.add_node("finalize_sources", orchestrator.finalize_sources_node)
    workflow.add_node("summarize", orchestrator.summarize_node)
    workflow.add_node("write", orchestrator.write_node)
    workflow.add_node("critic", orchestrator.critic_node)
    workflow.add_node("finalize", orchestrator.finalize_node)

    workflow.add_edge(START, "plan")
    workflow.add_conditional_edges(
        "plan",
        orchestrator.route_initial_tools,
        {"tavily": "search_tavily", "wikipedia": "search_wikipedia"},
    )
    workflow.add_edge("search_tavily", "finalize_sources")
    workflow.add_edge("search_wikipedia", "finalize_sources")
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
