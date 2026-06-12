"""Tests for bamboo_executor — the plan execution and synthesis layer.

Coverage:
- execute_plan with a single-tool FAST_PATH plan.
- execute_plan with a two-tool PLAN (order + merged evidence).
- execute_plan with an unknown tool name (graceful error, no raise).
- execute_plan with a failing tool (partial synthesis).
- execute_plan with all tools failing (top-level error message).
- _pick_synthesis_prompt selects the correct prompt for each tool set.
- Conversation history is threaded into the LLM call.
- RAG route uses the documentation-context system prompt.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bamboo.tools.bamboo_executor as ex_mod
from bamboo.tools.bamboo_executor import (
    _SYSTEM_GENERIC,
    _SYSTEM_HARVESTER_TIMESERIES,
    _SYSTEM_JOB,
    _SYSTEM_LOG_ANALYSIS,
    _SYSTEM_RAG,
    _SYSTEM_TASK,
    _pick_synthesis_prompt,
    execute_plan,
)
from bamboo.llm.types import Message
from bamboo.tools.planner import Plan, PlanRoute, ReusePolicy, ToolCall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(
    tool_name: str,
    args: dict[str, Any],
    route: PlanRoute = PlanRoute.FAST_PATH,
) -> Plan:
    """Build a minimal single-tool Plan.

    Args:
        tool_name: Name of the tool to call.
        args: Arguments dict for the tool.
        route: Plan route, default FAST_PATH.

    Returns:
        A validated Plan instance.
    """
    return Plan(
        route=route,
        confidence=0.95,
        tool_calls=[ToolCall(tool=tool_name, arguments=args)],
        reuse_policy=ReusePolicy(),
        explain="test plan",
    )


def _make_two_tool_plan(t1: str, a1: dict, t2: str, a2: dict) -> Plan:
    """Build a two-tool PLAN.

    Args:
        t1: First tool name.
        a1: First tool arguments.
        t2: Second tool name.
        a2: Second tool arguments.

    Returns:
        A validated Plan instance.
    """
    return Plan(
        route=PlanRoute.PLAN,
        confidence=0.85,
        tool_calls=[ToolCall(tool=t1, arguments=a1), ToolCall(tool=t2, arguments=a2)],
        reuse_policy=ReusePolicy(),
        explain="two-tool plan",
    )


def _evidence_result(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Wrap evidence in MCPContent as tools return it.

    Args:
        evidence: Evidence dict to wrap.

    Returns:
        One-element MCPContent list.
    """
    return [{"type": "text", "text": json.dumps({"evidence": evidence})}]


def _text_result(text: str) -> str:
    """Return a plain string as call_llm would return.

    Args:
        text: Text to return.

    Returns:
        The text unchanged (call_llm returns str, not MCPContent).
    """
    return text


def _mock_tool(name: str, call_fn: Any, props: dict | None = None) -> MagicMock:
    """Build a mock tool object with get_definition and call.

    Args:
        name: Tool name for the definition.
        call_fn: Async callable or mock for the call method.
        props: Optional inputSchema properties dict.

    Returns:
        MagicMock configured as a tool object.
    """
    return MagicMock(
        call=call_fn,
        get_definition=MagicMock(return_value={
            "name": name,
            "inputSchema": {
                "type": "object",
                "properties": props or {k: {} for k in [
                    "task_id", "job_id", "query", "context", "top_k"
                ]},
            },
        }),
    )


# ---------------------------------------------------------------------------
# _pick_synthesis_prompt
# ---------------------------------------------------------------------------


class TestPickSynthesisPrompt:
    """Synthesis prompt selection based on called tools."""

    def test_log_analysis_wins(self) -> None:
        """panda_log_analysis prompt is chosen over all others."""
        assert _pick_synthesis_prompt(["panda_log_analysis"]) == _SYSTEM_LOG_ANALYSIS

    def test_log_analysis_beats_job(self) -> None:
        """panda_log_analysis takes priority over panda_job_status."""
        assert _pick_synthesis_prompt(["panda_job_status", "panda_log_analysis"]) == _SYSTEM_LOG_ANALYSIS

    def test_job_status(self) -> None:
        """panda_job_status prompt is chosen without log analysis."""
        assert _pick_synthesis_prompt(["panda_job_status"]) == _SYSTEM_JOB

    def test_task_status(self) -> None:
        """panda_task_status prompt is chosen without job/log tools."""
        assert _pick_synthesis_prompt(["panda_task_status"]) == _SYSTEM_TASK

    def test_doc_search_rag(self) -> None:
        """panda_doc_search selects the RAG prompt."""
        assert _pick_synthesis_prompt(["panda_doc_search"]) == _SYSTEM_RAG

    def test_doc_bm25_rag(self) -> None:
        """panda_doc_bm25 also selects the RAG prompt."""
        assert _pick_synthesis_prompt(["panda_doc_bm25"]) == _SYSTEM_RAG

    def test_unknown_tool_generic(self) -> None:
        """Unknown tool names fall back to the generic prompt."""
        assert _pick_synthesis_prompt(["some_unknown_tool"]) == _SYSTEM_GENERIC

    def test_empty_list_generic(self) -> None:
        """Empty tool list falls back to the generic prompt."""
        assert _pick_synthesis_prompt([]) == _SYSTEM_GENERIC

    def test_harvester_timeseries_prompt(self) -> None:
        """atlas.harvester_timeseries selects the timeseries synthesis prompt."""
        assert _pick_synthesis_prompt(["atlas.harvester_timeseries"]) == _SYSTEM_HARVESTER_TIMESERIES

    def test_harvester_timeseries_not_overridden_by_workers(self) -> None:
        """atlas.harvester_timeseries alone uses timeseries prompt, not workers prompt."""
        from bamboo.tools.bamboo_executor import _SYSTEM_HARVESTER_WORKERS
        result = _pick_synthesis_prompt(["atlas.harvester_timeseries"])
        assert result == _SYSTEM_HARVESTER_TIMESERIES
        assert result != _SYSTEM_HARVESTER_WORKERS


# ---------------------------------------------------------------------------
# execute_plan — single tool FAST_PATH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_path_calls_correct_tool_and_synthesises():
    """FAST_PATH: the named tool is called and the result synthesised."""
    task_mock = AsyncMock(return_value=_evidence_result({"status": "done"}))
    llm_mock = AsyncMock(return_value=_text_result("Task 12345678 finished."))
    plan = _make_plan("panda_task_status", {"task_id": 12345678, "query": "status?"})
    with (
        patch.dict("bamboo.core.TOOLS",
                   {"panda_task_status": _mock_tool("panda_task_status", task_mock)}),
        patch.object(ex_mod, "call_llm", llm_mock),
    ):
        result = await execute_plan(plan, "What is task 12345678 status?", [])
    task_mock.assert_awaited_once_with({"task_id": 12345678, "query": "status?"})
    llm_mock.assert_awaited_once()
    system_arg = llm_mock.call_args[0][0]
    assert "task" in system_arg.lower() or "metadata" in system_arg.lower()
    assert result[0]["type"] == "text"
    assert "Task 12345678 finished." in result[0]["text"]


@pytest.mark.asyncio
async def test_fast_path_log_analysis_uses_correct_prompt():
    """panda_log_analysis tool triggers the log-analysis synthesis prompt."""
    log_mock = AsyncMock(return_value=_evidence_result({"failure_type": "segfault"}))
    llm_mock = AsyncMock(return_value=_text_result("Segfault in payload."))
    plan = _make_plan("panda_log_analysis", {"job_id": 9988776, "query": "why fail?"})
    with (
        patch.dict("bamboo.core.TOOLS",
                   {"panda_log_analysis": _mock_tool("panda_log_analysis", log_mock)}),
        patch.object(ex_mod, "call_llm", llm_mock),
    ):
        await execute_plan(plan, "Why did job 9988776 fail?", [])
    system_arg = llm_mock.call_args[0][0]
    assert ("diagnostic" in system_arg.lower() or
            "failure" in system_arg.lower() or
            "log" in system_arg.lower())


# ---------------------------------------------------------------------------
# execute_plan — two-tool PLAN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_tool_plan_calls_both_in_order():
    """PLAN route: both tools are called in declaration order."""
    call_order: list[str] = []

    async def _task_call(args: dict) -> list:
        call_order.append("task")
        return _evidence_result({"status": "done"})

    async def _log_call(args: dict) -> list:
        call_order.append("log")
        return _evidence_result({"failure": "oom"})

    llm_mock = AsyncMock(return_value=_text_result("Combined answer."))
    plan = _make_two_tool_plan(
        "panda_task_status", {"task_id": 1},
        "panda_log_analysis", {"job_id": 2},
    )
    with (
        patch.dict("bamboo.core.TOOLS", {
            "panda_task_status": _mock_tool("panda_task_status", _task_call),
            "panda_log_analysis": _mock_tool("panda_log_analysis", _log_call),
        }),
        patch.object(ex_mod, "call_llm", llm_mock),
    ):
        result = await execute_plan(plan, "Check task 1 and job 2", [])
    assert call_order == ["task", "log"], "Tools must be called in plan order"
    assert result[0]["type"] == "text"


@pytest.mark.asyncio
async def test_two_tool_plan_merges_evidence_in_user_prompt():
    """Both tools' evidence must appear in the LLM user prompt."""
    task_mock = AsyncMock(return_value=_evidence_result({"status": "done"}))
    log_mock = AsyncMock(return_value=_evidence_result({"failure_type": "timeout"}))
    llm_mock = AsyncMock(return_value=_text_result("Merged answer."))
    plan = _make_two_tool_plan(
        "panda_task_status", {"task_id": 999},
        "panda_log_analysis", {"job_id": 888},
    )
    with (
        patch.dict("bamboo.core.TOOLS", {
            "panda_task_status": _mock_tool("panda_task_status", task_mock),
            "panda_log_analysis": _mock_tool("panda_log_analysis", log_mock),
        }),
        patch.object(ex_mod, "call_llm", llm_mock),
    ):
        await execute_plan(plan, "Check task 999 and job 888", [])
    user_prompt = llm_mock.call_args[0][1]
    assert "panda_task_status" in user_prompt
    assert "panda_log_analysis" in user_prompt


# ---------------------------------------------------------------------------
# execute_plan — unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_and_does_not_raise():
    """An unknown tool name is handled gracefully with an error message."""
    plan = _make_plan("nonexistent_tool_xyz", {"foo": "bar"})
    with (
        patch.dict("bamboo.core.TOOLS", {}),
        patch.object(ex_mod, "find_tool_by_name", return_value=None),
    ):
        result = await execute_plan(plan, "some question", [])
    assert result[0]["type"] == "text"
    assert "fail" in result[0]["text"].lower() or "unknown" in result[0]["text"].lower()


# ---------------------------------------------------------------------------
# execute_plan — tool raises an exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_tool_partial_results_still_synthesised():
    """When one of two tools raises, the other's evidence is still synthesised."""
    good_mock = AsyncMock(return_value=_evidence_result({"status": "done"}))
    bad_mock = AsyncMock(side_effect=RuntimeError("network failure"))
    llm_mock = AsyncMock(return_value=_text_result("Partial answer."))
    plan = _make_two_tool_plan(
        "panda_task_status", {"task_id": 1},
        "panda_log_analysis", {"job_id": 2},
    )
    with (
        patch.dict("bamboo.core.TOOLS", {
            "panda_task_status": _mock_tool("panda_task_status", good_mock),
            "panda_log_analysis": _mock_tool("panda_log_analysis", bad_mock),
        }),
        patch.object(ex_mod, "call_llm", llm_mock),
    ):
        result = await execute_plan(plan, "Check task 1 and job 2", [])
    llm_mock.assert_awaited_once()
    user_prompt = llm_mock.call_args[0][1]
    assert "fail" in user_prompt.lower() or "network failure" in user_prompt.lower()
    assert result[0]["type"] == "text"


@pytest.mark.asyncio
async def test_all_tools_fail_returns_error_message():
    """When every tool call fails, a top-level error message is returned."""
    bad_mock = AsyncMock(side_effect=RuntimeError("complete failure"))
    plan = _make_plan("panda_task_status", {"task_id": 1})
    with patch.dict("bamboo.core.TOOLS",
                    {"panda_task_status": _mock_tool("panda_task_status", bad_mock)}):
        result = await execute_plan(plan, "Check task 1", [])
    assert result[0]["type"] == "text"
    assert "fail" in result[0]["text"].lower()


# ---------------------------------------------------------------------------
# execute_plan — history threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_threaded_into_llm_call():
    """Prior conversation turns are forwarded to call_llm as history."""
    task_mock = AsyncMock(return_value=_evidence_result({"status": "done"}))
    llm_mock = AsyncMock(return_value=_text_result("answer with context"))
    plan = _make_plan("panda_task_status", {"task_id": 555, "query": "q"})
    history: list[Message] = [
        {"role": "user", "content": "What was task 444?"},
        {"role": "assistant", "content": "Task 444 was done."},
    ]
    with (
        patch.dict("bamboo.core.TOOLS",
                   {"panda_task_status": _mock_tool("panda_task_status", task_mock)}),
        patch.object(ex_mod, "call_llm", llm_mock),
    ):
        await execute_plan(plan, "What is task 555 status?", history)
    _, _, passed_history = llm_mock.call_args[0]
    assert passed_history == history


# ---------------------------------------------------------------------------
# execute_plan — RAG route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rag_route_uses_documentation_context_prompt():
    """RAG tools trigger the documentation-context synthesis prompt."""
    rag_mock = AsyncMock(return_value=[{"type": "text", "text": "PanDA Doc Search result."}])
    llm_mock = AsyncMock(return_value="PanDA manages workloads.")
    # RETRIEVE route requires retrieval_query or tool_calls; we supply tool_calls.
    plan = Plan(
        route=PlanRoute.RETRIEVE,
        confidence=0.9,
        tool_calls=[ToolCall(tool="panda_doc_search", arguments={"query": "What is PanDA?", "top_k": 5})],
        reuse_policy=ReusePolicy(),
        explain="rag plan",
    )
    with (
        patch.dict("bamboo.core.TOOLS",
                   {"panda_doc_search": _mock_tool("panda_doc_search", rag_mock)}),
        patch.object(ex_mod, "call_llm", llm_mock),
    ):
        result = await execute_plan(plan, "What is PanDA?", [])
    system_arg = llm_mock.call_args[0][0]
    assert "documentation" in system_arg.lower() or "retrieved" in system_arg.lower()
    assert result[0]["type"] == "text"
