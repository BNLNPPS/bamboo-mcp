"""Tests for the two-stage topic guard (topic_guard.py).

All LLM calls are monkeypatched.  No real LLM credentials are needed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bamboo.tools.topic_guard import GuardResult, check_topic


# ---------------------------------------------------------------------------
# Stage 1 — keyword fast-path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyword_allow_panda():
    """Questions containing 'panda' are allowed immediately without LLM."""
    result = await check_topic("What is PanDA?")
    assert result.allowed is True
    assert result.reason == "keyword_allow"
    assert result.llm_used is False


@pytest.mark.asyncio
async def test_keyword_allow_task():
    """Questions about tasks are allowed immediately."""
    result = await check_topic("Why did task 12345678 fail?")
    assert result.allowed is True
    assert result.reason == "keyword_allow"
    assert result.llm_used is False


@pytest.mark.asyncio
async def test_keyword_allow_atlas():
    """Questions mentioning ATLAS are allowed immediately."""
    result = await check_topic("How does ATLAS brokerage work?")
    assert result.allowed is True
    assert result.reason == "keyword_allow"
    assert result.llm_used is False


@pytest.mark.asyncio
async def test_keyword_allow_cgsim():
    """Questions mentioning CGSim are allowed immediately."""
    result = await check_topic("How does CGSim work?")
    assert result.allowed is True
    assert result.reason == "keyword_allow"
    assert result.llm_used is False


@pytest.mark.asyncio
async def test_keyword_allow_simgrid():
    """Questions mentioning SimGrid are allowed immediately."""
    result = await check_topic("What is the SimGrid netzone model?")
    assert result.allowed is True
    assert result.reason == "keyword_allow"
    assert result.llm_used is False


@pytest.mark.asyncio
async def test_keyword_allow_cgsim_plugin_method():
    """Questions about CGSim plugin API methods are allowed immediately."""
    result = await check_topic("What arguments does assignJob take?")
    assert result.allowed is True
    assert result.reason == "keyword_allow"
    assert result.llm_used is False


@pytest.mark.asyncio
async def test_keyword_allow_cgsim_calibration():
    """Questions about CGSim calibration are allowed immediately."""
    result = await check_topic("How does CGSim calibrate job wall time?")
    assert result.allowed is True
    assert result.reason == "keyword_allow"
    assert result.llm_used is False


@pytest.mark.asyncio
async def test_rejection_message_mentions_cgsim():
    """Rejection message includes CGSim so users know it is a supported domain."""
    from bamboo.tools.topic_guard import _REJECTION_MESSAGE
    assert "CGSim" in _REJECTION_MESSAGE


@pytest.mark.asyncio
async def test_keyword_deny_recipe():
    """Clearly off-topic questions are denied immediately without LLM."""
    result = await check_topic("Give me a recipe for chocolate cake")
    assert result.allowed is False
    assert result.reason == "keyword_deny"
    assert result.llm_used is False
    assert result.rejection_message != ""


@pytest.mark.asyncio
async def test_keyword_deny_sports():
    """Sports questions are denied immediately."""
    result = await check_topic("Who won the football match last night?")
    assert result.allowed is False
    assert result.reason == "keyword_deny"
    assert result.llm_used is False


# ---------------------------------------------------------------------------
# Stage 2 — LLM classifier (ambiguous questions)
# ---------------------------------------------------------------------------

def _mock_llm_verdict(verdict: str) -> None:
    """Patch the LLM runtime to return a given verdict string."""
    mock_resp = MagicMock()
    mock_resp.text = verdict

    mock_client = MagicMock()
    mock_client.generate = AsyncMock(return_value=mock_resp)

    mock_manager = MagicMock()
    mock_manager.get_client = AsyncMock(return_value=mock_client)

    mock_registry = MagicMock()
    mock_registry.get = MagicMock(return_value=MagicMock())

    mock_selector = MagicMock()
    mock_selector.fast_profile = "fast"
    mock_selector.registry = mock_registry

    patch("bamboo.tools.topic_guard.get_llm_selector", return_value=mock_selector).start()
    patch("bamboo.tools.topic_guard.get_llm_manager", return_value=mock_manager).start()


def _build_llm_mocks(verdict: str):
    """Return (mock_selector, mock_manager) patched to return the given verdict."""
    mock_resp = MagicMock()
    mock_resp.text = verdict
    mock_client = MagicMock()
    mock_client.generate = AsyncMock(return_value=mock_resp)
    mock_manager = MagicMock()
    mock_manager.get_client = AsyncMock(return_value=mock_client)
    mock_registry = MagicMock()
    mock_registry.get = MagicMock(return_value=MagicMock())
    mock_selector = MagicMock()
    mock_selector.fast_profile = "fast"
    mock_selector.registry = mock_registry
    return mock_selector, mock_manager


@pytest.mark.asyncio
async def test_llm_allow_ambiguous_python():
    """Ambiguous question ('what is Python?') is escalated to LLM; ALLOW returned."""
    mock_selector, mock_manager = _build_llm_mocks("ALLOW")
    with (
        patch("bamboo.tools.topic_guard.get_llm_selector", return_value=mock_selector),
        patch("bamboo.tools.topic_guard.get_llm_manager", return_value=mock_manager),
    ):
        result = await check_topic("What is Python?")

    assert result.allowed is True
    assert result.reason == "llm_allow"
    assert result.llm_used is True


@pytest.mark.asyncio
async def test_llm_deny_ambiguous_offopic():
    """Ambiguous but off-topic question is escalated to LLM; DENY returned."""
    mock_selector, mock_manager = _build_llm_mocks("DENY")
    with (
        patch("bamboo.tools.topic_guard.get_llm_selector", return_value=mock_selector),
        patch("bamboo.tools.topic_guard.get_llm_manager", return_value=mock_manager),
    ):
        result = await check_topic("How do I bake sourdough bread?")

    assert result.allowed is False
    assert result.reason == "llm_deny"
    assert result.llm_used is True
    assert result.rejection_message != ""


@pytest.mark.asyncio
async def test_llm_failure_fails_open():
    """If the LLM classifier raises, the guard fails open (allows the question)."""
    with patch("bamboo.tools.topic_guard.get_llm_selector", side_effect=RuntimeError("LLM not ready")):
        result = await check_topic("How does caching work?")

    assert result.allowed is True
    assert result.reason == "llm_allow"
    assert result.llm_used is True


# ---------------------------------------------------------------------------
# Integration — guard wired into bamboo_answer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bamboo_answer_rejects_offtopic_before_any_tool():
    """bamboo_answer returns the rejection message without calling any tool."""
    import bamboo.tools.bamboo_answer as ba_mod
    from bamboo.tools.bamboo_answer import BambooAnswerTool

    llm_mock = AsyncMock()
    guard_mock = AsyncMock(return_value=GuardResult(
        allowed=False,
        reason="keyword_deny",
        llm_used=False,
        rejection_message="I can only answer questions about PanDA.",
    ))

    plan_mock = AsyncMock(return_value=[{"type": "text", "text": "should not reach"}])
    tool = BambooAnswerTool()
    with (
        patch.object(ba_mod, "check_topic", guard_mock),
        patch.object(ba_mod, "bamboo_plan_tool") as mock_pt,
        patch.object(ba_mod, "bamboo_llm_answer_tool", AsyncMock(call=llm_mock)),
    ):
        mock_pt.call = plan_mock
        result = await tool.call({"question": "Give me a recipe for pasta"})

    guard_mock.assert_awaited_once_with("Give me a recipe for pasta")
    plan_mock.assert_not_called()
    llm_mock.assert_not_awaited()
    assert "PanDA" in result[0]["text"]


@pytest.mark.asyncio
async def test_bamboo_answer_passes_ontopic_through_guard():
    """bamboo_answer proceeds normally when the guard allows the question."""
    import bamboo.tools.bamboo_answer as ba_mod
    from bamboo.tools.bamboo_answer import BambooAnswerTool

    guard_mock = AsyncMock(return_value=GuardResult(
        allowed=True, reason="keyword_allow", llm_used=False
    ))
    plan_mock = AsyncMock(
        return_value=[{"type": "text", "text": "PanDA is the workload manager."}]
    )
    tool = BambooAnswerTool()
    with (
        patch.object(ba_mod, "check_topic", guard_mock),
        patch("bamboo.tools.bamboo_answer.execute_plan", plan_mock),
    ):
        result = await tool.call({"question": "What is PanDA?"})

    guard_mock.assert_awaited_once()
    plan_mock.assert_awaited_once()
    assert "workload manager" in result[0]["text"]
