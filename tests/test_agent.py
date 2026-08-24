# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Tests for interfaces/agent/agent.py.

The mcp SDK is an optional dependency that may not be present in all test
environments.  Rather than stubbing it at module level (which leaks into
other tests and corrupts their sys.modules), we use a session-scoped
autouse fixture that installs minimal stubs via monkeypatch only for the
duration of this test session, and only for the sub-modules that are not
already present.  This is the same approach used in test_doc_rag.py.

Coverage targets
----------------
* :func:`_extract_json_block`    — JSON extraction from fenced / plain text.
* :func:`_truncate_observation`  — truncation boundary.
* :func:`_observation_from_result` — content block extraction.
* :class:`_ToolSelection`        — Pydantic validation.
* :class:`_EvalResult`           — Pydantic validation + confidence clamping.
* :class:`AgentMemory`           — history serialisation, observation text,
                                   truncated flag.
* :class:`AgentStep` / :class:`AgentResult` — dataclass construction.
* :class:`BambooAgent` — full run loop with a mocked MCP client:
  - normal completion (evaluator satisfied on first step),
  - multi-step completion (evaluator satisfied on second step),
  - early synthesis via ``should_synthesise=True``,
  - max_steps truncation,
  - tool call failure handling,
  - parse error fallbacks for reasoning and evaluation LLM calls,
  - zero-tools edge case,
  - confidence-below-threshold continuation,
  - field type assertions on :class:`AgentResult`.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import agent code.
# mcp stubs are injected by conftest.pytest_configure before collection.
# The conftest also appends repo_root to sys.path so interfaces/ is importable.
# ---------------------------------------------------------------------------

from interfaces.agent.agent import (  # noqa: E402
    AgentMemory,
    AgentResult,
    AgentStep,
    BambooAgent,
    _EvalResult,
    _ToolSelection,
    _extract_json_block,
    _observation_from_result,
    _truncate_observation,
)
from interfaces.shared.mcp_client import MCPAsyncClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mcp_result(text: str) -> MagicMock:
    """Build a minimal MCP tool result mock with a single text content block.

    Args:
        text: Text to include in the content block.

    Returns:
        MagicMock with a ``content`` list containing one text block.
    """
    block = MagicMock()
    block.type = "text"
    block.text = text
    result = MagicMock()
    result.content = [block]
    return result


def _make_tools_result(names: list[str]) -> MagicMock:
    """Build a minimal ``list_tools`` result mock.

    Args:
        names: Tool names to expose.

    Returns:
        MagicMock with a ``tools`` attribute listing the named tools.
    """
    tools = []
    for name in names:
        t = MagicMock()
        t.name = name
        t.description = f"Description of {name}"
        tools.append(t)
    result = MagicMock()
    result.tools = tools
    return result


def _make_client(
    *,
    tools: list[str] | None = None,
    llm_responses: list[str] | None = None,
    tool_responses: dict[str, str] | None = None,
) -> MagicMock:
    """Build a minimal async MCP client mock.

    ``bamboo_llm_answer`` calls consume ``llm_responses`` in order; all
    other tool calls look up ``tool_responses`` by name.

    Args:
        tools: Tool names returned by ``list_tools``.
        llm_responses: Ordered response strings for ``bamboo_llm_answer``.
        tool_responses: Mapping of tool_name → observation text for non-LLM tools.

    Returns:
        Configured MagicMock acting as an :class:`MCPAsyncClient`.
    """
    client = MagicMock(spec=MCPAsyncClient)
    client.list_tools = AsyncMock(return_value=_make_tools_result(tools or ["cric_query"]))

    _llm_idx: list[int] = [0]
    llm_resp_list = list(llm_responses or [])
    tool_resp_map = dict(tool_responses or {})

    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        if name == "bamboo_llm_answer":
            if _llm_idx[0] < len(llm_resp_list):
                text = llm_resp_list[_llm_idx[0]]
                _llm_idx[0] += 1
            else:
                text = '{"sufficient": true, "confidence": 0.99}'
            return _make_mcp_result(text)
        obs = tool_resp_map.get(name, f'{{"result": "from {name}"}}')
        return _make_mcp_result(obs)

    client.call_tool = _call_tool
    return client


# ---------------------------------------------------------------------------
# _extract_json_block
# ---------------------------------------------------------------------------


class TestExtractJsonBlock:
    """Tests for _extract_json_block."""

    def test_plain_json(self) -> None:
        """Plain JSON is returned verbatim."""
        raw = '{"tool_name": "foo", "tool_args": {}}'
        assert _extract_json_block(raw) == raw

    def test_fenced_json(self) -> None:
        """JSON wrapped in ```json fences is extracted."""
        raw = '```json\n{"a": 1}\n```'
        result = _extract_json_block(raw)
        assert json.loads(result) == {"a": 1}

    def test_plain_fence(self) -> None:
        """JSON wrapped in plain ``` fences is extracted."""
        raw = "```\n{\"x\": 2}\n```"
        result = _extract_json_block(raw)
        assert json.loads(result) == {"x": 2}

    def test_surrounding_text(self) -> None:
        """JSON surrounded by prose is extracted via brace scanning."""
        raw = 'Here is the output: {"k": "v"} done.'
        result = _extract_json_block(raw)
        assert json.loads(result) == {"k": "v"}

    def test_no_json(self) -> None:
        """When no braces are found, the input is returned as-is."""
        raw = "no json here"
        assert _extract_json_block(raw) == raw


# ---------------------------------------------------------------------------
# _truncate_observation
# ---------------------------------------------------------------------------


class TestTruncateObservation:
    """Tests for _truncate_observation."""

    def test_short_text_unchanged(self) -> None:
        """Text shorter than max_chars is returned unchanged."""
        text = "short"
        assert _truncate_observation(text, max_chars=100) == text

    def test_exact_boundary_unchanged(self) -> None:
        """Text of exactly max_chars is returned unchanged."""
        text = "x" * 50
        assert _truncate_observation(text, max_chars=50) == text

    def test_long_text_truncated(self) -> None:
        """Text longer than max_chars is truncated with an explanatory note."""
        text = "a" * 200
        result = _truncate_observation(text, max_chars=100)
        assert result.startswith("a" * 100)
        assert "truncated" in result
        assert "100 chars omitted" in result


# ---------------------------------------------------------------------------
# _observation_from_result
# ---------------------------------------------------------------------------


class TestObservationFromResult:
    """Tests for _observation_from_result."""

    def test_single_text_block(self) -> None:
        """A single text block is returned as plain text."""
        result = _make_mcp_result("hello world")
        assert _observation_from_result(result) == "hello world"

    def test_multiple_text_blocks_joined(self) -> None:
        """Multiple text blocks are joined by newlines."""
        block1 = MagicMock()
        block1.type = "text"
        block1.text = "part one"
        block2 = MagicMock()
        block2.type = "text"
        block2.text = "part two"
        result = MagicMock()
        result.content = [block1, block2]
        assert _observation_from_result(result) == "part one\npart two"

    def test_non_text_block_skipped(self) -> None:
        """Non-text content blocks are skipped; only text blocks are returned."""
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "kept"
        image_block = MagicMock()
        image_block.type = "image"
        result = MagicMock()
        result.content = [image_block, text_block]
        assert _observation_from_result(result) == "kept"

    def test_broken_result_returns_str(self) -> None:
        """A result that raises on attribute access falls back to str()."""
        broken = MagicMock()
        broken.content = None  # iteration over None raises TypeError
        out = _observation_from_result(broken)
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# _ToolSelection
# ---------------------------------------------------------------------------


class TestToolSelection:
    """Tests for _ToolSelection Pydantic model."""

    def test_valid_tool_call(self) -> None:
        """A well-formed JSON object produces a valid _ToolSelection."""
        data = {"tool_name": "atlas.cric_query", "tool_args": {"queue": "BNL"}, "thought": "x"}
        ts = _ToolSelection.model_validate(data)
        assert ts.tool_name == "atlas.cric_query"
        assert ts.tool_args == {"queue": "BNL"}
        assert not ts.should_synthesise

    def test_synthesise_flag(self) -> None:
        """should_synthesise=True with empty tool_name is valid."""
        data = {"tool_name": "", "tool_args": {}, "should_synthesise": True, "thought": "done"}
        ts = _ToolSelection.model_validate(data)
        assert ts.should_synthesise

    def test_defaults(self) -> None:
        """Missing optional fields receive their documented defaults."""
        ts = _ToolSelection.model_validate({"tool_name": "foo"})
        assert ts.tool_args == {}
        assert ts.thought == ""
        assert not ts.should_synthesise


# ---------------------------------------------------------------------------
# _EvalResult
# ---------------------------------------------------------------------------


class TestEvalResult:
    """Tests for _EvalResult Pydantic model."""

    def test_sufficient_true(self) -> None:
        """sufficient=True with high confidence parses correctly."""
        er = _EvalResult.model_validate({"sufficient": True, "confidence": 0.95})
        assert er.sufficient
        assert er.confidence == pytest.approx(0.95)
        assert er.missing is None

    def test_sufficient_false_with_missing(self) -> None:
        """sufficient=False with a missing description parses correctly."""
        er = _EvalResult.model_validate(
            {"sufficient": False, "confidence": 0.4, "missing": "need site data"}
        )
        assert not er.sufficient
        assert er.missing == "need site data"

    def test_confidence_clamped_above(self) -> None:
        """Confidence above 1.0 is clamped to exactly 1.0."""
        er = _EvalResult.model_validate({"sufficient": True, "confidence": 1.5})
        assert er.confidence == pytest.approx(1.0)

    def test_confidence_clamped_below(self) -> None:
        """Confidence below 0.0 is clamped to exactly 0.0."""
        er = _EvalResult.model_validate({"sufficient": False, "confidence": -0.1})
        assert er.confidence == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# AgentMemory
# ---------------------------------------------------------------------------


class TestAgentMemory:
    """Tests for AgentMemory."""

    def test_empty_history(self) -> None:
        """A fresh memory with no steps produces empty outputs."""
        mem = AgentMemory("what is 2+2?")
        assert mem.to_history() == []
        assert mem.observation_text() == ""
        assert mem.tool_names_used() == []

    def test_add_step_with_tool_produces_two_messages(self) -> None:
        """A step with a tool call produces an assistant + user message pair."""
        mem = AgentMemory("question")
        mem.add_step(AgentStep(
            step_index=1,
            thought="I need data",
            tool_name="cric_query",
            tool_args={"queue": "BNL"},
            observation='{"status": "online"}',
        ))
        history = mem.to_history()
        assert len(history) == 2
        assert history[0]["role"] == "assistant"
        assert "cric_query" in history[0]["content"]
        assert history[1]["role"] == "user"
        assert "online" in history[1]["content"]

    def test_add_step_without_observation_produces_one_message(self) -> None:
        """A step without an observation produces only an assistant message."""
        mem = AgentMemory("q")
        mem.add_step(AgentStep(
            step_index=1,
            thought="synthesising now",
            tool_name=None,
            tool_args={},
            observation=None,
        ))
        history = mem.to_history()
        assert len(history) == 1
        assert history[0]["role"] == "assistant"

    def test_tool_names_deduplicated_in_order(self) -> None:
        """tool_names_used() returns deduplicated names in first-call order."""
        mem = AgentMemory("q")
        for i in range(1, 4):
            mem.add_step(AgentStep(
                step_index=i,
                thought="",
                tool_name="cric_query" if i < 3 else "atlas.jobs_query",
                observation="obs",
            ))
        assert mem.tool_names_used() == ["cric_query", "atlas.jobs_query"]

    def test_observation_text_contains_labels(self) -> None:
        """observation_text() labels each block with step index and tool name."""
        mem = AgentMemory("q")
        mem.add_step(AgentStep(1, "t", "tool_a", {}, "obs_a"))
        text = mem.observation_text()
        assert "Step 1" in text
        assert "tool_a" in text
        assert "obs_a" in text

    def test_truncated_flag_default_false(self) -> None:
        """truncated is False on a freshly created AgentMemory."""
        mem = AgentMemory("q")
        assert not mem.truncated

    def test_truncated_flag_settable(self) -> None:
        """truncated can be set to True by the agent loop."""
        mem = AgentMemory("q")
        mem.truncated = True
        assert mem.truncated


# ---------------------------------------------------------------------------
# BambooAgent — full run loop
# ---------------------------------------------------------------------------


class TestBambooAgent:
    """Tests for BambooAgent with a mocked MCPAsyncClient."""

    @pytest.mark.asyncio
    async def test_single_step_success(self) -> None:
        """Agent completes in one step when the evaluator is immediately satisfied."""
        reasoning_resp = json.dumps({
            "tool_name": "cric_query",
            "tool_args": {"queue": "BNL"},
            "thought": "check queue status",
            "should_synthesise": False,
        })
        eval_resp = json.dumps({"sufficient": True, "confidence": 0.95})
        synthesis_resp = "BNL is online."

        client = _make_client(
            tools=["cric_query", "bamboo_llm_answer"],
            llm_responses=[reasoning_resp, eval_resp, synthesis_resp],
            tool_responses={"cric_query": '{"status": "online"}'},
        )

        agent = BambooAgent(client, max_steps=6, confidence_threshold=0.80, verbose=False)
        result = await agent.run("Is BNL online?")

        assert isinstance(result, AgentResult)
        assert "BNL" in result.answer
        assert len(result.steps) == 1
        assert result.steps[0].tool_name == "cric_query"
        assert not result.truncated
        assert result.confidence == pytest.approx(0.95)
        assert "cric_query" in result.tool_names_used

    @pytest.mark.asyncio
    async def test_two_step_completion(self) -> None:
        """Agent takes two steps when the evaluator is unsatisfied on step 1."""
        reasoning1 = json.dumps({
            "tool_name": "cric_query",
            "tool_args": {},
            "thought": "first tool",
            "should_synthesise": False,
        })
        eval1 = json.dumps({"sufficient": False, "confidence": 0.3, "missing": "need job data"})
        reasoning2 = json.dumps({
            "tool_name": "atlas.jobs_query",
            "tool_args": {},
            "thought": "second tool",
            "should_synthesise": False,
        })
        eval2 = json.dumps({"sufficient": True, "confidence": 0.90})
        synthesis = "Two-step answer."

        client = _make_client(
            tools=["cric_query", "atlas.jobs_query", "bamboo_llm_answer"],
            llm_responses=[reasoning1, eval1, reasoning2, eval2, synthesis],
        )

        agent = BambooAgent(client, max_steps=6, confidence_threshold=0.80, verbose=False)
        result = await agent.run("How many jobs at BNL?")

        assert len(result.steps) == 2
        assert result.steps[0].tool_name == "cric_query"
        assert result.steps[1].tool_name == "atlas.jobs_query"
        assert not result.truncated
        assert result.confidence == pytest.approx(0.90)

    @pytest.mark.asyncio
    async def test_early_synthesise_flag(self) -> None:
        """When the reasoning LLM returns should_synthesise=True, no tool is called."""
        reasoning_resp = json.dumps({
            "tool_name": "",
            "tool_args": {},
            "thought": "already know enough",
            "should_synthesise": True,
        })
        synthesis_resp = "Direct answer."

        client = _make_client(
            tools=["cric_query", "bamboo_llm_answer"],
            llm_responses=[reasoning_resp, synthesis_resp],
        )

        agent = BambooAgent(client, max_steps=6, verbose=False)
        result = await agent.run("What is 2+2?")

        assert result.answer == "Direct answer."
        assert len(result.steps) == 1
        assert result.steps[0].tool_name is None
        assert not result.truncated

    @pytest.mark.asyncio
    async def test_max_steps_truncation(self) -> None:
        """Agent sets truncated=True and synthesises when max_steps is exhausted."""
        always_reason = json.dumps({
            "tool_name": "cric_query",
            "tool_args": {},
            "thought": "still going",
            "should_synthesise": False,
        })
        always_eval = json.dumps({"sufficient": False, "confidence": 0.2})
        # 3 steps × (1 reason + 1 eval) = 6 LLM calls + 1 synthesis = 7.
        llm_responses = [always_reason, always_eval] * 3 + ["Truncated answer."]

        client = _make_client(
            tools=["cric_query", "bamboo_llm_answer"],
            llm_responses=llm_responses,
        )

        agent = BambooAgent(client, max_steps=3, verbose=False)
        result = await agent.run("Unsatisfiable question.")

        assert result.truncated
        assert len(result.steps) == 3
        assert "Truncated" in result.answer

    @pytest.mark.asyncio
    async def test_tool_call_failure_records_error_and_continues(self) -> None:
        """A failing tool call records the error string in observation and continues."""
        responses = [
            json.dumps({
                "tool_name": "broken_tool",
                "tool_args": {},
                "thought": "try it",
                "should_synthesise": False,
            }),
            json.dumps({"sufficient": True, "confidence": 0.9}),
            "Answer despite error.",
        ]

        base_client = _make_client(
            tools=["broken_tool", "bamboo_llm_answer"],
            llm_responses=responses,
        )
        original_call = base_client.call_tool

        async def _patched_call(name: str, arguments: dict[str, Any]) -> Any:
            if name != "bamboo_llm_answer":
                raise ConnectionError("network failure")
            return await original_call(name, arguments)

        base_client.call_tool = _patched_call

        agent = BambooAgent(base_client, max_steps=6, verbose=False)
        result = await agent.run("Call a broken tool.")

        assert len(result.steps) == 1
        assert result.steps[0].observation is not None
        assert "Tool call failed" in result.steps[0].observation
        assert "ConnectionError" in result.steps[0].observation

    @pytest.mark.asyncio
    async def test_reasoning_parse_error_forces_synthesis(self) -> None:
        """Invalid JSON from the reasoning LLM triggers immediate synthesis."""
        client = _make_client(
            tools=["cric_query", "bamboo_llm_answer"],
            llm_responses=["not valid json at all", "Fallback answer."],
        )

        agent = BambooAgent(client, max_steps=6, verbose=False)
        result = await agent.run("Parse error test.")

        assert result.answer == "Fallback answer."
        assert len(result.steps) == 1
        assert result.steps[0].tool_name is None

    @pytest.mark.asyncio
    async def test_eval_parse_error_continues_loop(self) -> None:
        """Invalid JSON from the evaluation LLM causes a safe fallback and loop continues."""
        responses = [
            json.dumps({
                "tool_name": "cric_query",
                "tool_args": {},
                "thought": "check",
                "should_synthesise": False,
            }),
            "this is not json",  # eval parse error → sufficient=False, confidence=0.5
            json.dumps({
                "tool_name": "",
                "tool_args": {},
                "thought": "now synthesise",
                "should_synthesise": True,
            }),
            "Final answer.",
        ]
        client = _make_client(
            tools=["cric_query", "bamboo_llm_answer"],
            llm_responses=responses,
        )

        agent = BambooAgent(client, max_steps=6, confidence_threshold=0.80, verbose=False)
        result = await agent.run("Eval parse error test.")

        assert isinstance(result.answer, str)
        assert len(result.answer) > 0

    @pytest.mark.asyncio
    async def test_result_fields_have_correct_types(self) -> None:
        """AgentResult exposes all public fields with their declared types."""
        reasoning_resp = json.dumps({
            "tool_name": "cric_query",
            "tool_args": {},
            "thought": "t",
            "should_synthesise": False,
        })
        eval_resp = json.dumps({"sufficient": True, "confidence": 0.88})

        client = _make_client(
            llm_responses=[reasoning_resp, eval_resp, "The answer."],
        )

        agent = BambooAgent(client, max_steps=4, verbose=False)
        result = await agent.run("Test fields.")

        assert isinstance(result.answer, str)
        assert isinstance(result.steps, list)
        assert isinstance(result.confidence, float)
        assert isinstance(result.truncated, bool)
        assert isinstance(result.tool_names_used, list)

    @pytest.mark.asyncio
    async def test_no_tools_discovered(self) -> None:
        """Agent runs without crashing even when no tools are discovered."""
        client = _make_client(
            tools=[],
            llm_responses=[
                json.dumps({
                    "tool_name": "",
                    "tool_args": {},
                    "thought": "no tools available",
                    "should_synthesise": True,
                }),
                "No tools available answer.",
            ],
        )

        agent = BambooAgent(client, max_steps=3, verbose=False)
        result = await agent.run("Anything?")

        assert isinstance(result.answer, str)
        assert len(result.answer) > 0

    @pytest.mark.asyncio
    async def test_confidence_below_threshold_continues_loop(self) -> None:
        """Evaluator result with confidence below threshold does not stop the loop early."""
        responses = [
            json.dumps({
                "tool_name": "cric_query",
                "tool_args": {},
                "thought": "first",
                "should_synthesise": False,
            }),
            json.dumps({"sufficient": True, "confidence": 0.5}),  # below threshold 0.80
            json.dumps({
                "tool_name": "",
                "tool_args": {},
                "thought": "synthesise",
                "should_synthesise": True,
            }),
            "Answer after two steps.",
        ]
        client = _make_client(
            tools=["cric_query", "bamboo_llm_answer"],
            llm_responses=responses,
        )

        agent = BambooAgent(client, max_steps=6, confidence_threshold=0.80, verbose=False)
        result = await agent.run("Low confidence test.")

        assert len(result.steps) == 2
        assert not result.truncated
