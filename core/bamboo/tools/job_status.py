"""PanDA job status tool.

Fetches job metadata directly from the BigPanDA REST API
(``GET /job?pandaid=<id>&json``) and returns structured evidence suitable
for LLM summarisation.

This tool has **no dependency on the upstream PanDA MCP server** — it talks
to BigPanDA's public HTTP API the same way ``panda_log_analysis`` does.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from bamboo.tools.base import MCPContent, text_content

logger = logging.getLogger(__name__)

#: Default BigPanDA base URL; override with ``PANDA_BASE_URL``.
_DEFAULT_BASE_URL: str = "https://bigpanda.cern.ch"


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for panda_job_status.

    Returns:
        Dict with name, description, inputSchema, examples, and tags.
    """
    return {
        "name": "panda_job_status",
        "description": (
            "Get the status and metadata of a specific PanDA job by its job "
            "ID (pandaid). Use when the question is about an individual job: "
            "its status, pilot errors, execution site, timing, or file summary. "
            "For task-level questions covering many jobs, use panda_task_status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": "PanDA job ID (pandaid).",
                },
                "query": {
                    "type": "string",
                    "description": "Original user query (optional).",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "examples": [
            {"job_id": 6837798305, "query": "What is the status of job 6837798305?"}
        ],
        "tags": ["atlas", "panda", "bigpanda", "job", "monitoring"],
    }


def _build_job_url(base_url: str, job_id: int) -> str:
    """Return the BigPanDA REST URL for a single job's metadata.

    Args:
        base_url: BigPanDA base URL (no trailing slash).
        job_id: PanDA job ID.

    Returns:
        Full URL string, e.g.
        ``"https://bigpanda.cern.ch/job?pandaid=6837798305&json"``.
    """
    return f"{base_url.rstrip('/')}/job?pandaid={job_id}&json"


def _files_summary(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise the files list from a BigPanDA job metadata response.

    Args:
        files: List of file dicts from the ``files`` key of the response.

    Returns:
        Dict with counts by type and status, and up to 10 failed file names.
    """
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    failed: list[str] = []
    for f in files:
        ftype = str(f.get("type") or "unknown")
        fstatus = str(f.get("status") or "unknown")
        by_type[ftype] = by_type.get(ftype, 0) + 1
        by_status[fstatus] = by_status.get(fstatus, 0) + 1
        if fstatus == "failed":
            lfn = f.get("lfn") or f.get("datasetname") or ""
            if lfn:
                failed.append(str(lfn))
    return {
        "total": len(files),
        "by_type": by_type,
        "by_status": by_status,
        "failed_files": failed[:10],
    }


def fetch_job_metadata(job_id: int, base_url: str) -> dict[str, Any]:
    """Fetch job metadata from BigPanDA and return a structured evidence dict.

    Calls ``GET /job?pandaid=<job_id>&json`` using the shared
    ``cached_fetch_jsonish`` helper (60 s TTL).  Synchronous; call via
    ``asyncio.to_thread`` from async contexts if needed.

    Args:
        job_id: PanDA job ID.
        base_url: BigPanDA base URL (no trailing slash).

    Returns:
        Evidence dict with job fields extracted from the API response,
        plus ``monitor_url`` and ``error`` (``None`` on success).
    """
    from askpanda_atlas._cache import cached_fetch_jsonish  # type: ignore[import]

    monitor_url = f"https://bigpanda.cern.ch/job?pandaid={job_id}"
    base_evidence: dict[str, Any] = {
        "job_id": job_id,
        "monitor_url": monitor_url,
        "error": None,
    }

    url = _build_job_url(base_url, job_id)
    logger.debug("panda_job_status: fetching %s", url)

    try:
        status_code, ctype, body, payload = cached_fetch_jsonish(url, ttl=60.0)
    except Exception as exc:  # noqa: BLE001
        base_evidence["error"] = f"HTTP request failed: {exc}"
        return base_evidence

    if status_code < 200 or status_code >= 300:
        base_evidence["error"] = (
            f"BigPanDA returned HTTP {status_code} for job {job_id}."
        )
        return base_evidence

    if payload is None:
        base_evidence["error"] = (
            f"Non-JSON response from BigPanDA (content-type={ctype!r}): "
            f"{body[:200]!r}"
        )
        return base_evidence

    # BigPanDA /job?pandaid=&json returns {"job": {...}, "files": [...]}
    # The top-level may also be the job dict directly in some API versions.
    if isinstance(payload, dict) and "job" in payload:
        job: dict[str, Any] = payload.get("job") or {}
        files: list[dict[str, Any]] = payload.get("files") or []
    elif isinstance(payload, dict) and "pandaid" in payload:
        # Flat response — job fields at top level, no files key
        job = payload
        files = []
    elif isinstance(payload, dict) and not payload.get("job") and not any(
        k not in ("files", "dsfiles", "job") for k in payload
    ):
        # Response contains only files/dsfiles keys — job was not found
        job = {}
        files = []
    else:
        base_evidence["error"] = (
            "Unexpected response structure from BigPanDA — "
            "neither 'job' key nor flat job dict found."
        )
        base_evidence["raw"] = str(payload)[:300]
        return base_evidence

    if not job:
        base_evidence["not_found"] = True
        base_evidence["error"] = f"Job {job_id} was not found in BigPanDA."
        return base_evidence

    return {
        **base_evidence,
        "jobstatus": job.get("jobstatus"),
        "jobsubstatus": job.get("jobsubstatus"),
        "jobname": job.get("jobname"),
        "produsername": job.get("produsername"),
        "computingsite": job.get("computingsite"),
        "cloud": job.get("cloud"),
        "atlasrelease": job.get("atlasrelease"),
        "transformation": job.get("transformation"),
        "jeditaskid": job.get("jeditaskid"),
        "attemptnr": job.get("attemptnr"),
        "maxattempt": job.get("maxattempt"),
        "creationtime": job.get("creationtime"),
        "starttime": job.get("starttime"),
        "endtime": job.get("endtime"),
        "duration": job.get("duration"),
        "waittime": job.get("waittime"),
        "commandtopilot": job.get("commandtopilot"),
        "piloterrorcode": job.get("piloterrorcode"),
        "piloterrordiag": job.get("piloterrordiag"),
        "exeerrorcode": job.get("exeerrorcode"),
        "exeerrordiag": job.get("exeerrordiag"),
        "taskbuffererrorcode": job.get("taskbuffererrorcode"),
        "taskbuffererrordiag": job.get("taskbuffererrordiag"),
        "ddmerrorcode": job.get("ddmerrorcode"),
        "ddmerrordiag": job.get("ddmerrordiag"),
        "cpuconsumptiontime": job.get("cpuconsumptiontime"),
        "gshare": job.get("gshare"),
        "resourcetype": job.get("resourcetype"),
        "corecount": job.get("corecount"),
        "file_summary_str": job.get("file_summary_str"),
        "files_summary": _files_summary(files),
    }


class PandaJobStatusTool:
    """MCP tool for fetching PanDA job status and metadata from BigPanDA."""

    def __init__(self) -> None:
        """Initialise with the tool definition."""
        self._def: dict[str, Any] = get_definition()

    def get_definition(self) -> dict[str, Any]:
        """Return the MCP tool definition.

        Returns:
            Tool definition dictionary.
        """
        return self._def

    async def call(self, arguments: dict[str, Any]) -> list[MCPContent]:
        """Fetch job metadata and return structured evidence.

        Fetches directly from the BigPanDA REST API — no upstream MCP
        server required.  Uses ``asyncio.to_thread`` to keep the event
        loop free during the synchronous HTTP call.

        Args:
            arguments: Dict with required ``job_id`` and optional ``query``.

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence and text summary.
        """
        import asyncio

        if not isinstance(arguments, dict):
            return text_content(json.dumps({
                "evidence": {
                    "error": "arguments must be a dict",
                    "provided": repr(arguments),
                },
            }))

        job_id = arguments.get("job_id")
        if job_id is None:
            return text_content(json.dumps({
                "evidence": {"error": "missing job_id", "provided": arguments},
            }))

        try:
            job_id_int = int(job_id)
        except Exception:  # noqa: BLE001
            return text_content(json.dumps({
                "evidence": {
                    "error": "job_id must be an integer",
                    "provided": str(arguments),
                },
            }))

        base_url: str = os.environ.get("PANDA_BASE_URL", _DEFAULT_BASE_URL)

        evidence = await asyncio.to_thread(fetch_job_metadata, job_id_int, base_url)

        if evidence.get("error"):
            return text_content(json.dumps({
                "evidence": evidence,
                "text": (
                    f"Could not retrieve metadata for job {job_id_int}: "
                    f"{evidence['error']}  "
                    f"Monitor: {evidence.get('monitor_url', '')}"
                ),
            }))

        status = evidence.get("jobstatus") or "unknown"
        summary = f"Job {job_id_int} status: {status}."
        if evidence.get("taskbuffererrordiag"):
            summary += f" Reason: {evidence['taskbuffererrordiag']}."
        return text_content(json.dumps({"evidence": evidence, "text": summary}))


panda_job_status_tool = PandaJobStatusTool()

__all__ = ["PandaJobStatusTool", "fetch_job_metadata", "panda_job_status_tool", "get_definition"]
