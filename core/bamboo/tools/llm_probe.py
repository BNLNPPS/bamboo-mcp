"""LLM connectivity probe tool.

This tool makes a minimal single-token request to the configured LLM provider
so that authentication and connectivity problems can be surfaced immediately
at server startup, rather than only when the user submits their first question.

The result is a small JSON object::

    {"status": "ok", "detail": "provider=mistral model=mistral-large-latest"}

On any failure, ``status`` is set to one of the recognised error categories
and ``detail`` carries a human-readable explanation.
"""

from __future__ import annotations

import json
from typing import Any

from bamboo.tools.base import text_content


# Sentinel returned when the LLM runtime is not yet initialised (e.g. tests
# that import the module without bootstrapping the server).
_STATUS_NOT_CONFIGURED = "not_configured"
_STATUS_OK = "ok"
_STATUS_AUTH_ERROR = "auth_error"
_STATUS_CONFIG_ERROR = "config_error"
_STATUS_RATE_LIMIT = "rate_limit"
_STATUS_TIMEOUT = "timeout"
_STATUS_PROVIDER_ERROR = "provider_error"


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Map an exception raised by the LLM client to a status/detail pair.

    Args:
        exc: Exception raised during the probe ``generate`` call.

    Returns:
        Tuple of ``(status_string, detail_string)`` where ``status_string`` is
        one of the ``_STATUS_*`` constants defined in this module.
    """
    # Deferred import: keeps module-level import free of optional provider deps.
    from bamboo.llm.exceptions import (  # pylint: disable=import-outside-toplevel
        LLMConfigError,
        LLMRateLimitError,
        LLMTimeoutError,
        LLMProviderError,
    )

    raw = str(exc)
    raw_lower = raw.lower()

    if isinstance(exc, LLMConfigError):
        return _STATUS_CONFIG_ERROR, raw

    if isinstance(exc, LLMRateLimitError):
        return _STATUS_RATE_LIMIT, raw

    if isinstance(exc, LLMTimeoutError):
        return _STATUS_TIMEOUT, raw

    if isinstance(exc, LLMProviderError):
        if any(s in raw_lower for s in ("401", "403", "unauthorized", "forbidden", "invalid api key", "authentication")):
            return _STATUS_AUTH_ERROR, raw
        if any(s in raw_lower for s in ("429", "rate limit", "rate_limit", "too many requests")):
            return _STATUS_RATE_LIMIT, raw
        if any(s in raw_lower for s in ("timeout", "timed out", "deadline")):
            return _STATUS_TIMEOUT, raw
        return _STATUS_PROVIDER_ERROR, raw

    # Catch-all for unexpected exception types.
    if any(s in raw_lower for s in ("401", "403", "unauthorized", "forbidden", "invalid api key", "authentication")):
        return _STATUS_AUTH_ERROR, raw

    return _STATUS_PROVIDER_ERROR, raw


async def _run_probe() -> dict[str, str]:
    """Make a minimal one-token generate call to check LLM connectivity.

    Uses the ``default`` model profile via the process-wide runtime objects.
    Sends a one-word ``"ping"`` message with ``max_tokens=1`` to keep cost and
    latency negligible.

    Returns:
        Dict with ``"status"`` and ``"detail"`` keys.  ``status`` is ``"ok"``
        on success or one of the ``_STATUS_*`` error constants on failure.
    """
    try:
        from bamboo.llm.runtime import (  # pylint: disable=import-outside-toplevel
            get_llm_manager,
            get_llm_selector,
        )
        from bamboo.llm.types import GenerateParams, Message  # pylint: disable=import-outside-toplevel
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {"status": _STATUS_NOT_CONFIGURED, "detail": str(exc)}

    try:
        selector = get_llm_selector()
        manager = get_llm_manager()
    except RuntimeError as exc:
        # Runtime not yet initialised — server still starting up.
        return {"status": _STATUS_NOT_CONFIGURED, "detail": str(exc)}

    try:
        default_profile = getattr(selector, "default_profile", "default")
        registry = getattr(selector, "registry", None)
        if registry is None:
            return {
                "status": _STATUS_NOT_CONFIGURED,
                "detail": "LLM selector has no registry.",
            }
        model_spec = registry.get(default_profile)
        client = await manager.get_client(model_spec)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        status, detail = _classify_error(exc)
        return {"status": status, "detail": detail}

    ping_messages: list[Message] = [{"role": "user", "content": "ping"}]
    try:
        resp = await client.generate(
            messages=ping_messages,
            params=GenerateParams(temperature=0.0, max_tokens=1),
        )
        provider = model_spec.provider
        model = model_spec.model
        _ = resp  # response text not needed for a connectivity probe
        return {
            "status": _STATUS_OK,
            "detail": f"provider={provider} model={model}",
        }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        status, detail = _classify_error(exc)
        return {"status": status, "detail": detail}


class LLMProbeTool:
    """Probe the configured LLM provider with a minimal test request.

    Intended to be called once during interface startup so that authentication
    and connectivity problems are reported before the user submits a question.
    The tool never raises — it always returns a JSON status object.
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool discovery definition.

        Returns:
            Dict compatible with the MCP tool discovery protocol.
        """
        return {
            "name": "bamboo_llm_probe",
            "description": (
                "Send a minimal single-token request to the configured LLM provider "
                "to verify that the API key is valid and the provider is reachable. "
                "Returns a JSON object with 'status' (ok / auth_error / config_error / "
                "rate_limit / timeout / provider_error / not_configured) and a 'detail' "
                "string. Use this at startup to surface credential problems before the "
                "user submits a question."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }

    async def call(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute the LLM probe and return a JSON status payload.

        Args:
            arguments: Ignored; accepted for MCP protocol compatibility.

        Returns:
            One-element list containing a text content block with a JSON
            object: ``{"status": "...", "detail": "..."}``.
        """
        del arguments  # intentionally unused
        result = await _run_probe()
        return text_content(json.dumps(result))


bamboo_llm_probe_tool = LLMProbeTool()
