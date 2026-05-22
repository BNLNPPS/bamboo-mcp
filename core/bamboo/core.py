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
#
# Authors
# - Paul Nilsson, paul.nilsson@cern.ch, 2026

"""Core MCP server wiring.

This module creates the MCP Server instance, registers tools/prompts, and
initializes process-wide resources (LLM selection + client caching).
"""
from __future__ import annotations

import inspect
import asyncio
import os
from typing import Any, cast

from mcp.server import Server
from mcp.types import ListToolsResult, Tool

from bamboo.config import Config

from bamboo.auth import TokenAuth

# Phase 0: multi-LLM wiring
from bamboo.llm.config_loader import build_model_registry_from_config
from bamboo.llm.manager import LLMClientManager
from bamboo.llm.selector import LLMSelector
from bamboo.llm.runtime import set_llm_manager, set_llm_selector

from bamboo.tools.health import bamboo_health_tool
from bamboo.tools.doc_rag import panda_doc_search_tool
from bamboo.tools.doc_bm25 import panda_doc_bm25_tool
from bamboo.tools.queue_info import panda_queue_info_tool
from bamboo.tools.task_status import panda_task_status_tool
from bamboo.tools.job_status import panda_job_status_tool  # type: ignore[import-untyped]
from bamboo.tools.log_analysis import panda_log_analysis_tool  # type: ignore[import-untyped]
try:
    from askpanda_atlas.jobs_query import panda_jobs_query_tool  # type: ignore[import]
    _JOBS_QUERY_AVAILABLE = True
except ImportError:
    _JOBS_QUERY_AVAILABLE = False

try:
    from askpanda_atlas.harvester_worker import (  # type: ignore[import]
        panda_harvester_workers_tool,
    )
    _HARVESTER_WORKERS_AVAILABLE = True
except ImportError:
    _HARVESTER_WORKERS_AVAILABLE = False

try:
    from askpanda_atlas.panda_server_health import (  # type: ignore[import]
        panda_server_health_tool,
    )
    _PANDA_SERVER_HEALTH_AVAILABLE = True
except ImportError:
    _PANDA_SERVER_HEALTH_AVAILABLE = False

try:
    from askpanda_atlas.cric_query import cric_query_tool  # type: ignore[import]
    _CRIC_QUERY_AVAILABLE = True
except ImportError:
    _CRIC_QUERY_AVAILABLE = False
from bamboo.tools.llm_passthrough import bamboo_llm_answer_tool
from bamboo.tools.bamboo_answer import bamboo_answer_tool
from bamboo.tools.planner import bamboo_plan_tool
from bamboo.tools.bamboo_executor import bamboo_last_evidence_tool, bamboo_promptlog_status_tool
from bamboo.tools.code_query import code_query_tool
from bamboo.tools.opensearch_query import opensearch_query_tool
from bamboo.tools.opensearch_promptlog_query import opensearch_promptlog_query_tool
from bamboo.tools.loader import list_tool_entry_points, find_tool_by_name
from bamboo.tracing import EVENT_TOOL_CALL, span
from bamboo.prompts.templates import (
    get_bamboo_system_prompt,
    get_failure_triage_prompt,
)

TOOLS = {
    "bamboo_health": bamboo_health_tool,
    "bamboo_llm_answer": bamboo_llm_answer_tool,
    "bamboo_answer": bamboo_answer_tool,
    "bamboo_plan": bamboo_plan_tool,
    "bamboo_last_evidence": bamboo_last_evidence_tool,
    "bamboo_promptlog_status": bamboo_promptlog_status_tool,
    "opensearch_query": opensearch_query_tool,
    "opensearch_promptlog_query": opensearch_promptlog_query_tool,
    "panda_doc_search": panda_doc_search_tool,
    "panda_doc_bm25": panda_doc_bm25_tool,
    "panda_queue_info": panda_queue_info_tool,
    "panda_task_status": panda_task_status_tool,
    "panda_job_status": panda_job_status_tool,
    "panda_log_analysis": panda_log_analysis_tool,
    "code_query": code_query_tool,
}
if _JOBS_QUERY_AVAILABLE:
    TOOLS["panda_jobs_query"] = panda_jobs_query_tool
if _HARVESTER_WORKERS_AVAILABLE:
    TOOLS["panda_harvester_workers"] = panda_harvester_workers_tool
if _PANDA_SERVER_HEALTH_AVAILABLE:
    TOOLS["panda_server_health"] = panda_server_health_tool
if _CRIC_QUERY_AVAILABLE:
    TOOLS["cric_query"] = cric_query_tool


def _load_entrypoint_tool_definitions() -> list[dict[str, Any]]:
    """Load tool definitions from installed plugin entry points.

    Bamboo supports a plugin architecture where tools can be provided via
    Python entry points (group: ``bamboo.tools`` and legacy ``askpanda.tools``).
    This helper discovers those tools and returns their MCP tool definitions.

    Returns:
        A list of tool definition dicts compatible with the MCP server.
    """
    # Build the set of MCP tool names already covered by the built-in TOOLS
    # dict so we can skip entry-point tools that would produce duplicates.
    # TOOLS keys are internal identifiers (e.g. "panda_task_status"); the MCP
    # name exposed to clients comes from get_definition()["name"] and may differ
    # (e.g. the entry-point "atlas.task_status" resolves to the same singleton).
    covered_mcp_names: set[str] = set()
    for tool in TOOLS.values():
        get_def_fn = getattr(tool, "get_definition", None)
        if callable(get_def_fn):
            try:
                defn = get_def_fn()
                if isinstance(defn, dict) and defn.get("name"):
                    covered_mcp_names.add(str(defn["name"]))
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    defs: list[dict[str, Any]] = []
    for ep in list_tool_entry_points():
        full_name = ep.get("name", "")
        # Skip if the entry-point key itself matches a built-in TOOLS key.
        if not full_name or full_name in TOOLS:
            continue

        # Entry point names are expected to be "<namespace>.<tool_name>".
        if "." not in full_name:
            continue
        namespace, tool_name = full_name.split(".", 1)

        resolved = find_tool_by_name(tool_name, namespace=namespace)
        if resolved is None:
            continue

        obj = resolved.obj
        get_def = getattr(obj, "get_definition", None)
        if not callable(get_def):
            continue
        try:
            raw = get_def()
            if not isinstance(raw, dict):
                continue
            d: dict[str, Any] = cast(dict[str, Any], raw)
        except Exception:  # pylint: disable=broad-exception-caught
            continue

        # Skip if this entry point resolves to the same MCP tool name as one
        # already registered in TOOLS (avoids listing e.g. atlas.task_status
        # twice when panda_task_status is already in the built-in dict).
        if d.get("name", "") in covered_mcp_names:
            continue

        # Ensure tool name is the fully-qualified entry point name.
        d["name"] = full_name
        defs.append(d)
    return defs


def _validate_arguments(
    tool_def: dict[str, Any], arguments: dict[str, Any]
) -> str | None:
    """Validate ``arguments`` against a tool's ``inputSchema``.

    Performs lightweight structural validation — sufficient to catch missing
    required fields and unknown extra keys — without pulling in a full
    JSON Schema library.  The MCP SDK does not validate arguments on ingress,
    so this is the only gate between client input and tool business logic.

    Args:
        tool_def: Tool definition dict as returned by ``get_definition()``.
        arguments: Argument mapping supplied by the client.

    Returns:
        A human-readable error string if validation fails, or ``None`` if the
        arguments are valid.
    """
    schema: dict[str, Any] = tool_def.get("inputSchema", {})
    props: dict[str, Any] = schema.get("properties", {})

    # Check anyOf (e.g. question OR messages required)
    any_of: list[dict[str, Any]] = schema.get("anyOf", [])
    if any_of:
        satisfied = any(
            all(arguments.get(k) for k in branch.get("required", []))
            for branch in any_of
        )
        if not satisfied:
            branches = " or ".join(
                str(b.get("required", [])) for b in any_of
            )
            return f"One of {branches} must be provided."

    # Check required fields
    for field in schema.get("required", []):
        if field not in arguments or arguments[field] is None:
            return f"Required argument missing: '{field}'."

    # Check additionalProperties: false
    if schema.get("additionalProperties") is False and props:
        extra = sorted(set(arguments) - set(props))
        if extra:
            return f"Unexpected argument(s): {extra}. Allowed: {sorted(props)}."

    return None


def create_server() -> Server:  # pylint: disable=too-complex  # noqa: C901
    """Create and configure the MCP Server instance.

    This function wires up multi-LLM selection (model registry, selector, and
    per-process LLM client manager), registers available tools and prompts on
    the Server "app" instance, and publishes shared runtime state so tool
    singletons can access it.
    """
    app: Server = Server(Config.SERVER_NAME)

    # ---- Phase 0: initialize multi-LLM selection + per-process client cache ----
    # Support both Config being a class of constants or a dataclass-like type.
    try:
        config_obj: Config | type[Config] = Config()  # type: ignore[call-arg]
    except TypeError:
        config_obj = Config  # type: ignore[assignment]

    model_registry: Any = build_model_registry_from_config(config_obj)
    llm_selector: LLMSelector = LLMSelector(
        registry=model_registry,
        default_profile=getattr(config_obj, "LLM_DEFAULT_PROFILE", "default"),
        fast_profile=getattr(config_obj, "LLM_FAST_PROFILE", "fast"),
        reasoning_profile=getattr(config_obj, "LLM_REASONING_PROFILE", "reasoning"),
    )
    llm_manager: LLMClientManager = LLMClientManager()

    # Attach for visibility (HTTP shutdown handler can close these).
    setattr(app, "model_registry", model_registry)
    setattr(app, "llm_selector", llm_selector)
    setattr(app, "llm_manager", llm_manager)

    # ---- Auth: token allowlist (used by HTTP transports; stdio ignores headers) ----
    # If no tokens are configured, auth is effectively disabled (dev-friendly).
    # Configure via:
    #   - BAMBOO_MCP_TOKENS_FILE=/path/to/tokens.txt
    #   - or BAMBOO_MCP_TOKENS="client:token,client2:token2"
    setattr(app, "auth", TokenAuth.from_env())

    # Also publish into runtime context so simple tool singletons can access it.
    set_llm_selector(llm_selector)
    set_llm_manager(llm_manager)

    @app.list_tools()
    async def list_tools() -> Any:
        """Return the set of registered tools for the active plugin.

        Core tools (those in the ``TOOLS`` dict) are always included.
        Plugin tools discovered via Python entry points are filtered to only
        those whose namespace matches the active plugin, as determined by the
        ``ASKPANDA_PLUGIN`` environment variable (default: ``atlas``).

        This keeps the tool list sent to the LLM minimal — an ATLAS user does
        not pay token cost for CGSim tool descriptions, and vice versa.

        Returns:
            Union[List[Tool], ListToolsResult, List[Dict[str, Any]]]: The tool
            list in the appropriate shape for the MCP server/client contract.
        """
        active_plugin: str = os.getenv("ASKPANDA_PLUGIN", "atlas").strip().lower()

        defs: list[dict[str, Any]] = [tool.get_definition() for tool in TOOLS.values()]

        # Include only plugin tools whose namespace matches the active plugin.
        for ep_def in _load_entrypoint_tool_definitions():
            tool_name: str = ep_def.get("name", "")
            namespace: str = tool_name.split(".", 1)[0] if "." in tool_name else ""
            if namespace == active_plugin:
                defs.append(ep_def)

        # If Tool is a real class/model, return Tool objects.
        if inspect.isclass(Tool):
            return [Tool(**d) for d in defs]

        # Otherwise, try wrapping in ListToolsResult (often a model even if Tool is TypedDict).
        if inspect.isclass(ListToolsResult):
            return ListToolsResult(tools=cast(list[Tool], defs))

        # Last resort: plain dicts
        return defs

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a registered tool by name.

        Args:
            name: Name of the tool to call.
            arguments: JSON-like mapping of arguments for the tool.

        Returns:
            Any: Result produced by the called tool.

        Raises:
            ValueError: If the requested tool name is unknown.
        """
        async with span(EVENT_TOOL_CALL, tool=name,
                        args_keys=sorted((arguments or {}).keys())):
            tool: Any | None = TOOLS.get(name)
            if tool is not None:
                get_def_fn = getattr(tool, "get_definition", None)
                if callable(get_def_fn):
                    raw_def = get_def_fn()
                    tool_def: dict[str, Any] = raw_def if isinstance(raw_def, dict) else {}
                    err = _validate_arguments(tool_def, arguments or {})
                    if err:
                        from bamboo.tools.base import text_content as _tc  # local import avoids cycle
                        return _tc(f"Invalid arguments for tool '{name}': {err}")
                return await tool.call(arguments or {})

            # Fallback: resolve tool from plugin entry points.
            # Tool names are expected to be either:
            #   - fully-qualified: "<namespace>.<tool_name>" (preferred)
            #   - unqualified: "tool_name" (will match any namespace that ends
            #     with that suffix)
            namespace: str | None = None
            tool_name: str = name
            if "." in name:
                namespace, tool_name = name.split(".", 1)

            resolved = find_tool_by_name(tool_name, namespace=namespace)
            if resolved is None:
                raise ValueError(f"Unknown tool: {name}")

            obj = resolved.obj
            call_fn = getattr(obj, "call", None)
            if not callable(call_fn):
                raise ValueError(f"Resolved tool has no callable 'call': {name}")

            if inspect.iscoroutinefunction(call_fn):
                return await call_fn(arguments or {})
            # Run sync tools in a thread.
            return await asyncio.to_thread(call_fn, arguments or {})

    @app.list_prompts()
    async def list_prompts() -> Any:
        """List available prompts and their metadata.

        Returns:
            List[Dict[str, Any]]: Each entry contains prompt `name`,
            `description` and optional `arguments` specification.
        """
        return [
            {"name": "bamboo_system", "description": "Core system prompt"},
            {
                "name": "failure_triage",
                "description": "Failure triage template",
                "arguments": [
                    {
                        "name": "log_text",
                        "description": "Log snippet",
                        "required": True,
                    }
                ],
            },
        ]

    @app.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None) -> Any:
        """Return the requested prompt payload.

        Args:
            name: Prompt name (e.g. 'bamboo_system' or 'failure_triage').
            arguments: Arguments mapping used to fill template values.

        Returns:
            Dict[str, Any]: Prompt payload (typically a `messages` list).

        Raises:
            ValueError: If the requested prompt name is unknown.
        """
        if name == "bamboo_system":
            return await get_bamboo_system_prompt()
        if name == "failure_triage":
            return await get_failure_triage_prompt((arguments or {}).get("log_text", ""))
        raise ValueError(f"Unknown prompt: {name}")

    return app
