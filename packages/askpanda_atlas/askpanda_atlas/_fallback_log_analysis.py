"""Fallback PandaLogAnalysisTool for standalone (no bamboo core) use.

Used only when bamboo core is not installed.  Imports the pure diagnostic
functions from ``log_analysis_impl`` (which are safe to import without
bamboo since ``bamboo.tools.base`` is only imported inside ``call()``),
and uses a local ``_text_content`` helper in place of
``bamboo.tools.base.text_content``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from askpanda_atlas.log_analysis_impl import (  # noqa: F401  (re-exported)
    FailureContext,
    classify_failure,
    extract_failure_context,
    extract_log_excerpt,
    fetch_and_analyse,
    get_definition,
    get_base_url,
)


def _text_content(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Wrap a dict as a JSON-serialised MCP text content item.

    Args:
        data: Dict to serialise.

    Returns:
        One-element MCP content list.
    """
    return [{"type": "text", "text": json.dumps(data)}]


class PandaLogAnalysisTool:
    """Self-contained log analysis tool used when bamboo core is not installed."""

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
        """Fetch logs and return structured failure analysis.

        Args:
            arguments: Dict with required ``job_id`` and optional fields.

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

        timeout: int = 60
        try:
            timeout = int(arguments.get("timeout") or 60)
        except (ValueError, TypeError):
            pass

        base_url = get_base_url()
        try:
            result = await asyncio.to_thread(
                fetch_and_analyse, job_id_int, base_url, timeout
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return _text_content({
                "evidence": {
                    "job_id": job_id_int,
                    "monitor_url": f"{base_url}/job?pandaid={job_id_int}",
                    "error": repr(exc),
                },
                "text": f"Unexpected error while analysing job {job_id_int}: {exc}",
            })
        return _text_content(result)
