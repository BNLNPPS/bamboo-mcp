"""Tests for BambooAnswerTool routing after the deterministic fast-path refactor.

After the refactor, _route() calls _build_deterministic_plan() for all common
cases and then calls execute_plan() directly — bypassing the LLM planner
entirely. The planner is only invoked when _build_deterministic_plan returns
None (which it never currently does; it covers all four cases). Tests mock
execute_plan at the bamboo.tools.bamboo_answer module level.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bamboo.tools.bamboo_answer import BambooAnswerTool, _build_deterministic_plan
from bamboo.tools.planner import PlanRoute


def _exec_result(text: str) -> list[dict]:
    """Return a fake execute_plan result (one-element MCPContent list)."""
    return [{"type": "text", "text": text}]


def _mock_guard(allowed: bool = True) -> MagicMock:
    """Return a mock topic-guard result."""
    g = MagicMock()
    g.allowed = allowed
    g.reason = "ok" if allowed else "off-topic"
    g.rejection_message = "Off-topic question."
    g.llm_used = False
    return g


# ---------------------------------------------------------------------------
# _build_deterministic_plan unit tests
# ---------------------------------------------------------------------------


def test_no_ids_returns_retrieve_plan():
    """Questions with no IDs produce a RETRIEVE plan with both RAG tools."""
    plan = _build_deterministic_plan("What is PanDA?", None, None)
    assert plan is not None
    assert plan.route == PlanRoute.RETRIEVE
    tools = [tc.tool for tc in plan.tool_calls]
    assert "panda_doc_search" in tools
    assert "panda_doc_bm25" in tools
    # Vector search must come before BM25 (stable ordering from _DEFAULT_DOC_TOOLS)
    assert tools.index("panda_doc_search") < tools.index("panda_doc_bm25")


def test_task_id_returns_task_plan():
    """Questions with a task ID produce a FAST_PATH panda_task_status plan."""
    plan = _build_deterministic_plan("What is task 12345678?", 12345678, None)
    assert plan is not None
    assert plan.route == PlanRoute.FAST_PATH
    assert plan.tool_calls[0].tool == "panda_task_status"
    assert plan.tool_calls[0].arguments["task_id"] == 12345678


def test_job_id_returns_job_plan():
    """Job ID without analysis keywords produces a panda_job_status plan."""
    plan = _build_deterministic_plan("What happened to job 9988776?", None, 9988776)
    assert plan is not None
    assert plan.route == PlanRoute.FAST_PATH
    assert plan.tool_calls[0].tool == "panda_job_status"


def test_job_id_with_analysis_returns_log_plan():
    """Job ID with analysis keywords produces a panda_log_analysis plan."""
    plan = _build_deterministic_plan("Why did job 9988776 fail?", None, 9988776)
    assert plan is not None
    assert plan.route == PlanRoute.FAST_PATH
    assert plan.tool_calls[0].tool == "panda_log_analysis"


def test_jobs_db_question_sets_queue_when_site_present():
    """Jobs-DB fast-path includes 'queue' argument when site is in the question.

    Regression test for the bug where the solo panda_jobs_query fast-path
    omitted the 'queue' argument even when a site name was present, causing
    the LLM to generate ``_queue = 'BNL'`` (exact match) instead of
    ``_queue ILIKE 'BNL%'``, which returned 0 rows.
    """
    plan = _build_deterministic_plan(
        "Show me 10 jobs at BNL that failed with pilot error code 1324", None, None
    )
    assert plan is not None
    assert plan.route == PlanRoute.FAST_PATH
    assert plan.tool_calls[0].tool == "panda_jobs_query"
    args = plan.tool_calls[0].arguments
    assert "queue" in args, "queue argument must be set when site is in question"
    assert args["queue"] == "BNL"


def test_jobs_db_question_no_site_omits_queue():
    """Jobs-DB fast-path omits 'queue' argument when no site is detectable."""
    plan = _build_deterministic_plan(
        "How many failed jobs are there right now?", None, None
    )
    assert plan is not None
    assert plan.route == PlanRoute.FAST_PATH
    assert plan.tool_calls[0].tool == "panda_jobs_query"
    args = plan.tool_calls[0].arguments
    assert "queue" not in args, "queue must be absent when no site is in the question"


# ---------------------------------------------------------------------------
# execute_plan boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_general_question_calls_execute_plan():
    """A question with no ID calls execute_plan with a RETRIEVE plan."""
    exec_mock = AsyncMock(return_value=_exec_result("PanDA is a workload manager."))
    guard_mock = AsyncMock(return_value=_mock_guard(allowed=True))
    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", exec_mock),
    ):
        result = await tool.call({"question": "What is PanDA?"})
    exec_mock.assert_awaited_once()
    plan_arg = exec_mock.call_args[0][0]
    assert plan_arg.route == PlanRoute.RETRIEVE
    assert result[0]["type"] == "text"
    assert "PanDA is a workload manager." in result[0]["text"]


@pytest.mark.asyncio
async def test_task_id_question_uses_task_plan():
    """A question with a task ID calls execute_plan with a task_status plan."""
    exec_mock = AsyncMock(return_value=_exec_result("Task 12345678 is done."))
    guard_mock = AsyncMock(return_value=_mock_guard(allowed=True))
    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", exec_mock),
    ):
        result = await tool.call({"question": "What is the status of task 12345678?"})
    plan_arg = exec_mock.call_args[0][0]
    assert plan_arg.tool_calls[0].tool == "panda_task_status"
    assert plan_arg.tool_calls[0].arguments["task_id"] == 12345678
    assert result[0]["type"] == "text"


@pytest.mark.asyncio
async def test_job_id_question_uses_job_plan():
    """A question with a job ID calls execute_plan with a job_status plan."""
    exec_mock = AsyncMock(return_value=_exec_result("Job 6837798305 failed."))
    guard_mock = AsyncMock(return_value=_mock_guard(allowed=True))
    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", exec_mock),
    ):
        result = await tool.call({"question": "What happened to job 6837798305?"})
    plan_arg = exec_mock.call_args[0][0]
    assert plan_arg.tool_calls[0].tool == "panda_job_status"
    assert plan_arg.tool_calls[0].arguments["job_id"] == 6837798305
    assert result[0]["type"] == "text"


@pytest.mark.asyncio
async def test_off_topic_question_blocked_before_execute():
    """An off-topic question is rejected by the guard; execute_plan never called."""
    exec_mock = AsyncMock(return_value=_exec_result("should not reach"))
    guard_mock = AsyncMock(return_value=_mock_guard(allowed=False))
    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", exec_mock),
    ):
        result = await tool.call({"question": "What is the stock price of CERN?"})
    exec_mock.assert_not_called()
    assert result[0]["type"] == "text"
    assert "Off-topic" in result[0]["text"]


@pytest.mark.asyncio
async def test_bypass_routing_skips_guard_and_execute():
    """bypass_routing=True skips topic guard and execute_plan; goes direct to LLM."""
    llm_reply = "Direct LLM answer."
    llm_mock = AsyncMock(return_value=[{"type": "text", "text": llm_reply}])
    guard_mock = AsyncMock(return_value=_mock_guard(allowed=True))
    exec_mock = AsyncMock(return_value=_exec_result("should not reach"))
    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", exec_mock),
        patch("bamboo.tools.bamboo_answer.bamboo_llm_answer_tool") as mock_llm,
    ):
        mock_llm.call = llm_mock
        result = await tool.call({"question": "hello", "bypass_routing": True})
    guard_mock.assert_not_awaited()
    exec_mock.assert_not_called()
    llm_mock.assert_awaited_once()
    assert llm_reply in result[0]["text"]


@pytest.mark.asyncio
async def test_history_threaded_into_execute_plan():
    """Prior conversation turns are forwarded to execute_plan as the history arg."""
    exec_mock = AsyncMock(return_value=_exec_result("follow-up answer"))
    guard_mock = AsyncMock(return_value=_mock_guard(allowed=True))
    messages = [
        {"role": "user", "content": "What is PanDA?"},
        {"role": "assistant", "content": "PanDA is a workload manager."},
        {"role": "user", "content": "How do I submit a job?"},
    ]
    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", exec_mock),
    ):
        await tool.call({"messages": messages})
    exec_mock.assert_awaited_once()
    # execute_plan(plan, question, history) — history is positional arg 2
    _, question_arg, history_arg = exec_mock.call_args[0]
    assert question_arg == "How do I submit a job?"
    assert any(m.get("role") == "assistant" for m in history_arg)
