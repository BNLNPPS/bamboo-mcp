"""Architectural tests: MCP narrow-waist and schema contract compliance.

Every tool registered in the Bamboo server must satisfy two contracts:

1. **Narrow-waist return type** — ``call()`` must return ``list[MCPContent]``,
   i.e. a non-empty list of ``{"type": ..., ...}`` dicts, so direct MCP
   clients receive a well-formed content list regardless of which tool they
   invoke.  Tools that communicate rich structured data (evidence dicts) do
   so by JSON-serialising the payload into the ``text`` field.

2. **Schema contract** — every ``get_definition()`` must declare a valid
   ``inputSchema`` with ``"type": "object"``.  All properties must carry
   ``"type"`` and ``"description"``.  Required fields must be declared, and
   ``"additionalProperties": False`` must be present (or ``anyOf`` used for
   tools with alternative required-field sets).

Tool singletons are imported directly to avoid pulling in ``bamboo.core``
(which imports ``mcp.server`` and requires the full MCP SDK at import time).
This matches the pattern used throughout the rest of the test suite.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bamboo.tools.base import MCPContent  # noqa: F401
from bamboo.tools.health import bamboo_health_tool
from bamboo.tools.llm_passthrough import bamboo_llm_answer_tool
from bamboo.tools.bamboo_answer import bamboo_answer_tool
from bamboo.tools.planner import bamboo_plan_tool
from bamboo.tools.doc_rag import panda_doc_search_tool
from bamboo.tools.doc_bm25 import panda_doc_bm25_tool
from bamboo.tools.queue_info import panda_queue_info_tool
from bamboo.tools.task_status import panda_task_status_tool
from bamboo.tools.job_status import panda_job_status_tool
from bamboo.tools.log_analysis import panda_log_analysis_tool
from askpanda_atlas.harvester_worker import panda_harvester_workers_tool  # type: ignore[import]
from askpanda_atlas.panda_server_health import panda_server_health_tool  # type: ignore[import]

# ---------------------------------------------------------------------------
# Tool registry — mirrors bamboo.core.TOOLS without importing core.py
# ---------------------------------------------------------------------------

TOOLS: dict[str, Any] = {
    "bamboo_health": bamboo_health_tool,
    "bamboo_llm_answer": bamboo_llm_answer_tool,
    "bamboo_answer": bamboo_answer_tool,
    "bamboo_plan": bamboo_plan_tool,
    "panda_doc_search": panda_doc_search_tool,
    "panda_doc_bm25": panda_doc_bm25_tool,
    "panda_queue_info": panda_queue_info_tool,
    "panda_task_status": panda_task_status_tool,
    "panda_job_status": panda_job_status_tool,
    "panda_log_analysis": panda_log_analysis_tool,
    "panda_harvester_workers": panda_harvester_workers_tool,
    "panda_server_health": panda_server_health_tool,
}

_TOOL_NAMES: list[str] = sorted(TOOLS.keys())

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_mcp_content_list(value: Any) -> bool:
    """Return True if ``value`` is a valid ``list[MCPContent]``.

    A valid MCP content list is a non-empty list of dicts each containing at
    least a ``"type"`` key.  When ``type == "text"`` the ``"text"`` key must
    also be present.

    Args:
        value: The value returned by a tool's ``call()`` method.

    Returns:
        True if the value conforms to the narrow-waist contract.
    """
    if not isinstance(value, list) or len(value) == 0:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if "type" not in item:
            return False
        if item["type"] == "text" and "text" not in item:
            return False
    return True


# ---------------------------------------------------------------------------
# Minimal stub arguments — one valid input per tool
# ---------------------------------------------------------------------------

_STUB_ARGS: dict[str, dict[str, Any]] = {
    "bamboo_health": {},
    "bamboo_llm_answer": {"question": "stub"},
    "bamboo_answer": {"question": "What is PanDA?"},
    "bamboo_plan": {"question": "stub"},
    "panda_doc_search": {"query": "stub"},
    "panda_doc_bm25": {"query": "stub"},
    "panda_queue_info": {"site": "BNL-ATLAS"},
    "panda_task_status": {"task_id": 1},
    "panda_job_status": {"job_id": 1},
    "panda_log_analysis": {"job_id": 1},
    "panda_harvester_workers": {"question": "How many pilots are running at BNL?"},
    "panda_server_health": {"query": "Is the PanDA server alive?"},
}

# ---------------------------------------------------------------------------
# Schema compliance tests — synchronous, no mocking needed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_tool_has_valid_schema(tool_name: str) -> None:
    """Every tool's inputSchema must declare type=object and well-typed properties.

    Args:
        tool_name: Name of the tool under test (injected by parametrize).
    """
    tool = TOOLS[tool_name]
    defn = tool.get_definition()

    assert isinstance(defn, dict), \
        f"{tool_name}: get_definition() must return a dict"
    assert defn.get("name"), \
        f"{tool_name}: definition must have a non-empty 'name'"
    assert defn.get("description"), \
        f"{tool_name}: definition must have a non-empty 'description'"

    schema = defn.get("inputSchema", {})
    assert isinstance(schema, dict), \
        f"{tool_name}: inputSchema must be a dict"
    assert schema.get("type") == "object", \
        f"{tool_name}: inputSchema must have type=object"

    props = schema.get("properties", {})
    for prop_name, prop_def in props.items():
        assert "type" in prop_def, \
            f"{tool_name}: property '{prop_name}' is missing 'type'"
        assert "description" in prop_def, \
            f"{tool_name}: property '{prop_name}' is missing 'description'"


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_tool_schema_closes_additionalproperties(tool_name: str) -> None:
    """inputSchema must declare additionalProperties=False (or use anyOf).

    Args:
        tool_name: Name of the tool under test (injected by parametrize).
    """
    tool = TOOLS[tool_name]
    schema = tool.get_definition().get("inputSchema", {})

    has_additional = "additionalProperties" in schema
    has_any_of = "anyOf" in schema
    assert has_additional or has_any_of, (
        f"{tool_name}: inputSchema must declare 'additionalProperties' "
        f"(or use 'anyOf' for tools with alternative required-field sets)"
    )
    if has_additional:
        assert schema["additionalProperties"] is False, (
            f"{tool_name}: 'additionalProperties' must be False, "
            f"got {schema['additionalProperties']!r}"
        )


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_tool_schema_declares_required_fields(tool_name: str) -> None:
    """Tools with properties must declare required fields or use anyOf.

    Args:
        tool_name: Name of the tool under test (injected by parametrize).
    """
    tool = TOOLS[tool_name]
    schema = tool.get_definition().get("inputSchema", {})
    props = schema.get("properties", {})

    if not props:
        return  # Zero-argument tools (bamboo_health) are exempt.

    has_required = bool(schema.get("required"))
    has_any_of = bool(schema.get("anyOf"))
    assert has_required or has_any_of, (
        f"{tool_name}: inputSchema has properties but no 'required' array "
        f"or 'anyOf' constraint"
    )

# ---------------------------------------------------------------------------
# Narrow-waist return type — async, mocks suppress all external I/O
# ---------------------------------------------------------------------------


def _build_llm_runtime_mocks() -> tuple[Any, Any]:
    """Build fake LLM selector and manager that satisfy llm_passthrough and planner.

    Both tools call ``get_llm_selector()`` / ``get_llm_manager()`` from the
    runtime module, then chain through ``selector.registry.get(profile)`` →
    ``await manager.get_client(spec)`` → ``await client.generate(...)``.

    Returns:
        Tuple of (fake_selector, fake_manager) ready for use as patch targets.
    """
    from bamboo.llm.types import LLMResponse, ModelSpec, TokenUsage

    fake_response = LLMResponse(text="stub llm text", usage=TokenUsage(0, 0, 0))
    fake_client = MagicMock()
    fake_client.generate = AsyncMock(return_value=fake_response)

    fake_manager = MagicMock()
    fake_manager.get_client = AsyncMock(return_value=fake_client)

    fake_spec = ModelSpec(provider="openai", model="stub-model")
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=fake_spec)

    fake_selector = MagicMock()
    fake_selector.default_profile = "default"
    fake_selector.registry = fake_registry

    return fake_selector, fake_manager


def _build_patches() -> list[tuple[str, Any]]:
    """Build patch list to suppress all external I/O during call() tests.

    Returns:
        List of (dotted-target-string, mock-object) pairs.
    """
    llm_content: list[MCPContent] = [{"type": "text", "text": "stub llm reply"}]
    llm_mock = AsyncMock(return_value=llm_content)

    rag_content: list[MCPContent] = [{"type": "text", "text": "stub rag"}]
    bm25_content: list[MCPContent] = [{"type": "text", "text": "stub bm25"}]
    rag_mock = AsyncMock(return_value=rag_content)
    bm25_mock = AsyncMock(return_value=bm25_content)

    evidence_json = json.dumps({
        "evidence": {"status": "done", "not_found": False},
        "text": "stub evidence",
    })
    evidence_content: list[MCPContent] = [{"type": "text", "text": evidence_json}]
    evidence_mock = AsyncMock(return_value=evidence_content)

    mcp_caller = MagicMock()
    mcp_caller.call = AsyncMock(return_value={"error": None, "text": "{}"})

    fetch_mock = MagicMock(
        return_value=(200, "application/json", "{}", {"status": "done"})
    )

    fake_selector, fake_manager = _build_llm_runtime_mocks()

    return [
        # bamboo_answer delegates to these tool singletons
        ("bamboo.tools.bamboo_answer.bamboo_llm_answer_tool", MagicMock(call=llm_mock)),
        ("bamboo.tools.bamboo_answer.panda_doc_search_tool", MagicMock(call=rag_mock)),
        ("bamboo.tools.bamboo_answer.panda_doc_bm25_tool", MagicMock(call=bm25_mock)),
        ("bamboo.tools.bamboo_answer.panda_task_status_tool", MagicMock(call=evidence_mock)),
        ("bamboo.tools.bamboo_answer.panda_job_status_tool", MagicMock(call=evidence_mock)),
        ("bamboo.tools.bamboo_answer.panda_log_analysis_tool", MagicMock(call=evidence_mock)),
        # llm_passthrough and planner use the LLM runtime singleton
        ("bamboo.llm.runtime.get_llm_selector", MagicMock(return_value=fake_selector)),
        ("bamboo.llm.runtime.get_llm_manager", MagicMock(return_value=fake_manager)),
        ("bamboo.tools.llm_passthrough.get_llm_selector", MagicMock(return_value=fake_selector)),
        ("bamboo.tools.llm_passthrough.get_llm_manager", MagicMock(return_value=fake_manager)),
        ("bamboo.tools.planner.get_llm_selector", MagicMock(return_value=fake_selector)),
        ("bamboo.tools.planner.get_llm_manager", MagicMock(return_value=fake_manager)),
        # job_status fetches from BigPanDA REST via cached_fetch_jsonish
        # (same helper used by harvester_workers; both are covered by the
        # askpanda_atlas._cache.cached_fetch_jsonish patch below)
        # log_analysis makes direct HTTP calls (metadata + log download)
        ("askpanda_atlas.log_analysis_impl._fetch_metadata",
         MagicMock(return_value={
             "job": {"jobstatus": "failed", "piloterrorcode": 0, "piloterrordiag": ""},
             "files": [], "dsfiles": [],
         })),
        ("askpanda_atlas.log_analysis_impl._fetch_log_text", MagicMock(return_value=None)),
        # task_status makes an HTTP fetch
        ("askpanda_atlas._fallback_http.fetch_jsonish", fetch_mock),
        # panda_harvester_workers makes an HTTP fetch via the cache
        ("askpanda_atlas._cache.cached_fetch_jsonish",
         MagicMock(return_value=(200, "application/json", "[]", {"_data": []}))),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
async def test_tool_call_returns_mcp_content_list(tool_name: str) -> None:
    """Every tool's call() must return a valid list[MCPContent].

    This is the core narrow-waist architectural assertion.  A tool returning
    a bare dict instead of list[MCPContent] produces a malformed MCP response
    for any client invoking it directly over the protocol.

    Args:
        tool_name: Name of the tool under test (injected by parametrize).
    """
    tool = TOOLS[tool_name]
    args = _STUB_ARGS.get(tool_name, {})

    active: list[Any] = []
    for target, mock_obj in _build_patches():
        try:
            p = patch(target, mock_obj)
            p.start()
            active.append(p)
        except AttributeError:
            pass

    try:
        result = await tool.call(args)
    finally:
        for p in active:
            try:
                p.stop()
            except RuntimeError:
                pass

    assert _is_mcp_content_list(result), (
        f"Tool '{tool_name}' returned {type(result).__name__} instead of "
        f"list[MCPContent].\n"
        f"  Value: {result!r:.300}\n\n"
        f"  Fix: wrap the return value with text_content(json.dumps(payload))."
    )


# ---------------------------------------------------------------------------
# Evidence tools: JSON round-trip verification
# ---------------------------------------------------------------------------

_EVIDENCE_TOOLS = ["panda_task_status", "panda_job_status", "panda_log_analysis", "panda_server_health"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", _EVIDENCE_TOOLS)
async def test_evidence_tool_text_is_json_with_evidence_key(tool_name: str) -> None:
    """Evidence tools must encode their payload as JSON in the text field.

    The ``text`` field must be valid JSON containing at least an ``"evidence"``
    key so ``bamboo_answer._unpack_tool_result()`` can deserialise it.

    Args:
        tool_name: Name of the evidence tool under test.
    """
    tool = TOOLS[tool_name]
    args = _STUB_ARGS[tool_name]

    mcp_caller = MagicMock()
    mcp_caller.call = AsyncMock(return_value={"error": None, "text": "{}"})
    fetch_mock = MagicMock(
        return_value=(200, "application/json", "{}", {"status": "done"})
    )

    fetch_200 = (200, "application/json", "{}", {"job": {}, "files": []})
    with (
        patch("askpanda_atlas._cache.cached_fetch_jsonish",
              return_value=fetch_200),
        patch("askpanda_atlas.log_analysis_impl._fetch_metadata",
              return_value={
                  "job": {"jobstatus": "failed", "piloterrorcode": 0, "piloterrordiag": ""},
                  "files": [], "dsfiles": [],
              }),
        patch("askpanda_atlas.log_analysis_impl._fetch_log_text", return_value=None),
        patch("askpanda_atlas._fallback_http.fetch_jsonish", fetch_mock),
    ):
        result = await tool.call(args)

    assert _is_mcp_content_list(result), \
        f"{tool_name}: call() must return list[MCPContent]"

    text = result[0].get("text", "")
    assert isinstance(text, str) and text.strip(), \
        f"{tool_name}: text field must be a non-empty string"

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{tool_name}: text field is not valid JSON.\n"
            f"  Error: {exc}\n"
            f"  Text: {text[:300]!r}"
        )

    assert "evidence" in parsed, (
        f"{tool_name}: JSON payload must contain an 'evidence' key. "
        f"Got keys: {list(parsed)}"
    )
