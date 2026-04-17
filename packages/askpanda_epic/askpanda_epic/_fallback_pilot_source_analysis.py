"""Fallback PilotSourceAnalysisTool for standalone ePIC (no askpanda_atlas) use.

Used only when ``askpanda_atlas`` is not installed alongside ``askpanda_epic``.
Returns a clear error message rather than crashing so the server remains
usable for all other ePIC tools.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


def get_definition() -> dict[str, Any]:
    """Return a minimal MCP tool definition for the fallback stub.

    Returns:
        Tool definition dictionary.
    """
    return {
        "name": "pilot_source_analysis",
        "description": (
            "Deep-dive into a pilot_monitoring_error by fetching relevant "
            "pilot3 source modules from GitHub. Requires askpanda_atlas to "
            "be installed alongside askpanda_epic for full functionality."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer"},
                "log_excerpt": {"type": "string"},
                "pilot_error_diag": {"type": "string"},
            },
            "required": ["job_id", "log_excerpt"],
            "additionalProperties": False,
        },
        "tags": ["epic", "eic", "panda", "pilot", "pilot3", "source", "exception"],
    }


def parse_traceback_frames(log_excerpt: str) -> list[dict[str, str]]:  # noqa: ARG001
    """Stub — returns empty list when askpanda_atlas is not installed."""
    return []


def parse_exception_line(log_excerpt: str) -> str:  # noqa: ARG001
    """Stub — returns empty string when askpanda_atlas is not installed."""
    return ""


def extract_function_source(source: str, func_name: str) -> str | None:  # noqa: ARG001
    """Stub — returns None when askpanda_atlas is not installed."""
    return None


def fetch_pilot_module(
    pilot_path: str,  # noqa: ARG001
    timeout: int,  # noqa: ARG001
) -> tuple[str | None, str]:
    """Stub — returns an error tuple when askpanda_atlas is not installed."""
    return None, "askpanda_atlas is not installed"


def fetch_and_analyse_pilot_source(
    job_id: int,
    log_excerpt: str,  # noqa: ARG001
    pilot_error_diag: str,  # noqa: ARG001
    timeout: int = 15,  # noqa: ARG001
) -> dict[str, Any]:
    """Stub — returns a structured error when askpanda_atlas is not installed."""
    return {
        "evidence": {
            "job_id": job_id,
            "error": (
                "askpanda_atlas is required for pilot source analysis. "
                "Install it alongside askpanda_epic to enable this feature."
            ),
        },
        "text": (
            f"Job {job_id}: pilot source analysis is unavailable — "
            "install askpanda_atlas to enable this feature."
        ),
    }


def _text_content(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "text", "text": json.dumps(data)}]


class PilotSourceAnalysisTool:
    """Stub pilot source analysis tool used when askpanda_atlas is not installed."""

    def __init__(self) -> None:
        """Initialise with the tool definition."""
        self._def = get_definition()

    def get_definition(self) -> dict[str, Any]:
        """Return the MCP tool definition.

        Returns:
            Tool definition dictionary.
        """
        return self._def

    async def call(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a structured error explaining askpanda_atlas is required.

        Args:
            arguments: Tool arguments (job_id required).

        Returns:
            One-element MCP content list with an error evidence dict.
        """
        if not isinstance(arguments, dict):
            return _text_content({"evidence": {"error": "arguments must be a dict"}})
        job_id = arguments.get("job_id")
        if job_id is None:
            return _text_content({"evidence": {"error": "missing job_id"}})
        try:
            job_id_int = int(job_id)
        except (ValueError, TypeError):
            return _text_content({"evidence": {"error": "job_id must be an integer"}})

        log_excerpt = str(arguments.get("log_excerpt") or "")
        pilot_error_diag = str(arguments.get("pilot_error_diag") or "")
        timeout = 15
        try:
            timeout = int(arguments.get("timeout") or 15)
        except (ValueError, TypeError):
            pass

        try:
            result = await asyncio.to_thread(
                fetch_and_analyse_pilot_source,
                job_id_int,
                log_excerpt,
                pilot_error_diag,
                timeout,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return _text_content({
                "evidence": {"job_id": job_id_int, "error": repr(exc)},
                "text": f"Unexpected error: {exc}",
            })
        return _text_content(result)
