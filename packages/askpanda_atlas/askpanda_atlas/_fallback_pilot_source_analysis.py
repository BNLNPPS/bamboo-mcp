"""Fallback PilotSourceAnalysisTool for standalone (no bamboo core) use.

Used only when bamboo core is not installed.  Imports the pure diagnostic
functions from ``pilot_source_analysis_impl`` (which are safe to import
without bamboo since ``bamboo.tools.base`` is only imported inside
``call()``), and uses a local ``_text_content`` helper in place of
``bamboo.tools.base.text_content``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from askpanda_atlas.pilot_source_analysis_impl import (  # noqa: F401  (re-exported)
    extract_function_source,
    fetch_and_analyse_pilot_source,
    fetch_pilot_module,
    get_definition,
    parse_exception_line,
    parse_traceback_frames,
)


def _text_content(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Wrap a dict as a JSON-serialised MCP text content item.

    Args:
        data: Dict to serialise.

    Returns:
        One-element MCP content list.
    """
    return [{"type": "text", "text": json.dumps(data)}]


class PilotSourceAnalysisTool:
    """Self-contained pilot source analysis tool used when bamboo core is not installed."""

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
        """Fetch pilot source and return structured analysis.

        Args:
            arguments: Dict with required ``job_id`` and ``log_excerpt``,
                and optional ``pilot_error_diag``.

        Returns:
            One-element MCP content list containing JSON-serialised evidence.
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
