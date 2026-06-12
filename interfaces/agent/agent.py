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

"""ReAct-style AI Agent for Bamboo MCP.

Architecture overview
---------------------
The agent implements a **Reason → Act → Observe → Evaluate** loop:

1. **Reason**: given the question and accumulated history, the LLM chooses
   the next tool to call and constructs its arguments.
2. **Act**: the chosen tool is called via :class:`MCPAsyncClient`.
3. **Observe**: the tool result is appended to :class:`AgentMemory`.
4. **Evaluate**: a fast LLM call decides whether the evidence gathered so
   far is sufficient, or another step is required.
5. **Synthesise**: once the evaluator signals sufficiency (or ``max_steps``
   is reached), a final synthesis call produces the natural-language answer.

This pipeline deliberately bypasses ``bamboo_answer`` / ``bamboo_executor``
— the agent *is* the orchestration layer for complex, multi-hop queries.

LLM profile mapping
-------------------
* Reasoning (tool selection / synthesis) → ``"reasoning"`` profile
* Evaluation (sufficiency check)         → ``"fast"`` profile

All LLM calls are routed through the ``bamboo_llm_answer`` MCP tool so that
the agent does not need to import or initialise the LLM stack directly.

Prompt logging
--------------
A dedicated OpenSearch index ``bamboomcp-agentlog`` is reserved for agent
runs (daily rollover: ``bamboomcp-agentlog-YYYY.MM.DD``).  The logging call
is **commented out** until the index template is provisioned; see the note
marked ``# AGENT_LOG`` in :meth:`BambooAgent._synthesise`.

Environment variables
---------------------
``BAMBOO_AGENT_MAX_STEPS``
    Maximum reasoning steps before forced synthesis (default: 6).
``BAMBOO_AGENT_CONFIDENCE``
    Minimum evaluator confidence to accept an answer in [0, 1] (default: 0.80).
``BAMBOO_AGENT_MAX_TOKENS``
    Maximum tokens for synthesis LLM call (default: 2048).

Usage::

    import asyncio
    from interfaces.shared.mcp_client import MCPAsyncClient, MCPServerConfig
    from interfaces.agent.agent import BambooAgent

    async def main() -> None:
        cfg = MCPServerConfig(transport="http", http_url="http://localhost:8000/mcp")
        async with MCPAsyncClient(cfg) as client:
            agent = BambooAgent(client)
            result = await agent.run(
                "Which ATLAS sites had the highest pilot failure rate last week?"
            )
            print(result.answer)

    asyncio.run(main())
"""
from __future__ import annotations

import json
import logging
import os
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, model_validator

from interfaces.shared.mcp_client import MCPAsyncClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent tuning constants — all overridable via environment variables
# ---------------------------------------------------------------------------

_DEFAULT_MAX_STEPS: int = int(os.getenv("BAMBOO_AGENT_MAX_STEPS", "6"))
_DEFAULT_CONFIDENCE: float = float(os.getenv("BAMBOO_AGENT_CONFIDENCE", "0.80"))
_DEFAULT_MAX_TOKENS: int = int(os.getenv("BAMBOO_AGENT_MAX_TOKENS", "2048"))

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AgentStep:
    """One reasoning step in the agent's trace.

    Attributes:
        step_index: 1-based position in the reasoning loop.
        thought: The LLM's rationale for the action taken (from the reasoning
            call).  Empty string when the step is a forced synthesis.
        tool_name: Name of the MCP tool that was called, or ``None`` when the
            step represents a final synthesis without a tool call.
        tool_args: Arguments forwarded to the tool, or empty dict.
        observation: Raw text observation returned by the tool, or ``None``
            when no tool was called.
        eval_sufficient: Whether the evaluator judged the evidence sufficient
            after this step.  ``None`` for the final synthesis step.
        eval_confidence: Evaluator confidence in [0, 1].  ``None`` when no
            evaluation was performed.
    """

    step_index: int
    thought: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    observation: str | None = None
    eval_sufficient: bool | None = None
    eval_confidence: float | None = None


@dataclass
class AgentResult:
    """The completed output of a :class:`BambooAgent` run.

    Attributes:
        answer: Synthesised natural-language answer.
        steps: Ordered list of reasoning steps that led to the answer.
        confidence: Final evaluator confidence score (0.0 when unavailable).
        truncated: ``True`` if the agent hit ``max_steps`` before the
            evaluator was satisfied.
        tool_names_used: Deduplicated list of MCP tool names that were
            successfully called, in first-call order.
    """

    answer: str
    steps: list[AgentStep]
    confidence: float
    truncated: bool
    tool_names_used: list[str]


# ---------------------------------------------------------------------------
# Pydantic models for structured LLM outputs
# ---------------------------------------------------------------------------


class _ToolSelection(BaseModel):
    """Structured output from the reasoning LLM.

    The reasoning LLM is asked to return a JSON object that matches this
    schema.  ``tool_name`` must be an exact MCP tool name visible in the
    server's ``list_tools`` response.

    Attributes:
        tool_name: Exact MCP tool name to call next.
        tool_args: Arguments to pass to the tool.
        thought: Brief rationale for choosing this tool.
        should_synthesise: Set to ``True`` when no further tool calls are
            needed and the agent should proceed directly to synthesis.
    """

    tool_name: str = Field(default="", description="Exact MCP tool name to call next.")
    tool_args: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments to pass to the tool.  Must match the tool's input schema.",
    )
    thought: str = Field(
        default="",
        max_length=1000,
        description="Brief rationale for choosing this tool (used in the trace).",
    )
    should_synthesise: bool = Field(
        default=False,
        description=(
            "Set to true when enough evidence has been gathered and no further "
            "tool calls are needed.  The agent will skip the tool call and proceed "
            "directly to synthesis."
        ),
    )


class _EvalResult(BaseModel):
    """Structured output from the evaluation LLM.

    The evaluation LLM is asked whether the evidence gathered so far is
    sufficient to produce a complete, accurate answer to the original question.

    Attributes:
        sufficient: ``True`` if the evidence is sufficient to answer.
        confidence: Confidence in the sufficiency judgement, clamped to [0, 1].
            The LLM occasionally returns values slightly outside this range;
            the pre-validator silently clamps them before the range check runs.
        missing: Short description of what is still missing, if anything.
        suggested_tool: A specific tool that might fill the gap, if known.
    """

    sufficient: bool = Field(..., description="True if the evidence is sufficient to answer.")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in the sufficiency judgement, clamped to [0, 1]."
    )
    missing: str | None = Field(
        default=None,
        description="Short description of what information is still missing, if any.",
    )
    suggested_tool: str | None = Field(
        default=None,
        description="A specific tool that might fill the gap, if known.",
    )

    @model_validator(mode="before")
    @classmethod
    def _clamp_confidence(cls, values: Any) -> Any:
        """Clamp confidence to [0, 1] before Pydantic's range check runs.

        The LLM occasionally drifts slightly outside [0, 1].  Running the clamp
        as a ``mode="before"`` validator ensures it fires before ``ge``/``le``
        constraints are applied, so valid-but-slightly-out-of-range values are
        accepted silently rather than raising a ``ValidationError``.

        Args:
            values: Raw input mapping from ``model_validate``.

        Returns:
            Input mapping with ``confidence`` clamped to [0, 1].
        """
        if isinstance(values, dict) and "confidence" in values:
            try:
                values["confidence"] = max(0.0, min(1.0, float(values["confidence"])))
            except (TypeError, ValueError):
                pass  # Let Pydantic raise its own ValidationError for bad types.
        return values


# ---------------------------------------------------------------------------
# Agent memory
# ---------------------------------------------------------------------------


class AgentMemory:
    """Accumulates the question, steps, and observations across the reasoning loop.

    The memory object is the single source of truth for what the agent knows
    at any point.  It serialises itself into the normalised
    ``list[dict[str, str]]`` message format expected by the LLM clients.

    Attributes:
        question: The original user question, unchanged throughout the run.
        steps: Ordered list of :class:`AgentStep` objects appended as the
            loop progresses.
        truncated: Set to ``True`` by :class:`BambooAgent` when the loop
            exhausts ``max_steps`` before the evaluator is satisfied.
    """

    def __init__(self, question: str) -> None:
        """Initialise memory with the original question.

        Args:
            question: The user's original question.
        """
        self.question: str = question
        self.steps: list[AgentStep] = []
        self.truncated: bool = False

    def add_step(self, step: AgentStep) -> None:
        """Append a completed step to the memory.

        Args:
            step: The completed step to record.
        """
        self.steps.append(step)

    def to_history(self) -> list[dict[str, str]]:
        """Serialise the accumulated trace as an LLM-friendly message list.

        Each completed step is rendered as an ``assistant`` message (thought +
        tool call intent) followed by a ``user`` message (observation).  This
        keeps the format provider-agnostic — no special tool-call message types.

        Returns:
            List of ``{"role": ..., "content": ...}`` dicts.
        """
        messages: list[dict[str, str]] = []
        for step in self.steps:
            # Assistant turn: what the agent decided to do.
            assistant_parts: list[str] = []
            if step.thought:
                assistant_parts.append(f"Thought: {step.thought}")
            if step.tool_name:
                args_str = json.dumps(step.tool_args, ensure_ascii=False)
                assistant_parts.append(
                    f"Action: call tool `{step.tool_name}` with args {args_str}"
                )
            if assistant_parts:
                messages.append({"role": "assistant", "content": "\n".join(assistant_parts)})

            # User turn: the observation returned by the tool.
            if step.observation is not None:
                obs_content = f"Observation (step {step.step_index}): {step.observation}"
                messages.append({"role": "user", "content": obs_content})
        return messages

    def tool_names_used(self) -> list[str]:
        """Return the ordered, deduplicated list of tool names called so far.

        Returns:
            List of tool name strings in first-call order.
        """
        seen: dict[str, None] = {}
        for step in self.steps:
            if step.tool_name:
                seen[step.tool_name] = None
        return list(seen)

    def observation_text(self) -> str:
        """Concatenate all observations into a single context string.

        Returns:
            Newline-delimited string of all step observations, labelled by
            step index and tool name.
        """
        parts: list[str] = []
        for step in self.steps:
            if step.observation is not None:
                header = f"[Step {step.step_index} — {step.tool_name or 'N/A'}]"
                parts.append(f"{header}\n{step.observation}")
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SYSTEM_REASONING: str = textwrap.dedent("""\
    You are BambooAgent, an AI assistant for the PanDA distributed computing
    system (ATLAS experiment and other HEP experiments at CERN).

    Your task is to answer the user's question by calling MCP tools one step
    at a time.  After each observation you will be called again to decide the
    next action.

    Rules:
    - Choose the single most relevant tool per step from the list provided.
    - Pass only arguments that are documented in the tool's input schema.
    - If you already have enough evidence to answer fully, set
      "should_synthesise": true and omit tool_name / tool_args.
    - Keep your "thought" to one or two sentences.

    Respond ONLY with a valid JSON object matching this schema — nothing else:
    {
      "tool_name": "<exact tool name>",
      "tool_args": { ... },
      "thought": "<brief rationale>",
      "should_synthesise": false
    }
    When ready to synthesise (no further tool calls needed):
    {
      "should_synthesise": true,
      "thought": "<why evidence is now sufficient>",
      "tool_name": "",
      "tool_args": {}
    }
""")

_SYSTEM_EVALUATION: str = textwrap.dedent("""\
    You are an evidence evaluator for a PanDA distributed computing AI assistant.

    Your job is to decide whether the tool observations collected so far are
    sufficient to answer the user's question completely and accurately.

    Rules:
    - "sufficient" is true only if the observations directly and fully answer
      what was asked.  Partial data or proxy metrics are NOT sufficient.
    - "confidence" reflects how sure you are about your judgement (0 = not at
      all sure, 1 = completely sure).
    - If not sufficient, describe in "missing" what information is still needed
      and, if possible, name a specific MCP tool in "suggested_tool".

    Respond ONLY with a valid JSON object matching this schema — nothing else:
    {
      "sufficient": true|false,
      "confidence": <float 0..1>,
      "missing": "<description or null>",
      "suggested_tool": "<tool name or null>"
    }
""")

_SYSTEM_SYNTHESIS: str = textwrap.dedent("""\
    You are BambooAgent, an AI assistant for the PanDA distributed computing
    system (ATLAS experiment and other HEP experiments at CERN).

    You have gathered evidence by calling MCP tools in multiple reasoning steps.
    Synthesise a clear, well-structured answer to the user's original question
    using ONLY the evidence provided.

    Rules:
    - Be concise but complete.
    - If the evidence is incomplete, state clearly what could not be determined
      and why.
    - Do not invent numbers, site names, or error codes not present in the
      evidence.
    - Use Markdown formatting where it aids clarity (tables, bullet lists).
    - Include a Mermaid diagram (fenced ```mermaid block) only when a diagram
      genuinely clarifies a process or data flow — omit it for status answers.
""")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_json_block(text: str) -> str:
    """Extract the first JSON object from a possibly annotated LLM response.

    The LLM is instructed to return only JSON, but may occasionally wrap the
    output in markdown fences.  This function strips the fences and returns
    the first ``{...}`` block found.

    Args:
        text: Raw LLM response text.

    Returns:
        The extracted JSON string.  May still be invalid JSON; callers must
        handle ``json.JSONDecodeError``.
    """
    # Strip common markdown fences.
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    # Take the outermost {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _truncate_observation(text: str, max_chars: int = 6000) -> str:
    """Truncate a tool observation to keep the context window manageable.

    Args:
        text: Raw observation string.
        max_chars: Maximum character budget.

    Returns:
        Possibly truncated string with a note appended when truncated.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[...truncated — {len(text) - max_chars} chars omitted]"


def _observation_from_result(result: Any) -> str:
    """Convert a raw MCP tool result to a human-readable observation string.

    MCP tool results carry a ``content`` list of content blocks.  Each block
    has a ``type`` and, for text blocks, a ``text`` field.

    Args:
        result: Raw result object returned by ``MCPAsyncClient.call_tool()``.

    Returns:
        Concatenated text of all text-type content blocks, or a JSON fallback.
    """
    try:
        parts: list[str] = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        if parts:
            return "\n".join(parts)
        # Fallback: serialise whatever we received.
        return json.dumps(result, default=str)
    except Exception:  # pylint: disable=broad-exception-caught
        return str(result)


# ---------------------------------------------------------------------------
# BambooAgent
# ---------------------------------------------------------------------------


class BambooAgent:
    """Multi-step ReAct agent that orchestrates MCP tool calls to answer a question.

    The agent drives a Reason → Act → Observe → Evaluate loop using two LLM
    profiles:

    * **Reasoning** calls (tool selection + synthesis) use the ``reasoning``
      LLM profile (high-quality, may be slower).
    * **Evaluation** calls (sufficiency check) use the ``fast`` LLM profile
      (low-latency, concise structured output).

    All LLM calls are routed through the ``bamboo_llm_answer`` MCP tool, so
    the agent does not need to import or initialise the LLM stack directly.
    This also means the same provider / model configuration used by the server
    is automatically respected.

    Args:
        mcp_client: A connected :class:`~interfaces.shared.mcp_client.MCPAsyncClient`.
        max_steps: Maximum reasoning iterations before forced synthesis.
            Defaults to :envvar:`BAMBOO_AGENT_MAX_STEPS` (6).
        confidence_threshold: Minimum evaluator confidence to accept an answer
            early, in [0, 1].  Defaults to :envvar:`BAMBOO_AGENT_CONFIDENCE`
            (0.80).
        max_tokens: Token budget for the final synthesis LLM call.  Defaults
            to :envvar:`BAMBOO_AGENT_MAX_TOKENS` (2048).
        verbose: When ``True``, step-by-step progress is logged at INFO level
            and printed to stdout (useful for the CLI ``--verbose`` flag).

    Example::

        agent = BambooAgent(client, max_steps=8, verbose=True)
        result = await agent.run("Which sites had most pilot failures this week?")
        print(result.answer)
    """

    def __init__(
        self,
        mcp_client: MCPAsyncClient,
        *,
        max_steps: int = _DEFAULT_MAX_STEPS,
        confidence_threshold: float = _DEFAULT_CONFIDENCE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        verbose: bool = False,
    ) -> None:
        """Initialise the agent.

        Args:
            mcp_client: Connected async MCP client.
            max_steps: Maximum reasoning loop iterations.
            confidence_threshold: Sufficiency confidence threshold in [0, 1].
            max_tokens: Max tokens for final synthesis LLM call.
            verbose: Log and print step-by-step progress.
        """
        self._client = mcp_client
        self._max_steps = max_steps
        self._confidence_threshold = confidence_threshold
        self._max_tokens = max_tokens
        self._verbose = verbose
        self._available_tools: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, question: str) -> AgentResult:
        """Execute the full ReAct loop for ``question`` and return the result.

        The loop proceeds as follows:

        1. Discover available MCP tools.
        2. For each step up to ``max_steps``:

           a. Ask the reasoning LLM which tool to call (or to synthesise).
           b. Call the chosen tool; record the observation.
           c. Ask the evaluation LLM whether the evidence is sufficient.
           d. If sufficient and confidence ≥ threshold, break.

        3. Synthesise the final answer from all accumulated observations.

        Args:
            question: The user's natural-language question.

        Returns:
            :class:`AgentResult` with the answer, full step trace, confidence,
            truncation flag, and deduplicated tool list.
        """
        self._log(f"Agent starting — question: {question!r}")

        await self._refresh_tool_list()

        memory = AgentMemory(question)
        last_eval_confidence: float = 0.0
        truncated: bool = False

        for step_idx in range(1, self._max_steps + 1):
            self._log(f"─── Step {step_idx}/{self._max_steps} ───")

            # ── Reason ────────────────────────────────────────────────────
            selection = await self._reason(memory)
            self._log(f"  Thought: {selection.thought}")

            # The reasoning LLM may signal early synthesis.
            if selection.should_synthesise:
                self._log("  LLM signalled early synthesis — skipping tool call.")
                memory.add_step(AgentStep(
                    step_index=step_idx,
                    thought=selection.thought,
                    tool_name=None,
                    tool_args={},
                    observation=None,
                    eval_sufficient=True,
                    eval_confidence=1.0,
                ))
                last_eval_confidence = 1.0
                break

            self._log(f"  Action: {selection.tool_name}({json.dumps(selection.tool_args)})")

            # ── Act ───────────────────────────────────────────────────────
            observation = await self._act(selection.tool_name, selection.tool_args)
            self._log(f"  Observation: {len(observation)} chars")

            # ── Evaluate ──────────────────────────────────────────────────
            eval_result = await self._evaluate(memory, question, observation, step_idx)
            self._log(
                f"  Eval: sufficient={eval_result.sufficient} "
                f"confidence={eval_result.confidence:.2f}"
                + (f"  missing={eval_result.missing!r}" if eval_result.missing else "")
            )

            memory.add_step(AgentStep(
                step_index=step_idx,
                thought=selection.thought,
                tool_name=selection.tool_name,
                tool_args=selection.tool_args,
                observation=observation,
                eval_sufficient=eval_result.sufficient,
                eval_confidence=eval_result.confidence,
            ))
            last_eval_confidence = eval_result.confidence

            if eval_result.sufficient and eval_result.confidence >= self._confidence_threshold:
                self._log("  Evaluator satisfied — proceeding to synthesis.")
                break

        else:
            # for-loop exhausted without a break → forced synthesis.
            truncated = True
            memory.truncated = True
            self._log(
                f"  Max steps ({self._max_steps}) reached — "
                "synthesising with available evidence."
            )

        # ── Synthesise ────────────────────────────────────────────────────
        answer = await self._synthesise(question, memory)

        result = AgentResult(
            answer=answer,
            steps=memory.steps,
            confidence=last_eval_confidence,
            truncated=truncated,
            tool_names_used=memory.tool_names_used(),
        )
        self._log(
            f"Agent complete — {len(memory.steps)} step(s), "
            f"confidence={result.confidence:.2f}, truncated={result.truncated}"
        )
        return result

    # ------------------------------------------------------------------
    # Private: reasoning, acting, evaluating, synthesising
    # ------------------------------------------------------------------

    async def _refresh_tool_list(self) -> None:
        """Populate ``_available_tools`` from the MCP server's tool listing.

        The tool list is injected into every reasoning prompt so the LLM can
        choose from real, server-side tool names and descriptions.
        """
        try:
            result = await self._client.list_tools()
            tools = getattr(result, "tools", result) or []
            self._available_tools = [
                {
                    "name": getattr(t, "name", str(t)),
                    "description": getattr(t, "description", "")[:300],
                }
                for t in tools
            ]
            self._log(f"Discovered {len(self._available_tools)} tools.")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Could not list tools from MCP server: %s", exc)
            self._available_tools = []

    async def _reason(self, memory: AgentMemory) -> _ToolSelection:
        """Ask the reasoning LLM which tool to call next.

        Builds a prompt that includes the original question, the available
        tools list, and the full step history, then parses the structured JSON
        response into a :class:`_ToolSelection`.

        Args:
            memory: Current agent memory (question + all prior steps).

        Returns:
            Parsed :class:`_ToolSelection`.  Falls back to a synthesise-now
            directive on parse failure to prevent infinite loops.
        """
        tools_summary = "\n".join(
            f"  - {t['name']}: {t['description']}"
            for t in self._available_tools
        ) or "  (no tools discovered)"

        user_prompt = (
            f"Question: {memory.question}\n\n"
            f"Available tools:\n{tools_summary}\n\n"
        )
        if memory.steps:
            user_prompt += f"Evidence gathered so far:\n{memory.observation_text()}\n\n"

        user_prompt += (
            "Decide the NEXT action.  "
            "Respond only with the JSON object described in your instructions."
        )

        raw = await self._llm_call(
            system=_SYSTEM_REASONING,
            user=user_prompt,
            history=memory.to_history(),
            profile="reasoning",
            max_tokens=512,
        )
        return self._parse_tool_selection(raw)

    async def _act(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        """Call the named MCP tool and return the observation as text.

        Args:
            tool_name: Exact tool name as returned by ``list_tools``.
            tool_args: Arguments to pass to the tool.

        Returns:
            Truncated plain-text observation string.  On tool failure, returns
            an error marker string so the loop can continue.
        """
        try:
            result = await self._client.call_tool(tool_name, tool_args)
            raw_obs = _observation_from_result(result)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Tool call %r failed: %s", tool_name, exc)
            raw_obs = f"[Tool call failed: {type(exc).__name__}: {exc}]"

        return _truncate_observation(raw_obs)

    async def _evaluate(
        self,
        memory: AgentMemory,
        question: str,
        latest_observation: str,
        step_idx: int,
    ) -> _EvalResult:
        """Ask the fast evaluation LLM whether the evidence is now sufficient.

        Args:
            memory: Current agent memory (does not yet include the latest step).
            question: Original user question.
            latest_observation: Observation returned by the most recent tool call.
            step_idx: Current step index (used as a label in the prompt).

        Returns:
            Parsed :class:`_EvalResult`.  Falls back to ``sufficient=False,
            confidence=0.5`` on parse failure so the loop continues safely.
        """
        user_prompt = f"Original question: {question}\n\n"
        prior_text = memory.observation_text()
        if prior_text:
            user_prompt += f"Prior evidence:\n{prior_text}\n\n"
        user_prompt += (
            f"Latest observation (step {step_idx}):\n{latest_observation}\n\n"
            "Is the combined evidence sufficient to give a complete, accurate answer?  "
            "Respond only with the JSON object described in your instructions."
        )

        raw = await self._llm_call(
            system=_SYSTEM_EVALUATION,
            user=user_prompt,
            history=[],
            profile="fast",
            max_tokens=256,
        )
        return self._parse_eval_result(raw)

    async def _synthesise(self, question: str, memory: AgentMemory) -> str:
        """Produce the final natural-language answer from accumulated evidence.

        Args:
            question: Original user question.
            memory: Completed agent memory containing all observations.

        Returns:
            Synthesised answer string.

        Note:
            Agent-run prompt logging to a dedicated OpenSearch index is
            **prepared but commented out** (marked ``# AGENT_LOG``) until the
            ``bamboomcp-agentlog-YYYY.MM.DD`` index template is provisioned.
        """
        observations = memory.observation_text()
        step_count = len(memory.steps)
        user_prompt = (
            f"Question: {question}\n\n"
            f"Evidence gathered across {step_count} reasoning step(s):\n"
            f"{observations}\n\n"
            "Synthesise a complete, accurate answer to the question above."
        )
        if memory.truncated:
            user_prompt += (
                "\n\nNote: the agent reached its maximum step limit before the "
                "evaluator was fully satisfied.  State clearly what could not be "
                "determined from the available evidence."
            )

        answer = await self._llm_call(
            system=_SYSTEM_SYNTHESIS,
            user=user_prompt,
            history=[],
            profile="reasoning",
            max_tokens=self._max_tokens,
        )

        # AGENT_LOG: fire-and-forget logging to bamboomcp-agentlog-YYYY.MM.DD.
        # Uncomment once the dedicated index template is provisioned in OpenSearch.
        #
        # try:
        #     import asyncio as _asyncio  # noqa: PLC0415
        #     from bamboo.llm.prompt_log import log_prompt  # noqa: PLC0415
        #     _asyncio.create_task(
        #         log_prompt(
        #             system_prompt=_SYSTEM_SYNTHESIS,
        #             user_prompt=user_prompt,
        #             response=answer,
        #             tools_used=memory.tool_names_used(),
        #             provider="",   # populate from llm_info helper when wiring
        #             model="",      # populate from llm_info helper when wiring
        #             max_tokens=self._max_tokens,
        #             raw_question=question,
        #         ),
        #         name="bamboo.agent_log",
        #     )
        # except Exception as _exc:
        #     logger.debug("agent_log scheduling failed: %s", _exc)

        return answer

    # ------------------------------------------------------------------
    # Private: LLM call dispatcher
    # ------------------------------------------------------------------

    async def _llm_call(
        self,
        *,
        system: str,
        user: str,
        history: list[dict[str, str]],
        profile: str,
        max_tokens: int = 1024,
    ) -> str:
        """Call an LLM via the ``bamboo_llm_answer`` MCP tool.

        Rather than importing the LLM stack directly, the agent routes all LLM
        calls through the ``bamboo_llm_answer`` MCP tool.  This ensures the
        same LLM configuration (provider, model, API key resolution) used by
        the server is respected, without duplicating the initialisation logic.

        Note:
            ``bamboo_llm_answer`` always uses the server's configured default
            LLM profile and does not accept a profile selector argument
            (``additionalProperties: False`` in its input schema would reject
            any extra key).  The ``profile`` parameter is retained in the
            signature for documentation and future use should the tool gain
            profile selection support, but is not forwarded in the call.

        Args:
            system: System prompt string.
            user: Current user message content.
            history: Prior turn messages as ``[{"role": ..., "content": ...}]``.
            profile: Intended LLM profile (``"reasoning"`` or ``"fast"``).
                Currently informational only — not forwarded to the tool.
            max_tokens: Token budget for this specific call.

        Returns:
            Raw LLM response text.

        Raises:
            RuntimeError: If the tool call fails or returns no text content.
        """
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user})

        result = await self._client.call_tool(
            "bamboo_llm_answer",
            {
                "messages": messages,
                "max_tokens": max_tokens,
            },
        )
        text = _observation_from_result(result)
        if not text:
            raise RuntimeError(
                f"bamboo_llm_answer returned empty response for profile={profile!r}"
            )
        return text

    # ------------------------------------------------------------------
    # Private: structured output parsers
    # ------------------------------------------------------------------

    def _parse_tool_selection(self, raw: str) -> _ToolSelection:
        """Parse the reasoning LLM's JSON response into a :class:`_ToolSelection`.

        Args:
            raw: Raw LLM response text.

        Returns:
            Parsed :class:`_ToolSelection`.  Returns a synthesise-now directive
            on parse failure to prevent infinite loops.
        """
        try:
            data = json.loads(_extract_json_block(raw))
            return _ToolSelection.model_validate(data)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Could not parse tool selection from LLM output: %s\n%.200s", exc, raw
            )
            return _ToolSelection(
                tool_name="",
                tool_args={},
                thought="(JSON parse error — forcing synthesis)",
                should_synthesise=True,
            )

    def _parse_eval_result(self, raw: str) -> _EvalResult:
        """Parse the evaluation LLM's JSON response into an :class:`_EvalResult`.

        Args:
            raw: Raw LLM response text.

        Returns:
            Parsed :class:`_EvalResult`.  Returns ``sufficient=False,
            confidence=0.5`` on parse failure so the loop continues safely.
        """
        try:
            data = json.loads(_extract_json_block(raw))
            return _EvalResult.model_validate(data)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Could not parse eval result from LLM output: %s\n%.200s", exc, raw
            )
            return _EvalResult(
                sufficient=False, confidence=0.5, missing="(JSON parse error)", suggested_tool=None
            )

    # ------------------------------------------------------------------
    # Private: logging / verbose output
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        """Emit a message via the logger and, in verbose mode, to stdout.

        Args:
            message: Message string.
        """
        logger.info(message)
        if self._verbose:
            print(message)
