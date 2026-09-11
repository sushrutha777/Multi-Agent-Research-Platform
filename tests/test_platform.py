from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents import CriticAgent, OrchestratorAgent, WriterAgent, _heuristic_plan, _json_from_text, build_graph
from app.cache import _cache_key
from app.guardrails import validate_input, validate_output
from app.output import generate_json_report, generate_pdf
from app.tools import ToolError, validate_public_url


def config_stub(**overrides):
    values = {
        "tavily_api_key": "tavily",
        "wikipedia_enabled": True,
        "firecrawl_api_key": "firecrawl",
        "firecrawl_allowed_domains": ["example.com"],
        "firecrawl_allowed_paths": [],
        "firecrawl_max_pages": 3,
        "search_max_results": 5,
        "max_tool_calls": 8,
        "tool_timeout": 5,
        "firecrawl_base_url": "https://api.firecrawl.dev",
        "agent_report_truncate": 3000,
        "agent_max_iterations": 2,
        "llm_max_retries": 1,
        "llm_retry_delay": 0,
        "guardrails_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_job_request_schema_and_output_formats():
    from app.main import ResearchRequest

    assert ResearchRequest(topic="hello", output_format="json").output_format == "json"
    with pytest.raises(Exception):
        ResearchRequest(topic="", output_format="text")
    with pytest.raises(Exception):
        ResearchRequest(topic="hello", output_format="xml")


def test_exact_cache_key_normalizes_whitespace_and_case():
    assert _cache_key("  Quantum   Computing ") == _cache_key("quantum computing")


@pytest.mark.asyncio
async def test_invalid_private_url_is_rejected(monkeypatch):
    monkeypatch.setattr("app.tools._resolves_to_private_address", AsyncMock(return_value=False))
    with pytest.raises(ToolError):
        await validate_public_url("http://127.0.0.1/admin", config_stub())


@pytest.mark.asyncio
async def test_firecrawl_domain_allowlist_is_enforced(monkeypatch):
    monkeypatch.setattr("app.tools._resolves_to_private_address", AsyncMock(return_value=False))
    with pytest.raises(ToolError):
        await validate_public_url(
            "https://not-allowed.example.org/page",
            config_stub(),
            require_firecrawl_allowlist=True,
        )


def test_research_plan_json_parser_and_fallback():
    assert _json_from_text("```json\n{\"tools\": [\"tavily\"]}\n```") == {"tools": ["tavily"]}
    plan = _heuristic_plan("latest AI market trends", config_stub())
    assert "tavily" in plan["tools"]


@pytest.mark.asyncio
async def test_search_tool_failure_is_recorded_without_crashing_graph_node():
    orchestrator = OrchestratorAgent(config_stub())
    orchestrator.search_agent.tavily = AsyncMock(side_effect=RuntimeError("rate limited"))
    result = await orchestrator.tavily_node({"research_plan": {"queries": ["test"]}})
    assert "Tavily failed: rate limited" in result["errors"]


@pytest.mark.asyncio
async def test_mocked_langgraph_workflow_uses_tools_and_revises(monkeypatch):
    cfg = config_stub(agent_max_iterations=3)
    tool_source = {
        "title": "Example source",
        "url": "https://example.com/source",
        "provider": "tavily",
        "content": "Evidence fact",
        "retrieved_at": "now",
    }
    monkeypatch.setattr("app.agents.tavily_search", AsyncMock(return_value=[tool_source]))
    monkeypatch.setattr(
        "app.agents.wikipedia_search",
        AsyncMock(return_value=[{**tool_source, "url": "https://example.com/wiki", "provider": "wikipedia"}]),
    )
    calls = {"writer": 0, "critic": 0}

    async def fake_llm(config, function_name, message):
        if "Create a research tool plan" in message:
            return '{"tools":["tavily","wikipedia"],"queries":["question"],"urls":[],"firecrawl_after_search":false}'
        if "Summarize the supplied evidence" in message:
            return "Evidence summary [S1] [S2]"
        if "Critique this report" in message:
            calls["critic"] += 1
            passed = calls["critic"] > 1
            return '{"passed": %s, "issues": [], "unsupported_claims": [], "citation_issues": [], "source_quality": [], "missing_information": [], "contradictions": []}' % str(passed).lower()
        calls["writer"] += 1
        return "Report draft [S1] [S2]"

    monkeypatch.setattr("app.agents._tz_call", fake_llm)
    final = await build_graph(cfg).ainvoke(
        {
            "user_query": "question",
            "session_id": "test",
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
            "session_history": [],
        }
    )
    assert final["status"] == "completed"
    assert len(final["sources"]) == 2
    assert final["iteration_count"] == 2
    assert calls["writer"] == 2


@pytest.mark.asyncio
async def test_writer_removes_unknown_citations(monkeypatch):
    monkeypatch.setattr(
        "app.agents._tz_call",
        AsyncMock(return_value="Claim [S1]. Fabricated claim [S99]."),
    )
    writer = WriterAgent(config_stub())
    report = await writer.run(
        "question",
        [{"citation_id": "S1", "title": "Source", "url": "https://example.com"}],
        [{"summary": "Claim [S1]."}],
    )
    assert "[S1]" in report
    assert "S99" not in report


@pytest.mark.asyncio
async def test_critic_rejects_unknown_citation(monkeypatch):
    monkeypatch.setattr(
        "app.agents._tz_call",
        AsyncMock(return_value='{"passed": true, "issues": [], "unsupported_claims": [], "citation_issues": []}'),
    )
    critic = CriticAgent(config_stub())
    result = await critic.run(
        "question",
        "Claim [S99]",
        [{"citation_id": "S1", "title": "Source", "url": "https://example.com", "content": "fact"}],
    )
    assert result["passed"] is False
    assert result["citation_issues"]


def test_critic_revision_loop_is_bounded():
    orchestrator = OrchestratorAgent(config_stub(agent_max_iterations=2))
    assert orchestrator.route_after_critic({"critique": {"passed": False}, "iteration_count": 1}) == "write"
    assert orchestrator.route_after_critic({"critique": {"passed": False}, "iteration_count": 2}) == "finalize"


@pytest.mark.asyncio
async def test_disabled_local_guardrails_allow_test_execution():
    config = config_stub(guardrails_enabled=False)
    assert await validate_input(config, "safe") == (True, "")
    assert await validate_output(config, "safe") == (True, "")


@pytest.mark.asyncio
async def test_guardrail_intervention_blocks_output(monkeypatch):
    config = config_stub(guardrails_enabled=True, bedrock_guardrail_id="id", bedrock_guardrail_version="1")
    monkeypatch.setattr("app.guardrails.with_retry", AsyncMock(return_value={"action": "GUARDRAIL_INTERVENED"}))
    assert (await validate_output(config, "unsafe"))[0] is False


def test_report_generation_preserves_structured_output():
    payload = generate_json_report("topic", "Executive Summary\nFact [S1]", "job", datetime.now(timezone.utc))
    assert payload["topic"] == "topic"
    assert payload["word_count"] == 4
    assert generate_pdf("topic", "report").startswith(b"%PDF")
