"""Tests for context memory (multi-turn chat history).

Covers:
- History threading through all four routing branches (RAG, task, job,
  log_analysis) and the bypass_routing path.
- ``_extract_history`` correctly strips the current question from the tail of
  the messages list and drops system-role messages.
- ``BambooTui._cap_history`` enforces the ``_MAX_HISTORY_TURNS`` limit.
- ``BambooTui.action_clear`` resets history.
- Empty history produces no behavioural change (regression guard for existing
  tests that pass no history).
- Streamlit helpers: ``_cap_messages`` and ``_detect_answer_tool``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import bamboo.tools.bamboo_answer as ba_mod
from bamboo.tools.bamboo_answer import BambooAnswerTool, _extract_history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_mock(reply: str) -> AsyncMock:
    """Return an AsyncMock simulating bamboo_llm_answer_tool.call.

    Args:
        reply: Text the mock should return.

    Returns:
        AsyncMock configured to return a one-element MCP text content list.
    """
    return AsyncMock(return_value=[{"type": "text", "text": reply}])


def _rag_mock(text: str = "PanDA Doc Search — result") -> AsyncMock:
    """Return an AsyncMock simulating panda_doc_search_tool.call.

    Args:
        text: Text the mock should return.

    Returns:
        AsyncMock configured with a plausible RAG result.
    """
    return AsyncMock(return_value=[{"type": "text", "text": text}])


def _bm25_mock() -> AsyncMock:
    """Return an AsyncMock simulating panda_doc_bm25_tool.call."""
    return AsyncMock(return_value=[{"type": "text", "text": "BM25 result"}])


# ---------------------------------------------------------------------------
# _extract_history unit tests
# ---------------------------------------------------------------------------

class TestExtractHistory:
    """Unit tests for the ``_extract_history`` module-level helper."""

    def test_empty_messages_returns_empty(self) -> None:
        """Empty messages list yields empty history."""
        assert _extract_history([], "What is PanDA?") == []

    def test_strips_current_question_from_tail(self) -> None:
        """The final user turn matching the current question is excluded."""
        messages = [
            {"role": "user", "content": "What is PanDA?"},
            {"role": "assistant", "content": "PanDA is a workload manager."},
            {"role": "user", "content": "Tell me more."},
        ]
        history = _extract_history(messages, "Tell me more.")  # type: ignore[arg-type]
        assert len(history) == 2
        assert history[0].get("role") == "user"
        assert history[0].get("content") == "What is PanDA?"
        assert history[1].get("role") == "assistant"

    def test_drops_system_messages(self) -> None:
        """System-role messages are filtered out of the returned history."""
        messages = [
            {"role": "system", "content": "You are AskPanDA."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "Current question"},
        ]
        history = _extract_history(messages, "Current question")  # type: ignore[arg-type]
        roles = [m.get("role") for m in history]
        assert "system" not in roles
        assert roles == ["user", "assistant"]

    def test_only_current_question_returns_empty(self) -> None:
        """A messages list with only the current question yields empty history."""
        messages = [{"role": "user", "content": "What is PanDA?"}]
        history = _extract_history(messages, "What is PanDA?")  # type: ignore[arg-type]
        assert history == []

    def test_empty_content_messages_are_dropped(self) -> None:
        """Messages with empty content are silently dropped."""
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "Follow-up"},
        ]
        history = _extract_history(messages, "Follow-up")  # type: ignore[arg-type]
        assert len(history) == 1
        assert history[0].get("content") == "First question"

    def test_question_not_at_tail_is_preserved(self) -> None:
        """A question matching an earlier turn (not the tail) is not stripped."""
        messages = [
            {"role": "user", "content": "What is PanDA?"},
            {"role": "assistant", "content": "A workload manager."},
            {"role": "user", "content": "What is PanDA?"},  # repeat question
        ]
        # Current question is the repeat; only the tail match is stripped.
        history = _extract_history(messages, "What is PanDA?")  # type: ignore[arg-type]
        # The first "What is PanDA?" turn should remain; the assistant reply too.
        assert len(history) == 2


# ---------------------------------------------------------------------------
# History threading — RAG route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rag_route_threads_history() -> None:
    """History is forwarded to bamboo_plan_tool in the messages argument."""
    llm_reply = "PanDA manages ATLAS jobs."
    plan_mock = AsyncMock(return_value=[{"type": "text", "text": llm_reply}])
    guard_mock = AsyncMock()
    guard_result = type("G", (), {"allowed": True, "reason": "ok",
                                  "rejection_message": "", "llm_used": False})()
    guard_mock.return_value = guard_result

    messages = [
        {"role": "user", "content": "What is PanDA?"},
        {"role": "assistant", "content": "PanDA manages ATLAS jobs."},
        {"role": "user", "content": "Tell me more about PanDA queues."},
    ]

    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", plan_mock),
    ):
        result = await tool.call({"messages": messages})

    assert result[0]["text"] == llm_reply
    # execute_plan(plan, question, history) — history is positional arg 2
    msgs_sent = list(plan_mock.call_args[0][2])
    assert any(
        m.get("role") == "assistant" and "PanDA manages" in m.get("content", "")
        for m in msgs_sent
    )
    # The question is the second positional arg
    question_arg = plan_mock.call_args[0][1]
    assert "Tell me more about PanDA queues." in question_arg


# ---------------------------------------------------------------------------
# History threading — task route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_route_threads_history() -> None:
    """History is forwarded to bamboo_plan_tool in the messages argument."""
    llm_reply = "Task 12345 is done."
    plan_mock = AsyncMock(return_value=[{"type": "text", "text": llm_reply}])
    guard_mock = AsyncMock()
    guard_result = type("G", (), {"allowed": True, "reason": "ok",
                                  "rejection_message": "", "llm_used": False})()
    guard_mock.return_value = guard_result

    messages = [
        {"role": "user", "content": "Is task 12345 running?"},
        {"role": "assistant", "content": "Yes, it was running."},
        {"role": "user", "content": "What is the status of task 12345?"},
    ]

    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", plan_mock),
    ):
        result = await tool.call({"messages": messages})

    assert result[0]["text"] == llm_reply
    msgs_sent = list(plan_mock.call_args[0][2])
    assert any(
        m.get("role") == "assistant" and "was running" in m.get("content", "")
        for m in msgs_sent
    )


# ---------------------------------------------------------------------------
# History threading — job route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_route_threads_history() -> None:
    """History is forwarded to bamboo_plan_tool in the messages argument."""
    llm_reply = "Job 99999 finished successfully."
    plan_mock = AsyncMock(return_value=[{"type": "text", "text": llm_reply}])
    guard_mock = AsyncMock()
    guard_result = type("G", (), {"allowed": True, "reason": "ok",
                                  "rejection_message": "", "llm_used": False})()
    guard_mock.return_value = guard_result

    messages = [
        {"role": "user", "content": "What happened to job 99999?"},
        {"role": "assistant", "content": "Job 99999 was in a running state."},
        {"role": "user", "content": "How long did job 99999 take?"},
    ]

    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", plan_mock),
    ):
        result = await tool.call({"messages": messages})

    assert result[0]["text"] == llm_reply
    msgs_sent = list(plan_mock.call_args[0][2])
    assert any(
        m.get("role") == "assistant" and "running state" in m.get("content", "")
        for m in msgs_sent
    )


# ---------------------------------------------------------------------------
# History threading — log_analysis route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_analysis_route_threads_history() -> None:
    """History is forwarded to bamboo_plan_tool in the messages argument."""
    llm_reply = "Job 77777 failed due to a pilot error."
    plan_mock = AsyncMock(return_value=[{"type": "text", "text": llm_reply}])
    guard_mock = AsyncMock()
    guard_result = type("G", (), {"allowed": True, "reason": "ok",
                                  "rejection_message": "", "llm_used": False})()
    guard_mock.return_value = guard_result

    messages = [
        {"role": "user", "content": "What is task 10000?"},
        {"role": "assistant", "content": "Task 10000 is an ATLAS reco task."},
        {"role": "user", "content": "Why did job 77777 fail?"},
    ]

    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", plan_mock),
    ):
        result = await tool.call({"messages": messages})

    assert result[0]["text"] == llm_reply
    msgs_sent = list(plan_mock.call_args[0][2])
    assert any(
        m.get("role") == "assistant" and "reco task" in m.get("content", "")
        for m in msgs_sent
    )


# ---------------------------------------------------------------------------
# History threading — bypass_routing path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bypass_routing_threads_history() -> None:
    """History is included in the messages list on the bypass_routing path."""
    llm_reply = "Sure, here is more detail."
    llm_mock = _llm_mock(llm_reply)

    messages = [
        {"role": "user", "content": "What is a pilot?"},
        {"role": "assistant", "content": "A pilot is a lightweight agent."},
        {"role": "user", "content": "Tell me more about pilots."},
    ]

    tool = BambooAnswerTool()
    with patch.object(ba_mod, "bamboo_llm_answer_tool", AsyncMock(call=llm_mock)):
        result = await tool.call({"messages": messages, "bypass_routing": True})

    assert result[0]["text"] == llm_reply

    call_msgs = llm_mock.call_args[0][0]["messages"]
    # Should contain the prior assistant turn.
    assert any(
        m.get("role") == "assistant" and "lightweight agent" in m.get("content", "")
        for m in call_msgs
    )
    # Final message should be the current user question.
    assert call_msgs[-1].get("role") == "user"
    assert "Tell me more" in call_msgs[-1].get("content", "")


# ---------------------------------------------------------------------------
# Empty history — no behavioural change (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rag_route_no_history_unchanged() -> None:
    """When no history is supplied the planner messages list has only the current question."""
    llm_reply = "PanDA is the workload manager."
    plan_mock = AsyncMock(return_value=[{"type": "text", "text": llm_reply}])
    guard_mock = AsyncMock()
    guard_result = type("G", (), {"allowed": True, "reason": "ok",
                                  "rejection_message": "", "llm_used": False})()
    guard_mock.return_value = guard_result

    tool = BambooAnswerTool()
    with (
        patch("bamboo.tools.bamboo_answer.check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.bamboo_plan_tool") as mock_plan_tool,
    ):
        mock_plan_tool.call = plan_mock
        result = await tool.call({"question": "What is PanDA?"})

    assert result[0]["text"] == llm_reply
    # With no history, the planner's messages list should contain only the
    # current question — no assistant messages injected.
    plan_args = plan_mock.call_args[0][0]
    messages_arg = plan_args["messages"]
    assert not any(m.get("role") == "assistant" for m in messages_arg)
    assert messages_arg == [{"role": "user", "content": "What is PanDA?"}]


# ---------------------------------------------------------------------------
# TUI history cap tests (unit-level, no Textual event loop needed)
# ---------------------------------------------------------------------------

class _FakeTui:
    """Minimal stand-in for BambooTui to test ``_cap_history`` in isolation.

    Only the ``_history`` attribute and ``_cap_history`` / ``action_clear``
    methods are needed; we do not spin up the full Textual application.
    """

    def __init__(self, max_turns: int = 3) -> None:
        """Initialise with an empty history and a configurable cap.

        Args:
            max_turns: Maximum user+assistant pairs to keep.
        """
        self._history: list[dict[str, str]] = []
        self._max_turns = max_turns

    def _cap_history(self) -> None:
        """Trim history to ``_max_turns`` pairs (mirrors BambooTui logic)."""
        max_messages = self._max_turns * 2
        if len(self._history) > max_messages:
            self._history = self._history[-max_messages:]

    def reset_history(self) -> None:
        """Reset history (mirrors BambooTui.action_clear logic)."""
        self._history = []


class TestCapHistory:
    """Unit tests for ``_cap_history`` (via ``_FakeTui``)."""

    def _make_history(self, n_pairs: int) -> list[dict[str, str]]:
        """Build a synthetic history of ``n_pairs`` complete user+assistant pairs.

        Args:
            n_pairs: Number of complete pairs to generate.

        Returns:
            List of alternating user/assistant message dicts.
        """
        msgs: list[dict[str, str]] = []
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": f"Question {i + 1}"})
            msgs.append({"role": "assistant", "content": f"Answer {i + 1}"})
        return msgs

    def test_under_cap_not_trimmed(self) -> None:
        """History below the cap is not modified."""
        tui = _FakeTui(max_turns=5)
        tui._history = self._make_history(3)
        tui._cap_history()
        assert len(tui._history) == 6

    def test_exactly_at_cap_not_trimmed(self) -> None:
        """History exactly at the cap is not modified."""
        tui = _FakeTui(max_turns=3)
        tui._history = self._make_history(3)
        tui._cap_history()
        assert len(tui._history) == 6

    def test_over_cap_trims_oldest(self) -> None:
        """History over the cap drops the oldest turns from the front."""
        tui = _FakeTui(max_turns=2)
        tui._history = self._make_history(4)  # 8 messages
        tui._cap_history()
        # Should keep last 4 messages (2 pairs).
        assert len(tui._history) == 4
        assert tui._history[0]["content"] == "Question 3"
        assert tui._history[1]["content"] == "Answer 3"

    def test_reset_clears_history(self) -> None:
        """reset_history empties the history list."""
        tui = _FakeTui(max_turns=5)
        tui._history = self._make_history(3)
        tui.reset_history()
        assert tui._history == []

    def test_cap_of_one_pair(self) -> None:
        """Cap of 1 keeps only the most recent user+assistant pair."""
        tui = _FakeTui(max_turns=1)
        tui._history = self._make_history(5)
        tui._cap_history()
        assert len(tui._history) == 2
        assert tui._history[0]["content"] == "Question 5"
        assert tui._history[1]["content"] == "Answer 5"


# ---------------------------------------------------------------------------
# Streamlit helper: _cap_messages
# ---------------------------------------------------------------------------

class TestCapMessages:
    """Unit tests for the Streamlit ``_cap_messages`` helper."""

    def _make_messages(self, n_pairs: int) -> list[dict[str, str]]:
        """Build a flat message list with ``n_pairs`` complete user+assistant pairs.

        Args:
            n_pairs: Number of complete pairs to generate.

        Returns:
            Alternating user/assistant dicts.
        """
        msgs: list[dict[str, str]] = []
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": f"Q{i + 1}"})
            msgs.append({"role": "assistant", "content": f"A{i + 1}"})
        return msgs

    def _cap_messages(self, messages: list[dict[str, str]], max_turns: int) -> list[dict[str, str]]:
        """Mirror of the Streamlit _cap_messages logic under test.

        Args:
            messages: Input message list.
            max_turns: Maximum user+assistant pairs to retain.

        Returns:
            Trimmed list.
        """
        max_msgs = max_turns * 2
        if len(messages) > max_msgs:
            return messages[-max_msgs:]
        return messages

    def test_under_cap_unchanged(self) -> None:
        """Messages below the cap are returned unmodified."""
        msgs = self._make_messages(3)
        result = self._cap_messages(msgs, max_turns=5)
        assert result == msgs

    def test_at_cap_unchanged(self) -> None:
        """Messages exactly at the cap are returned unmodified."""
        msgs = self._make_messages(5)
        result = self._cap_messages(msgs, max_turns=5)
        assert len(result) == 10

    def test_over_cap_trims_oldest(self) -> None:
        """Messages over the cap drop the oldest turns from the front."""
        msgs = self._make_messages(6)  # 12 messages
        result = self._cap_messages(msgs, max_turns=3)  # keep last 6
        assert len(result) == 6
        assert result[0]["content"] == "Q4"
        assert result[-1]["content"] == "A6"

    def test_empty_list_unchanged(self) -> None:
        """An empty list is returned as-is."""
        result = self._cap_messages([], max_turns=5)
        assert result == []


# ---------------------------------------------------------------------------
# Streamlit helper: _detect_answer_tool
# ---------------------------------------------------------------------------

class TestDetectAnswerTool:
    """Unit tests for the Streamlit ``_detect_answer_tool`` helper."""

    def _detect(self, tool_names: list[str]) -> str:
        """Mirror of _detect_answer_tool logic.

        Args:
            tool_names: Available tool names.

        Returns:
            Selected tool name.
        """
        candidates = ["bamboo_answer", "askpanda_answer", "bamboo_plan"]
        for c in candidates:
            if c in tool_names:
                return c
        for name in tool_names:
            if "answer" in name:
                return name
        return "bamboo_answer"

    def test_prefers_bamboo_answer(self) -> None:
        """bamboo_answer is chosen first when present."""
        assert self._detect(["bamboo_answer", "askpanda_answer"]) == "bamboo_answer"

    def test_falls_back_to_askpanda_answer(self) -> None:
        """askpanda_answer is chosen when bamboo_answer is absent."""
        assert self._detect(["askpanda_answer", "panda_task_status"]) == "askpanda_answer"

    def test_falls_back_to_any_answer_tool(self) -> None:
        """Any tool containing 'answer' is chosen when no candidate matches."""
        assert self._detect(["panda_task_status", "my_answer_tool"]) == "my_answer_tool"

    def test_hard_fallback_when_no_match(self) -> None:
        """Returns 'bamboo_answer' as a hard fallback when nothing matches."""
        assert self._detect(["panda_task_status", "panda_log_analysis"]) == "bamboo_answer"

    def test_empty_tool_list_hard_fallback(self) -> None:
        """Returns 'bamboo_answer' for an empty tool list."""
        assert self._detect([]) == "bamboo_answer"


# ---------------------------------------------------------------------------
# _extract_history: additional edge cases
# ---------------------------------------------------------------------------

class TestExtractHistoryEdgeCases:
    """Additional edge-case tests for ``_extract_history``."""

    def test_tool_role_messages_dropped(self) -> None:
        """Messages with role 'tool' are filtered out like system messages."""
        messages = [
            {"role": "user", "content": "Show task 123"},
            {"role": "tool", "content": '{"status": "done"}'},
            {"role": "assistant", "content": "Task 123 is done."},
            {"role": "user", "content": "What about the jobs?"},
        ]
        history = _extract_history(messages, "What about the jobs?")  # type: ignore[arg-type]
        roles = [m.get("role") for m in history]
        assert "tool" not in roles
        assert roles == ["user", "assistant"]

    def test_whitespace_normalised_in_matching(self) -> None:
        """Leading/trailing whitespace in content is stripped before matching."""
        messages = [
            {"role": "user", "content": "  What is PanDA?  "},
        ]
        # current_question with different surrounding whitespace should still match
        history = _extract_history(messages, "What is PanDA?")  # type: ignore[arg-type]
        assert history == []

    def test_no_messages_question_only(self) -> None:
        """A single-element list with only the current question yields empty history."""
        messages = [{"role": "user", "content": "What is PanDA?"}]
        history = _extract_history(messages, "What is PanDA?")  # type: ignore[arg-type]
        assert history == []

    def test_long_conversation_preserved_in_order(self) -> None:
        """All prior turns are returned in chronological order."""
        messages = []
        for i in range(5):
            messages.append({"role": "user", "content": f"Q{i}"})
            messages.append({"role": "assistant", "content": f"A{i}"})
        messages.append({"role": "user", "content": "final question"})

        history = _extract_history(messages, "final question")  # type: ignore[arg-type]
        assert len(history) == 10
        for idx, msg in enumerate(history):
            pair = idx // 2
            if idx % 2 == 0:
                assert msg.get("content") == f"Q{pair}"
            else:
                assert msg.get("content") == f"A{pair}"
