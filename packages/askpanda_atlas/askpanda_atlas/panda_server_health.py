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

    Strings that begin with ``"Error"`` or contain exception keywords
    (e.g. ``SSLError``, ``ConnectionError``) are treated as not-alive,
    since the PanDA server may return a plain-text error message when its
    own internal calls fail.

    Args:
        raw: Raw text returned by the upstream MCP ``system_is_alive`` tool.

    Returns:
        ``True`` if the server appears to be alive, ``False`` otherwise.
    """
    stripped = raw.strip()
    if not stripped:
        return False

    # Error strings returned by the PanDA MCP server when its own internal
    # calls fail (e.g. SSL issues reaching pandaserver.cern.ch).
    _error_signals = ("Error ", "Error:", "SSLError", "ConnectionError",
                      "Timeout", "Max retries exceeded", "Exception")
    if any(stripped.startswith(sig) or sig in stripped for sig in _error_signals):
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


def _diagnose_error(raw: str) -> str | None:
    """Return a human-readable diagnosis for a known PanDA MCP error string.

    The PanDA MCP server sometimes returns plain-text error messages when
    its own internal calls fail.  This function maps known error patterns
    to actionable explanations so the LLM can report them clearly without
    needing domain knowledge about CERN infrastructure.

    Args:
        raw: Raw error text returned by the upstream MCP tool.

    Returns:
        A human-readable explanation string, or ``None`` if the error is
        not recognised.
    """
    r = raw.lower()

    # SSL error on port 25443 — the PanDA MCP server cannot reach
    # pandaserver.cern.ch:25443 due to a certificate issue on its side.
    if ("ssl" in r or "pem lib" in r) and "25443" in raw:
        return (
            "The PanDA MCP server at aipanda120.cern.ch was reached "
            "successfully, but it encountered an SSL certificate error "
            "when calling the backend PanDA server at "
            "pandaserver.cern.ch:25443. This is a server-side issue — "
            "the PanDA MCP server's own TLS configuration or certificate "
            "is misconfigured. Contact the PanDA MCP server administrators."
        )

    # Generic SSL error not on port 25443 — could be Bamboo-side CA issue.
    if "ssl" in r or "certificate verify failed" in r or "pem lib" in r:
        return (
            "An SSL/TLS certificate error occurred. If this is a new "
            "Bamboo deployment, ensure the CERN Grid CA and CERN Root CA 2 "
            "are appended to the certifi bundle in the virtualenv, or set "
            "SSL_CERT_FILE to a CA bundle that includes the CERN CAs. "
            "See the Bamboo question cheatsheet for setup instructions."
        )

    # Connection refused or timeout — MCP server unreachable.
    if "connectionrefused" in r.replace(" ", "") or "max retries exceeded" in r:
        return (
            "The PanDA MCP server could not be reached. "
            "Check that PANDA_MCP_BASE_URL is correct and that the server "
            "at aipanda120.cern.ch:8443 is running. "
            "You must be on the CERN network or connected via VPN (e.g. eduVPN)."
        )

    # Timeout.
    if "timeout" in r or "timed out" in r:
        return (
            "The connection to the PanDA MCP server timed out. "
            "Check your network connectivity to aipanda120.cern.ch and "
            "that you are on the CERN network or connected via VPN."
        )

    # Authentication / token error.
    if "401" in raw or "unauthorized" in r or "token" in r and "invalid" in r:
        return (
            "Authentication failed. The OIDC token may be expired or invalid. "
            "Re-run `uvx --from panda-mcp-client get-panda-token` to obtain "
            "a fresh token, which will be saved to ~/.panda_id_token."
        )

    return None


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
            evidence dict with keys ``is_alive``, ``raw_response``,
            ``error_explanation`` (human-readable diagnosis when not alive),
            and optionally ``error``.
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
        error_explanation: str | None = None if is_alive else _diagnose_error(raw)

        evidence = {
            "is_alive": is_alive,
            "raw_response": raw[:500],
            "error": None,
            "error_explanation": error_explanation,
        }

        if is_alive:
            summary = "The PanDA server is alive and responding."
        else:
            base = (
                f"The PanDA MCP server was reached but reported an error: {raw[:200]}"
                if raw and len(raw) > 10
                else f"The PanDA server does not appear to be alive. Response: {raw[:200]}"
            )
            summary = (
                f"{base}\n\n{error_explanation}"
                if error_explanation
                else base
            )

        return text_content(json.dumps({"evidence": evidence, "text": summary}))


panda_server_health_tool = PandaServerHealthTool()

__all__ = [
    "PandaServerHealthTool",
    "panda_server_health_tool",
    "get_definition",
]
