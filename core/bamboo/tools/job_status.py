"""PanDA job status tool.

Fetches job metadata from BigPanDA via the ``panda`` MCP
server and returns structured evidence suitable for LLM summarisation.

The upstream server returns a JSON object with ``job`` and ``files`` keys.
This tool extracts the most useful fields from ``job`` and computes a
``files_summary`` from the ``files`` list.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from bamboo.tools._mcp_caller import get_mcp_caller  # type: ignore[import-untyped]
from bamboo.tools.base import MCPContent, text_content

logger = logging.getLogger(__name__)

_SERVER = "panda"
_TOOL = "download_bigpanda_metadata"


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


def _files_summary(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise the files list from a BigPanDA job metadata response.

    Args:
        files: List of file dicts from the ``files`` key of the response.

    Returns:
        Dict with counts by type and status, and failed file names.
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

        The result is a one-element ``list[MCPContent]`` whose ``text`` field
        contains the JSON-serialised evidence dict.  Callers that need the raw
        evidence should parse ``json.loads(result[0]["text"])``.  This keeps
        the tool compliant with the MCP narrow-waist contract.

        Args:
            arguments: Dict with required ``job_id`` and optional ``query``.

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence and text summary.
        """
        if not isinstance(arguments, dict):
            return text_content(json.dumps({"evidence": {"error": "arguments must be a dict", "provided": repr(arguments)}}))

        job_id = arguments.get("job_id")
        if job_id is None:
            return text_content(json.dumps({"evidence": {"error": "missing job_id", "provided": arguments}}))

        try:
            job_id_int = int(job_id)
        except Exception:  # pylint: disable=broad-exception-caught
            return text_content(json.dumps({"evidence": {"error": "job_id must be an integer", "provided": str(arguments)}}))

        monitor_url = f"https://bigpanda.cern.ch/job?pandaid={job_id_int}"
        base_evidence: dict[str, Any] = {
            "job_id": job_id_int,
            "monitor_url": monitor_url,
        }

        caller = get_mcp_caller()
        result = await caller.call(
            server_name=_SERVER,
            tool_name=_TOOL,
            arguments={"panda_id": str(job_id_int)},
        )

        if result["error"]:
            base_evidence["error"] = result["error"]
            return text_content(json.dumps({
                "evidence": base_evidence,
                "text": f"Failed to fetch metadata for job {job_id_int}: {result['error']}",
            }))

        raw_text: str = result["text"] or ""

        # The response starts with a "Job NNN metadata:" header, then JSON
        json_start = raw_text.find("{")
        if json_start < 0:
            base_evidence["error"] = "No JSON found in response"
            base_evidence["raw"] = raw_text[:500]
            return text_content(json.dumps({
                "evidence": base_evidence,
                "text": f"Unexpected response format for job {job_id_int}.",
            }))

        try:
            data: dict[str, Any] = json.loads(raw_text[json_start:])
        except json.JSONDecodeError as e:
            base_evidence["error"] = f"JSON parse error: {e}"
            base_evidence["raw"] = raw_text[:500]
            return text_content(json.dumps({
                "evidence": base_evidence,
                "text": f"Could not parse metadata for job {job_id_int}.",
            }))

        job: dict[str, Any] = data.get("job") or {}
        files: list[dict[str, Any]] = data.get("files") or []

        if not job:
            base_evidence["not_found"] = True
            return text_content(json.dumps({
                "evidence": base_evidence,
                "text": f"Job {job_id_int} was not found in BigPanDA.",
            }))

        evidence: dict[str, Any] = {
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

        status = evidence.get("jobstatus") or "unknown"
        summary = f"Job {job_id_int} status: {status}."
        if evidence.get("taskbuffererrordiag"):
            summary += f" Reason: {evidence['taskbuffererrordiag']}."
        return text_content(json.dumps({"evidence": evidence, "text": summary}))


panda_job_status_tool = PandaJobStatusTool()

__all__ = ["PandaJobStatusTool", "panda_job_status_tool", "get_definition"]
