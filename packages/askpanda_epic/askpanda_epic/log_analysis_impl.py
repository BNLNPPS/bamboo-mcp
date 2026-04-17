"""ePIC PanDA job log analysis tool — canonical implementation.

Fetches job metadata and pilot log from the PanDA monitor, extracts a relevant
context window using known log message patterns, classifies the failure,
and returns structured evidence suitable for LLM summarisation.

This is experiment-specific logic and belongs in the plugin package, not
in bamboo core.  Core provides only a thin shim that re-exports this tool.

The only bamboo dependency (``bamboo.tools.base``) is imported lazily
inside ``PandaLogAnalysisTool.call()`` so that the pure diagnostic
functions (``classify_failure``, ``extract_log_excerpt``, etc.) remain
importable even when bamboo core is not installed.

Interface
---------
- ``panda_log_analysis_tool.get_definition()`` — MCP tool definition
- ``await panda_log_analysis_tool.call(arguments)`` — returns
  ``list[MCPContent]`` whose ``text`` field is a JSON-serialised dict
  with ``evidence`` and ``text`` keys.

Evidence keys
-------------
job_id, monitor_url, jobstatus, jobsubstatus, computingsite, cloud,
atlasrelease, jeditaskid, attemptnr, maxattempt, piloterrorcode,
piloterrordiag, exeerrorcode, exeerrordiag, taskbuffererrorcode,
taskbuffererrordiag, ddmerrorcode, ddmerrordiag, starttime, endtime,
duration, failure_type, log_url, log_excerpt, log_available.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from typing import Any

from askpanda_epic._fallback_http import get_base_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Failure classification patterns
# ---------------------------------------------------------------------------

# Each entry: (category_name, [keywords to search in combined error text])
# Order matters — first match wins.
_FAILURE_PATTERNS: list[tuple[str, list[str]]] = [
    ("reassigned_by_jedi", ["reassigned by jedi", "toreassign"]),
    ("stagein_timeout", ["file transfer timed out", "timeout during stage-in", "cp_timeout"]),
    ("stageout_timeout", ["timed out during stage-out", "timeout during stage-out"]),
    ("timeout", ["timeout", "timed out", "walltime", "cpu time exceeded", "tobekilled"]),
    ("segfault", ["segmentation fault", "sigsegv", "signal 11"]),
    ("disk_full", ["no space left", "disk quota", "disk full", "work directory.*too large"]),
    ("memory", ["out of memory", "oom killer", "memory limit", "job has exceeded the memory"]),
    ("network", ["connection refused", "network unreachable", "dns failure", "socket error"]),
    ("input_missing", ["no such file", "file not found", "input file missing"]),
    ("stagein_failed", ["failed to stage-in", "stage-in failed", "piloterrorcode.*1099"]),
    ("payload_error", ["athena", "traceback", "exception", "abort", "core dump"]),
    ("pilot_error", ["piloterrorcode"]),
]

# Map pilot error codes to a search string likely to appear near the failure
# in the log, used to anchor the context-window extraction.
_PILOT_CODE_PATTERNS: dict[int, str] = {
    1099: "Failed to stage-in file",
    1104: r"work directory .* is too large",
    1150: "pilot has decided to kill looping job",
    1151: "File transfer timed out",
    1201: "caught signal",
    1235: "job has exceeded the memory limit",
    1305: "",          # payload failure — use tail of payload.stdout instead
    1324: "Service not available",
}

# Number of preceding lines to include when a pattern match is found
_CONTEXT_LINES: int = 40
# For payload logs (no keyword search), take the last N lines
_PAYLOAD_TAIL_LINES: int = 300
# Maximum log excerpt length sent to the LLM (characters)
_MAX_EXCERPT_CHARS: int = 6000


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch_metadata(job_id: int, base_url: str, timeout: int) -> dict[str, Any] | None:
    """Fetch job metadata JSON from the PanDA monitor, using the in-process TTL cache.

    Results are cached for :data:`~askpanda_epic._cache.METADATA_TTL`
    seconds (60 s) so follow-up questions within the same session do not
    trigger redundant HTTP requests.

    Args:
        job_id: PanDA job ID.
        base_url: PanDA monitor base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed JSON dict (with ``job``, ``files``, ``dsfiles`` keys) or
        ``None`` on failure.
    """
    from askpanda_epic._cache import cached_fetch_jsonish  # type: ignore[import]

    url = f"{base_url}/job?pandaid={job_id}&json"
    status, _ctype, _text, payload = cached_fetch_jsonish(url, timeout)
    if status < 200 or status >= 300 or payload is None:
        logger.warning("Metadata fetch failed for job %d: HTTP %d", job_id, status)
        return None
    return payload


def _fetch_log_text(job_id: int, filename: str, base_url: str, timeout: int) -> str | None:
    """Download a pilot or payload log file, using the in-process cache.

    Log files are immutable once written, so hits are cached for the
    lifetime of the process via :func:`~askpanda_epic._cache.cached_fetch_log`
    (TTL = ``math.inf``).  A log that has been downloaded once is never
    re-fetched.

    Args:
        job_id: PanDA job ID.
        filename: Log filename to fetch (e.g. ``pilotlog.txt``).
        base_url: PanDA monitor base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Full log text as a string, or ``None`` if the file is not found
        or the download fails.
    """
    from askpanda_epic._cache import cached_fetch_log  # type: ignore[import]

    url = f"{base_url}/filebrowser/?pandaid={job_id}&json&filename={filename}"
    logger.info("Fetching log (cache-aware): %s", url)
    return cached_fetch_log(url, timeout)


# ---------------------------------------------------------------------------
# Context window extraction
# ---------------------------------------------------------------------------

def _extract_context_window(log_text: str, pattern: str, n_lines: int) -> str:
    """Extract lines from a log up to and including the first pattern match.

    Maintains a rolling buffer of ``n_lines`` and returns it when the
    compiled pattern is found, exactly as AskPanDA's
    ``extract_preceding_lines_streaming`` does.

    Args:
        log_text: Full log content as a string.
        pattern: Regular expression to search for.
        n_lines: Number of lines to include before (and including) the
            match line.

    Returns:
        Extracted context string, or an empty string if the pattern is
        not found.
    """
    compiled = re.compile(pattern, re.IGNORECASE)
    buffer: deque[str] = deque(maxlen=n_lines)
    for line in log_text.splitlines(keepends=True):
        buffer.append(line)
        if compiled.search(line):
            return "".join(buffer)
    return ""


def _extract_tail(log_text: str, n_lines: int) -> str:
    """Return the last ``n_lines`` lines of a log.

    Args:
        log_text: Full log content.
        n_lines: Number of trailing lines to return.

    Returns:
        Last ``n_lines`` lines joined as a single string.
    """
    lines = log_text.splitlines(keepends=True)
    return "".join(lines[-n_lines:])


def _select_log_filename(job: dict[str, Any]) -> str:
    """Choose the appropriate log file to download based on job metadata.

    Pilot error code 1305 indicates a user payload failure; in that case
    ``payload.stdout`` contains the relevant output.  All other failures
    are diagnosed from ``pilotlog.txt``.

    Args:
        job: The ``job`` dict from the PanDA monitor metadata response.

    Returns:
        Log filename string (``"pilotlog.txt"`` or ``"payload.stdout"``).
    """
    pilot_error_code = job.get("piloterrorcode") or 0
    try:
        code = int(pilot_error_code)
    except (ValueError, TypeError):
        code = 0
    return "payload.stdout" if code == 1305 else "pilotlog.txt"


def extract_log_excerpt(
    log_text: str,
    log_filename: str,
    pilot_error_code: int,
    pilot_error_diag: str,
) -> str:
    """Extract the most relevant section of a log file for LLM analysis.

    For pilotlog.txt: searches for a known error keyword anchored to the
    pilot error code, falling back to the raw ``piloterrordiag`` prefix.
    For payload logs (piloterrorcode 1305): returns the last
    ``_PAYLOAD_TAIL_LINES`` lines.

    Args:
        log_text: Full log content as a string.
        log_filename: Name of the log file (used to detect payload logs).
        pilot_error_code: Numeric pilot error code from job metadata.
        pilot_error_diag: Textual pilot error diagnosis from job metadata.

    Returns:
        Extracted context window, truncated to ``_MAX_EXCERPT_CHARS``
        characters.  Empty string if no relevant section is found.
    """
    is_payload = "payload" in log_filename

    if is_payload or pilot_error_code == 1305:
        excerpt = _extract_tail(log_text, _PAYLOAD_TAIL_LINES)
    else:
        search_pattern = _PILOT_CODE_PATTERNS.get(pilot_error_code)
        if search_pattern is None:
            # Unknown code: use first 40 chars of piloterrordiag as pattern
            search_pattern = re.escape(pilot_error_diag[:40]) if pilot_error_diag else ""

        if search_pattern:
            excerpt = _extract_context_window(log_text, search_pattern, _CONTEXT_LINES)
        else:
            excerpt = ""

        # If no match found, fall back to the tail
        if not excerpt:
            logger.info("Pattern not found in log; falling back to tail extraction.")
            excerpt = _extract_tail(log_text, _CONTEXT_LINES)

    return excerpt[:_MAX_EXCERPT_CHARS] if excerpt else ""


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

def classify_failure(job: dict[str, Any], log_excerpt: str) -> str:
    """Classify a job failure from job metadata fields and log excerpt.

    Builds a single search string from key error fields plus the log
    excerpt, then checks it against ``_FAILURE_PATTERNS`` in order.

    Args:
        job: The ``job`` dict from the PanDA monitor metadata response.
        log_excerpt: Extracted context window from the pilot log.

    Returns:
        A short failure category string (e.g. ``"stagein_timeout"``).
        Falls back to ``"unknown"`` if no pattern matches.
    """
    search = " ".join([
        str(job.get("taskbuffererrordiag") or ""),
        str(job.get("piloterrordiag") or ""),
        str(job.get("exeerrordiag") or ""),
        str(job.get("jobsubstatus") or ""),
        str(job.get("commandtopilot") or ""),
        log_excerpt,
    ]).lower()

    for category, keywords in _FAILURE_PATTERNS:
        if any(kw in search for kw in keywords):
            return category
    return "unknown"


# ---------------------------------------------------------------------------
# Synchronous fetch-and-analyse (run via asyncio.to_thread)
# ---------------------------------------------------------------------------

def fetch_and_analyse(job_id: int, base_url: str, timeout: int) -> dict[str, Any]:
    """Fetch metadata and log, extract context window, classify failure.

    Intentionally synchronous so it can be offloaded to a thread pool via
    ``asyncio.to_thread``, keeping the async event loop unblocked during
    network I/O.

    Args:
        job_id: PanDA job ID.
        base_url: PanDA monitor base URL (from environment or default).
        timeout: HTTP timeout in seconds for each request.

    Returns:
        Dict with ``evidence`` and ``text`` keys suitable for
        JSON serialisation and return as MCP content.
    """
    monitor_url = f"{base_url}/job?pandaid={job_id}"
    base_evidence: dict[str, Any] = {
        "job_id": job_id,
        "monitor_url": monitor_url,
    }

    # --- Step 1: Fetch metadata ---
    payload = _fetch_metadata(job_id, base_url, timeout)
    if payload is None:
        base_evidence["error"] = "Failed to fetch job metadata from the PanDA monitor"
        return {
            "evidence": base_evidence,
            "text": f"Could not retrieve metadata for job {job_id}.",
        }

    job: dict[str, Any] = payload.get("job") or {}
    if not job:
        base_evidence["not_found"] = True
        return {
            "evidence": base_evidence,
            "text": f"Job {job_id} was not found in the PanDA monitor.",
        }

    jobstatus = str(job.get("jobstatus") or "")
    pilot_error_code: int = 0
    try:
        pilot_error_code = int(job.get("piloterrorcode") or 0)
    except (ValueError, TypeError):
        pass
    pilot_error_diag: str = str(job.get("piloterrordiag") or "")

    # --- Step 2: Download log (only for failed/holding/cancelled jobs) ---
    log_excerpt = ""
    log_url: str | None = None
    log_available = False

    if jobstatus in ("failed", "holding", "cancelled"):
        log_filename = _select_log_filename(job)
        log_url = (
            f"{base_url}/filebrowser/?pandaid={job_id}"
            f"&json&filename={log_filename}"
        )
        log_text = _fetch_log_text(job_id, log_filename, base_url, timeout)
        if log_text:
            log_available = True
            log_excerpt = extract_log_excerpt(
                log_text, log_filename, pilot_error_code, pilot_error_diag
            )
        else:
            logger.info("Log unavailable for job %d; proceeding with metadata only.", job_id)

    # --- Step 3: Classify failure ---
    failure_type = classify_failure(job, log_excerpt)

    # --- Step 4: Build evidence dict ---
    evidence: dict[str, Any] = {
        **base_evidence,
        "jobstatus": jobstatus,
        "jobsubstatus": job.get("jobsubstatus"),
        "computingsite": job.get("computingsite"),
        "cloud": job.get("cloud"),
        "atlasrelease": job.get("atlasrelease"),
        "jeditaskid": job.get("jeditaskid"),
        "attemptnr": job.get("attemptnr"),
        "maxattempt": job.get("maxattempt"),
        "transformation": job.get("transformation"),
        "piloterrorcode": pilot_error_code,
        "piloterrordiag": pilot_error_diag,
        "exeerrorcode": job.get("exeerrorcode"),
        "exeerrordiag": job.get("exeerrordiag"),
        "taskbuffererrorcode": job.get("taskbuffererrorcode"),
        "taskbuffererrordiag": job.get("taskbuffererrordiag"),
        "ddmerrorcode": job.get("ddmerrorcode"),
        "ddmerrordiag": job.get("ddmerrordiag"),
        "starttime": job.get("starttime"),
        "endtime": job.get("endtime"),
        "duration": job.get("duration"),
        "failure_type": failure_type,
        "log_url": log_url,
        "log_available": log_available,
        "log_excerpt": log_excerpt or None,
    }

    summary = f"Job {job_id} ({jobstatus}): failure type '{failure_type}'."
    if job.get("taskbuffererrordiag"):
        summary += f" Task buffer: {job['taskbuffererrordiag']}."
    elif pilot_error_diag:
        summary += f" Pilot: {pilot_error_diag[:120]}."

    # Build a pre-formatted Markdown links block stored in evidence so that
    # bamboo_executor can append it verbatim after LLM synthesis, bypassing
    # the LLM entirely and guaranteeing real URLs reach the TUI.
    link_lines: list[str] = [f"- [PanDA Monitor]({monitor_url})"]
    if log_url:
        log_label = (
            "Pilot Log"
            if log_available
            else "Pilot Log (may not be available yet)"
        )
        link_lines.append(f"- [{log_label}]({log_url})")
    evidence["links_md"] = "\n\nLinks:\n" + "\n".join(link_lines)

    return {"evidence": evidence, "text": summary}


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for panda_log_analysis.

    Returns:
        Dict with ``name``, ``description``, ``inputSchema``,
        ``examples``, and ``tags`` keys.
    """
    return {
        "name": "panda_log_analysis",
        "description": (
            "Diagnose why a specific PanDA job failed. Downloads the job's "
            "pilot log and error metadata from the PanDA monitor, extracts the "
            "relevant failure context, and classifies the error "
            "(e.g. stage-in timeout, segfault, memory error, network issue, "
            "payload failure, JEDI reassignment). Use when the question asks "
            "why a job failed, what the error was, or what action to take."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": "PanDA job ID (pandaid) to analyse.",
                },
                "query": {
                    "type": "string",
                    "description": "Original user query (optional).",
                },
                "context": {
                    "type": "string",
                    "description": "Optional context (site, task ID, release, etc.).",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "examples": [
            {"job_id": 6799893074, "query": "Why did job 6799893074 fail?"},
        ],
        "tags": ["epic", "eic", "panda", "job", "log", "failure", "diagnosis"],
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class PandaLogAnalysisTool:
    """MCP tool for downloading and analysing PanDA job failure logs.

    Fetches job metadata and pilot/payload logs directly from the PanDA monitor,
    extracts a failure context window using pilot error code patterns,
    classifies the failure, and returns structured evidence for LLM
    summarisation.
    """

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
        """Fetch logs and return structured failure analysis.

        ``bamboo.tools.base`` is imported here (deferred) so the rest of
        this module remains importable when bamboo core is not installed.
        All blocking HTTP calls are offloaded to a thread pool via
        ``asyncio.to_thread`` so the async event loop is not blocked.

        The result is a one-element ``list[MCPContent]`` whose ``text``
        field contains the JSON-serialised evidence dict.  Callers that
        need the raw evidence should parse
        ``json.loads(result[0]["text"])``.  This keeps the tool compliant
        with the MCP narrow-waist contract.

        Args:
            arguments: Dict with required ``job_id`` (int) and optional
                ``query`` (str) and ``context`` (str).

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence and text summary.
        """
        from bamboo.tools.base import text_content  # deferred — see module docstring

        def _err(payload: dict[str, Any]) -> list[Any]:
            return text_content(json.dumps(payload))

        if not isinstance(arguments, dict):
            return _err({
                "evidence": {
                    "error": "arguments must be a dict",
                    "provided": repr(arguments),
                },
            })

        job_id = arguments.get("job_id")
        if job_id is None:
            return _err({"evidence": {"error": "missing job_id", "provided": str(arguments)}})

        try:
            job_id_int = int(job_id)
        except (ValueError, TypeError):
            return _err({
                "evidence": {
                    "error": "job_id must be an integer",
                    "provided": str(arguments),
                },
            })

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
            logger.exception("Unexpected error analysing job %d", job_id_int)
            return _err({
                "evidence": {
                    "job_id": job_id_int,
                    "monitor_url": f"{base_url}/job?pandaid={job_id_int}",
                    "error": repr(exc),
                },
                "text": f"Unexpected error while analysing job {job_id_int}: {exc}",
            })

        return text_content(json.dumps(result))


panda_log_analysis_tool = PandaLogAnalysisTool()

__all__ = [
    "PandaLogAnalysisTool",
    "classify_failure",
    "extract_log_excerpt",
    "fetch_and_analyse",
    "get_definition",
    "panda_log_analysis_tool",
]
