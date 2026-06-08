"""Lightweight caller for upstream MCP tool servers.

Bamboo tools that delegate to external MCP servers (grid-monitoring,
panda, etc.) use this module to invoke those tools.

The caller spawns a one-shot ``bamboo.server`` subprocess and uses the
stdio MCP transport, exactly as the Streamlit and Textual interfaces do.
When the upstream server is not available the call returns a structured
error dict rather than raising, so tools can return graceful evidence.

Process-wide singleton
----------------------
``get_mcp_caller()`` returns a shared :class:`MCPCaller` instance that is
initialised lazily on first use and reused for the lifetime of the process.
Call ``set_mcp_caller(caller)`` during server startup (or in tests) to
inject a custom implementation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_mcp_caller: "MCPCaller | None" = None


def set_mcp_caller(caller: "MCPCaller") -> None:
    """Set the process-wide MCP caller (called during server startup or tests).

    Args:
        caller: MCPCaller instance to register.
    """
    global _mcp_caller  # pylint: disable=global-statement
    _mcp_caller = caller


def get_mcp_caller() -> "MCPCaller":
    """Return the process-wide MCP caller, creating a default one if needed.

    Returns:
        The registered MCPCaller instance.
    """
    global _mcp_caller  # pylint: disable=global-statement
    if _mcp_caller is None:
        _mcp_caller = MCPCaller()
    return _mcp_caller


class MCPCaller:
    """Calls tools on upstream MCP servers connected to the current process.

    This implementation uses ``mcp.client.session.ClientSession`` over a
    stdio transport to call tools on a spawned server subprocess.  For the
    common case where Bamboo is itself running as an MCP server (stdio or
    HTTP), the upstream servers are already connected via the host (Claude
    Desktop, Claude.ai, etc.) — in that scenario the caller delegates
    directly via the runtime MCP session if one is registered.

    When no upstream session is available the call fails gracefully and
    returns a structured error dict.
    """

    def __init__(self) -> None:
        """Initialise with an empty upstream session registry."""
        self._sessions: dict[str, Any] = {}

    def register_session(self, server_name: str, session: Any) -> None:
        """Register an upstream MCP session by server name.

        Args:
            server_name: Logical server name (e.g. ``"grid-monitoring"``).
            session: ``mcp.client.session.ClientSession`` instance.
        """
        self._sessions[server_name] = session

    async def call(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a tool on an upstream MCP server.

        Args:
            server_name: Logical server name (e.g. ``"grid-monitoring"``).
            tool_name: Tool name on that server (e.g. ``"panda_job_get_status"``).
            arguments: Tool arguments.

        Returns:
            Dict with ``text`` (raw response text) and ``error`` (None on
            success, error string on failure).
        """
        session = self._sessions.get(server_name)
        if session is None:
            return {
                "text": None,
                "error": (
                    f"Upstream MCP server '{server_name}' is not connected. "
                    "Ensure the server is running and registered with Bamboo."
                ),
            }
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=60.0,
            )
            # MCP tool results have a .content list of content blocks
            text_parts: list[str] = []
            content = getattr(result, "content", None) or []
            for block in content:
                t = getattr(block, "text", None)
                if t:
                    text_parts.append(str(t))
            text = "\n".join(text_parts) if text_parts else str(result)
            return {"text": text, "error": None}
        except asyncio.TimeoutError:
            return {"text": None, "error": f"Timeout calling {server_name}:{tool_name}"}
        except Exception as e:  # pylint: disable=broad-exception-caught
            return {"text": None, "error": f"{server_name}:{tool_name} raised: {e!r}"}


__all__ = ["MCPCaller", "get_mcp_caller", "set_mcp_caller"]
