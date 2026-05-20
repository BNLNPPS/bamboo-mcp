"""PanDA server health tool.

Calls the ``system_is_alive`` tool on the external PanDA MCP server and returns
structured evidence suitable for LLM summarisation.  This is the first
Bamboo tool that delegates to the PanDA MCP server; it answers questions
such as:

- "Is the PanDA server alive?"
- "Is PanDA OK?"
- "Is the PanDA server running?"

The upstream ``system_is_alive`` tool takes no arguments and returns a short
status string from the PanDA server.

Session setup
-------------
The session must be registered with the process-wide ``MCPCaller`` before
this tool can reach the PanDA MCP server.  That wiring happens at Bamboo
server startup via ``panda_mcp_session.run_panda_mcp_session()``.  If no
session is registered the tool returns a graceful error dict — it never
raises.
"""
from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger(__name__)

_SERVER: str = "panda"
_TOOL: str = "system_is_alive"


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for panda_server_health.

    Returns:
        Dict with name, description, inputSchema, examples, and tags.
    """
    return {
        "name": "panda_server_health",
        "description": (
            "Check whether the PanDA server is alive and responding. "
            "Use for questions like 'Is the PanDA server OK?', "
            "'Is PanDA alive?', 'Is the PanDA server running?', or "
            "'What is the PanDA server status?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "examples": [
            {"query": "Is the PanDA server alive?"},
            {"query": "Is PanDA OK?"},
        ],
        "tags": ["atlas", "panda", "health", "status", "alive"],
    }


def _parse_alive(raw: str) -> bool:
    """Determine whether the server reports itself alive from raw response text.

    The ``system_is_alive`` tool typically returns a short string such as
    ``"True"`` or a JSON object ``{"alive": true}``.  This function
    handles both formats conservatively: only an explicit falsy value
    causes it to return ``False``; any non-empty response that cannot
    be parsed as JSON is treated as alive.

    Args:
        raw: Raw text returned by the upstream MCP ``system_is_alive`` tool.

    Returns:
        ``True`` if the server appears to be alive, ``False`` otherwise.
    """
    stripped = raw.strip()
    if not stripped:
        return False

    # Try JSON first.
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, bool):
            return parsed
        if isinstance(parsed, dict):
            alive_val = parsed.get("alive", parsed.get("status", True))
            if isinstance(alive_val, bool):
                return alive_val
            if isinstance(alive_val, str):
                return alive_val.lower() not in {"false", "0", "no", "down"}
        # Any non-empty JSON object/array is treated as alive.
        return True
    except (json.JSONDecodeError, ValueError):
        pass

    # Plain string fallback.
    return stripped.lower() not in {"false", "0", "no", "down", "dead"}


class PandaServerHealthTool:
    """MCP tool for checking PanDA server liveness via the PanDA MCP server."""

    def __init__(self) -> None:
        """Initialise with the tool definition."""
        self._def: dict[str, Any] = get_definition()

    def get_definition(self) -> dict[str, Any]:
        """Return the MCP tool definition.

        Returns:
            Tool definition dictionary.
        """
        return self._def

    async def call(self, arguments: dict[str, Any]) -> list[Any]:
        """Check PanDA server liveness and return structured evidence.

        Calls the ``system_is_alive`` tool on the ``"panda"`` MCP server registered
        with the process-wide ``MCPCaller``.  The result is a one-element
        ``list[MCPContent]`` whose ``text`` field contains a JSON-serialised
        evidence dict conforming to the Bamboo narrow-waist contract.

        Args:
            arguments: Dict optionally containing ``query`` (the original
                user question, used only for logging).

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence dict with keys ``is_alive``, ``raw_response``, and
            optionally ``error``.
        """
        # Deferred imports — bamboo-core must not be imported at module level.
        from bamboo.tools._mcp_caller import get_mcp_caller  # type: ignore[import-untyped]
        from bamboo.tools.base import text_content  # type: ignore[import-untyped]

        caller = get_mcp_caller()
        result = await caller.call(
            server_name=_SERVER,
            tool_name=_TOOL,
            arguments={},
        )

        if result["error"]:
            evidence: dict[str, Any] = {
                "is_alive": False,
                "error": result["error"],
                "raw_response": None,
            }
            return text_content(json.dumps({
                "evidence": evidence,
                "text": (
                    f"Could not reach the PanDA server: {result['error']}"
                ),
            }))

        raw: str = result["text"] or ""
        is_alive = _parse_alive(raw)
        evidence = {
            "is_alive": is_alive,
            "raw_response": raw[:500],
            "error": None,
        }

        if is_alive:
            summary = "The PanDA server is alive and responding."
        else:
            summary = f"The PanDA server does not appear to be alive. Response: {raw[:200]}"

        return text_content(json.dumps({"evidence": evidence, "text": summary}))


panda_server_health_tool = PandaServerHealthTool()

__all__ = [
    "PandaServerHealthTool",
    "panda_server_health_tool",
    "get_definition",
]
