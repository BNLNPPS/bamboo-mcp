"""ATLAS PanDA job log analysis tool — canonical implementation.

Fetches job metadata and pilot log from BigPanDA, extracts a relevant
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
from dataclasses import dataclass, field
from typing import Any

from askpanda_atlas._fallback_http import get_base_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Failure classification patterns
# ---------------------------------------------------------------------------

# Each entry: (category_name, [keywords to search in combined error text])
# Order matters — first match wins.
# stageout_timeout MUST appear before stagein_timeout: the piloterrordiag for
# stage-out timeouts (code 1152) begins with "File transfer timed out during
# stage-out", which also matches the stagein_timeout keyword "file transfer
# timed out".  The more specific stage-out check must win.
_FAILURE_PATTERNS: list[tuple[str, list[str]]] = [
    ("reassigned_by_jedi", ["reassigned by jedi", "toreassign"]),
    ("stageout_timeout", [
        "file transfer timed out during stage-out",
        "timed out during stage-out",
        "timeout during stage-out",
    ]),
    ("stagein_timeout", ["file transfer timed out", "timeout during stage-in", "cp_timeout"]),
    ("timeout", ["timeout", "timed out", "walltime", "cpu time exceeded", "tobekilled"]),
    ("segfault", ["segmentation fault", "sigsegv", "signal 11"]),
    ("disk_full", ["no space left", "disk quota", "disk full", "work directory.*too large"]),
    ("memory", ["out of memory", "oom killer", "memory limit", "job has exceeded the memory"]),
    ("network", ["connection refused", "network unreachable", "dns failure", "socket error"]),
    ("input_missing", ["no such file", "file not found", "input file missing"]),
    ("stagein_failed", ["failed to stage-in", "stage-in failed", "piloterrorcode.*1099"]),
    # Pilot infrastructure errors: UID not found in process table scan during CPU monitoring.
    # Must appear before payload_error — the WARNING log contains "exception" and would
    # otherwise be misclassified as a user payload failure.
    ("pilot_monitoring_error", ["getpwuid", "uid not found", "list_processes_and_threads"]),
    # Release / container setup failures detected in setup.stdout before payload runs.
    # Must appear before payload_error so the setup log excerpt wins over the empty
    # payload stdout that accompanies these jobs.
    ("setup_release_not_found", ["no matched release is found", "!!!error!!!"]),
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
    # 1354: UID not found when scanning the process table for CPU monitoring.
    # This is a pilot infrastructure error, not a user payload failure.
    1354: "getpwuid",
}

# Pilot error codes whose diagnostic content *follows* the anchor line
# (e.g. a WARNING header followed by a multi-line traceback).
# For these codes _extract_context_window_with_trailing is used instead of
# _extract_context_window so the traceback body is included in the excerpt.
_TRAILING_CONTEXT_CODES: frozenset[int] = frozenset({1354})

# Number of preceding lines to include when a pattern match is found
_CONTEXT_LINES: int = 40
# Number of lines to continue collecting *after* the match line for error
# codes whose relevant content follows the anchor (e.g. Python tracebacks
# that start with a WARNING line and then span several subsequent lines).
_TRAILING_LINES: int = 30
# For pilotlog.txt fallback: number of tail lines when no pattern matches
_PAYLOAD_TAIL_LINES: int = 300
# Maximum log excerpt length sent to the LLM (characters)
_MAX_EXCERPT_CHARS: int = 6000
# Characters reserved for payload.stderr within the excerpt budget.
# Guarantees the stderr traceback is always included even when stdout is long.
_STDERR_RESERVED_CHARS: int = 2000
# Characters taken from the end of payload.stdout (char-based, not line-based).
# Char-based slicing guarantees recency regardless of line length — verbose
# INFO lines won't push ERROR lines out of the budget the way a line-count does.
_STDOUT_CHAR_TAIL: int = _MAX_EXCERPT_CHARS - _STDERR_RESERVED_CHARS


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch_metadata(job_id: int, base_url: str, timeout: int) -> dict[str, Any] | None:
    """Fetch job metadata JSON from BigPanDA, using the in-process TTL cache.

    Results are cached for :data:`~askpanda_atlas._cache.METADATA_TTL`
    seconds (60 s) so follow-up questions within the same session do not
    trigger redundant HTTP requests.

    Args:
        job_id: PanDA job ID.
        base_url: BigPanDA base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed JSON dict (with ``job``, ``files``, ``dsfiles`` keys) or
        ``None`` on failure.
    """
    from askpanda_atlas._cache import cached_fetch_jsonish  # type: ignore[import]

    url = f"{base_url}/job?pandaid={job_id}&json"
    status, _ctype, _text, payload = cached_fetch_jsonish(url, timeout)
    if status < 200 or status >= 300 or payload is None:
        logger.warning("Metadata fetch failed for job %d: HTTP %d", job_id, status)
        return None
    return payload


def _fetch_log_text(job_id: int, filename: str, base_url: str, timeout: int) -> str | None:
    """Download a pilot or payload log file, using the in-process cache.

    Log files are immutable once written, so hits are cached for the
    lifetime of the process via :func:`~askpanda_atlas._cache.cached_fetch_log`
    (TTL = ``math.inf``).  A log that has been downloaded once is never
    re-fetched.

    Args:
        job_id: PanDA job ID.
        filename: Log filename to fetch (e.g. ``pilotlog.txt``).
        base_url: BigPanDA base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Full log text as a string, or ``None`` if the file is not found
        or the download fails.
    """
    from askpanda_atlas._cache import cached_fetch_log  # type: ignore[import]

    url = f"{base_url}/filebrowser/?pandaid={job_id}&json&filename={filename}"
    logger.info("Fetching log (cache-aware): %s", url)
    return cached_fetch_log(url, timeout)


# ---------------------------------------------------------------------------
# Setup log error detection
# ---------------------------------------------------------------------------

# Patterns that, when found in setup.stdout, indicate a fatal setup error that
# is the definitive root cause of the job failure.  When any of these match,
# payload.stdout and payload.stderr are not downloaded — they will be empty
# (the payload never ran) and attempting to fetch them wastes time and budget.
# Order does not matter; all are checked.
_SETUP_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"!!!error!!!", re.IGNORECASE),
    re.compile(r"no matched release is found", re.IGNORECASE),
    re.compile(r"asetup.*failed", re.IGNORECASE),
    re.compile(r"error.*release.*not.*found", re.IGNORECASE),
]


def _setup_log_has_error(setup_text: str) -> bool:
    """Return True if setup.stdout contains a recognisable fatal setup error.

    Checks ``setup_text`` against :data:`_SETUP_ERROR_PATTERNS`.  A match
    means the Athena/Apptainer environment setup failed before the payload
    could start, making payload.stdout and payload.stderr empty and
    irrelevant.

    Args:
        setup_text: Full content of the setup.stdout log file.

    Returns:
        ``True`` if any setup-error pattern matches; ``False`` otherwise.
    """
    for pattern in _SETUP_ERROR_PATTERNS:
        if pattern.search(setup_text):
            return True
    return False


def _fetch_file_index(
    job_id: int,
    base_url: str,
    timeout: int,
) -> dict[str, int] | None:
    """Fetch the filebrowser directory listing for a job, returning file sizes.

    Calls ``/filebrowser/?pandaid={job_id}&json`` (no ``filename=`` param) to
    retrieve a JSON list of files available for the job.  The result is parsed
    into a mapping of ``{filename: size_in_bytes}`` so callers can skip
    zero-length files before attempting to download them.

    The response is cached via :func:`~askpanda_atlas._cache.cached_fetch_jsonish`
    using the metadata TTL (60 s).  A ``None`` return means the listing could
    not be fetched; callers must treat this as "assume all files are non-empty"
    (fail-open) so that an unavailable index does not suppress log downloads.

    BigPanDA's filebrowser JSON listing uses the following structure::

        [{"name": "pilotlog.txt", "size": 123456}, ...]

    Args:
        job_id: PanDA job ID.
        base_url: BigPanDA base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Dict mapping filename to size in bytes, or ``None`` on failure.
    """
    from askpanda_atlas._cache import cached_fetch_jsonish  # type: ignore[import]

    url = f"{base_url}/filebrowser/?pandaid={job_id}&json"
    logger.info("Fetching file index: %s", url)
    status, _ctype, _text, payload = cached_fetch_jsonish(url, timeout)
    if status < 200 or status >= 300 or payload is None:
        logger.warning(
            "File index fetch failed for job %d: HTTP %d", job_id, status
        )
        return None

    # The response may be a list directly, or a dict wrapping a list.
    entries: list[Any] = []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        # Some BigPanDA versions wrap the list under a "files" key.
        for key in ("files", "data", "results"):
            if isinstance(payload.get(key), list):
                entries = payload[key]
                break

    if not entries:
        logger.debug("File index for job %d is empty or unrecognised format", job_id)
        return {}

    index: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("filename") or ""
        size = entry.get("size") or entry.get("fsize") or 0
        if name:
            try:
                index[str(name)] = int(size)
            except (ValueError, TypeError):
                index[str(name)] = 0

    return index


def _file_is_nonempty(file_index: dict[str, int] | None, filename: str) -> bool:
    """Return True if *filename* should be downloaded based on the file index.

    Uses a fail-open policy: if *file_index* is ``None`` (the directory
    listing could not be fetched), the file is considered non-empty and
    the download proceeds.  This preserves existing behaviour when the
    index endpoint is unavailable.

    Args:
        file_index: Mapping of ``{filename: size_bytes}`` from
            :func:`_fetch_file_index`, or ``None`` if the index is
            unavailable.
        filename: The log filename to check (e.g. ``"setup.stdout"``).

    Returns:
        ``True`` if the file should be downloaded; ``False`` if it is
        confirmed to have zero bytes and can safely be skipped.
    """
    if file_index is None:
        return True  # fail-open: index unavailable, assume non-empty
    size = file_index.get(filename)
    if size is None:
        # File not listed at all — may not exist yet; attempt download anyway
        # to get a definitive 404 rather than silently skipping.
        return True
    return size > 0


# ---------------------------------------------------------------------------
# Payload log noise stripping
# ---------------------------------------------------------------------------

# Matches PanDA pilot "=== ls in <dir> ===" directory listing sections.
# These appear in payload.stdout between the application error output and the
# "==== Result ====" footer, consuming budget without diagnostic value.
_LS_SECTION_RE: re.Pattern[str] = re.compile(
    r"\n=== ls in [^\n]+=+\n"
    r"(?:(?:total \d+|[dlrwxs\-]{10})[^\n]*\n)*",
    re.MULTILINE,
)


def _strip_payload_noise(text: str) -> str:
    """Remove PanDA pilot boilerplate sections from payload stdout text.

    Strips ``=== ls in <dir> ===`` directory listing blocks that the PanDA
    pilot appends between the application output and the result footer.  These
    sections are structurally identifiable (heading + lines starting with
    permission bits or ``total N``) and consume character budget without
    contributing diagnostic information.

    Args:
        text: Raw payload stdout text.

    Returns:
        Text with ls listing sections removed and excess blank lines collapsed.
    """
    text = _LS_SECTION_RE.sub("\n", text)
    # Collapse runs of 3+ blank lines left by the removed sections.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


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


def _extract_context_window_with_trailing(
    log_text: str,
    pattern: str,
    n_before: int,
    n_trailing: int,
) -> str:
    """Extract lines around a pattern match, including lines that follow it.

    Like :func:`_extract_context_window` but continues collecting up to
    ``n_trailing`` lines after the match.  Collection stops early if a blank
    line is encountered after the match (blank lines reliably signal the end
    of a Python traceback block in pilot logs).

    Use this for error codes whose diagnostic content *follows* the anchor
    line (e.g. a WARNING header line followed by a multi-line traceback).

    Args:
        log_text: Full log content as a string.
        pattern: Regular expression to search for.
        n_before: Lines to include before (and including) the match line.
        n_trailing: Maximum additional lines to collect after the match.

    Returns:
        Extracted context string, or an empty string if the pattern is
        not found.
    """
    compiled = re.compile(pattern, re.IGNORECASE)
    buffer: deque[str] = deque(maxlen=n_before)
    lines = log_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        buffer.append(line)
        if compiled.search(line):
            # Collect up to n_trailing further lines, stopping on a blank line.
            trailing: list[str] = []
            for subsequent in lines[i + 1:i + 1 + n_trailing]:
                if subsequent.strip() == "":
                    trailing.append(subsequent)
                    break
                trailing.append(subsequent)
            return "".join(buffer) + "".join(trailing)
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
    """Choose the primary log file to download based on job metadata.

    Pilot error code 1305 indicates a user payload failure; in that case
    ``payload.stdout`` is the primary log.  ``payload.stderr`` is also
    fetched separately and appended to the excerpt when available, since
    some failures (e.g. Python tracebacks, segfaults) only appear there.
    All other failures are diagnosed from ``pilotlog.txt``.

    Args:
        job: The ``job`` dict from the BigPanDA metadata response.

    Returns:
        Primary log filename string (``"pilotlog.txt"`` or ``"payload.stdout"``).
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
        # Strip pilot boilerplate (ls listings) before taking the char tail
        # so the budget is spent on application errors, not directory output.
        # Char-based tail: errors appear at the very end of payload.stdout;
        # a line-count tail on verbose logs would cut them off.
        log_text = _strip_payload_noise(log_text)
        excerpt = log_text[-_STDOUT_CHAR_TAIL:]
    else:
        search_pattern = _PILOT_CODE_PATTERNS.get(pilot_error_code)
        if search_pattern is None:
            # Unknown code: use first 40 chars of piloterrordiag as pattern
            search_pattern = re.escape(pilot_error_diag[:40]) if pilot_error_diag else ""

        if search_pattern:
            if pilot_error_code in _TRAILING_CONTEXT_CODES:
                excerpt = _extract_context_window_with_trailing(
                    log_text, search_pattern, _CONTEXT_LINES, _TRAILING_LINES
                )
            else:
                excerpt = _extract_context_window(log_text, search_pattern, _CONTEXT_LINES)
        else:
            excerpt = ""

        # If no match found, fall back to the tail.
        # WARNING-level so production logs surface gaps in _PILOT_CODE_PATTERNS.
        if not excerpt:
            logger.warning(
                "Pattern %r not found in log for pilot error code %d; "
                "falling back to tail extraction. Consider adding this code "
                "to _PILOT_CODE_PATTERNS.",
                search_pattern,
                pilot_error_code,
            )
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
        job: The ``job`` dict from the BigPanDA metadata response.
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

@dataclass
class _LogFetchResult:
    """Collected outputs from one of the log-fetch helper functions.

    Bundles the six mutable values that the two fetch helpers must return to
    ``fetch_and_analyse`` so the helpers can be extracted into their own
    functions without needing to return an unwieldy tuple.

    Attributes:
        log_excerpt: Extracted context window for LLM analysis.
        log_url: Filebrowser URL of the primary log file, or ``None``.
        log_available: True if any usable log content was obtained.
        stderr_url: Filebrowser URL of ``payload.stderr``, or ``None``.
        setup_log_url: Filebrowser URL of ``setup.stdout``, or ``None``.
        setup_log_excerpt: Full (budget-capped) content of ``setup.stdout``,
            or ``None`` if it was not fetched or contained no error.
    """

    log_excerpt: str = ""
    log_url: str | None = None
    log_available: bool = False
    stderr_url: str | None = None
    setup_log_url: str | None = None
    setup_log_excerpt: str | None = field(default=None)


def _fetch_logs_payload(
    job_id: int,
    pilot_error_code: int,
    pilot_error_diag: str,
    file_index: dict[str, int] | None,
    base_url: str,
    timeout: int,
) -> _LogFetchResult:
    """Fetch and excerpt logs for pilot error code 1305 (payload failure).

    Checks ``setup.stdout`` first.  If it contains a recognisable fatal setup
    error the function returns immediately with that content as the excerpt
    and does not attempt ``payload.stdout`` or ``payload.stderr`` — the
    payload never ran so those files will be empty.

    If ``setup.stdout`` is absent, empty, or error-free the function falls
    through to the ``payload.stdout`` → ``payload.stderr`` path, skipping any
    file that the file-size index confirms to be zero-length.

    Args:
        job_id: PanDA job ID.
        pilot_error_code: Numeric pilot error code (always 1305 for this path).
        pilot_error_diag: Textual pilot error diagnosis from job metadata.
        file_index: Mapping of ``{filename: size_bytes}`` from
            :func:`_fetch_file_index`, or ``None`` (fail-open).
        base_url: BigPanDA base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Populated :class:`_LogFetchResult` instance.
    """
    result = _LogFetchResult()

    # --- setup.stdout first ---
    setup_fetched = False
    if _file_is_nonempty(file_index, "setup.stdout"):
        result.setup_log_url = (
            f"{base_url}/filebrowser/?pandaid={job_id}&json&filename=setup.stdout"
        )
        setup_text = _fetch_log_text(job_id, "setup.stdout", base_url, timeout)
        if setup_text:
            setup_fetched = True
            if _setup_log_has_error(setup_text):
                result.setup_log_excerpt = setup_text[:_MAX_EXCERPT_CHARS]
                result.log_excerpt = result.setup_log_excerpt
                result.log_available = True
                logger.info(
                    "Setup error found in setup.stdout for job %d; "
                    "skipping payload.stdout and payload.stderr.",
                    job_id,
                )
                return result
    else:
        logger.info("setup.stdout is zero-length for job %d; skipping.", job_id)

    # --- Fall through to payload logs ---
    log_filename = "payload.stdout"
    result.log_url = (
        f"{base_url}/filebrowser/?pandaid={job_id}&json&filename={log_filename}"
    )
    log_text: str | None = None
    if _file_is_nonempty(file_index, log_filename):
        log_text = _fetch_log_text(job_id, log_filename, base_url, timeout)
    else:
        logger.info("payload.stdout is zero-length for job %d; skipping.", job_id)

    stderr_text: str | None = None
    if _file_is_nonempty(file_index, "payload.stderr"):
        result.stderr_url = (
            f"{base_url}/filebrowser/?pandaid={job_id}&json&filename=payload.stderr"
        )
        stderr_text = _fetch_log_text(job_id, "payload.stderr", base_url, timeout)
    else:
        logger.info("payload.stderr is zero-length for job %d; skipping.", job_id)

    if log_text or stderr_text:
        result.log_available = True
        stdout_budget = _MAX_EXCERPT_CHARS - _STDERR_RESERVED_CHARS
        stdout_excerpt = extract_log_excerpt(
            log_text or "", log_filename,
            pilot_error_code, pilot_error_diag,
        )[:stdout_budget]
        if stderr_text:
            result.log_excerpt = (
                stdout_excerpt
                + "\n\n--- payload.stderr ---\n"
                + stderr_text[:_STDERR_RESERVED_CHARS]
            )
        else:
            result.log_excerpt = stdout_excerpt
    elif setup_fetched:
        # setup.stdout was fetched but contained no recognised error; use it
        # as the excerpt so the LLM still has environment context.
        result.log_available = True
        result.log_excerpt = result.setup_log_excerpt or ""
    else:
        logger.info(
            "No usable logs for job %d; proceeding with metadata only.", job_id
        )

    return result


def _fetch_logs_pilotlog(
    job: dict[str, Any],
    job_id: int,
    pilot_error_code: int,
    pilot_error_diag: str,
    file_index: dict[str, int] | None,
    base_url: str,
    timeout: int,
) -> _LogFetchResult:
    """Fetch and excerpt the pilot log for all non-1305 error codes.

    Selects either ``pilotlog.txt`` or ``payload.stdout`` via
    :func:`_select_log_filename`, skips the file if the size index confirms
    zero bytes, then extracts the relevant context window.

    Args:
        job: The ``job`` dict from the BigPanDA metadata response.
        job_id: PanDA job ID.
        pilot_error_code: Numeric pilot error code from job metadata.
        pilot_error_diag: Textual pilot error diagnosis from job metadata.
        file_index: Mapping of ``{filename: size_bytes}`` from
            :func:`_fetch_file_index`, or ``None`` (fail-open).
        base_url: BigPanDA base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Populated :class:`_LogFetchResult` instance.
    """
    result = _LogFetchResult()
    log_filename = _select_log_filename(job)
    result.log_url = (
        f"{base_url}/filebrowser/?pandaid={job_id}&json&filename={log_filename}"
    )

    log_text: str | None = None
    if _file_is_nonempty(file_index, log_filename):
        log_text = _fetch_log_text(job_id, log_filename, base_url, timeout)
    else:
        logger.info("%s is zero-length for job %d; skipping.", log_filename, job_id)

    if log_text:
        result.log_available = True
        result.log_excerpt = extract_log_excerpt(
            log_text, log_filename, pilot_error_code, pilot_error_diag,
        )
    else:
        logger.info(
            "Log unavailable for job %d; proceeding with metadata only.", job_id
        )

    return result


def fetch_and_analyse(job_id: int, base_url: str, timeout: int) -> dict[str, Any]:
    """Fetch metadata and logs, extract context window, classify failure.

    Intentionally synchronous so it can be offloaded to a thread pool via
    ``asyncio.to_thread``, keeping the async event loop unblocked during
    network I/O.

    For pilot error code 1305 the download order is: ``setup.stdout`` first
    (see :func:`_fetch_logs_payload`); for all other codes ``pilotlog.txt``
    is used (see :func:`_fetch_logs_pilotlog`).  Both helpers skip any file
    confirmed to be zero-length by the filebrowser file-size index.

    Args:
        job_id: PanDA job ID.
        base_url: BigPanDA base URL (from environment or default).
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
        base_evidence["error"] = "Failed to fetch job metadata from BigPanDA"
        return {
            "evidence": base_evidence,
            "text": f"Could not retrieve metadata for job {job_id}.",
        }

    job: dict[str, Any] = payload.get("job") or {}
    if not job:
        base_evidence["not_found"] = True
        return {
            "evidence": base_evidence,
            "text": f"Job {job_id} was not found in BigPanDA.",
        }

    jobstatus = str(job.get("jobstatus") or "")
    pilot_error_code: int = 0
    try:
        pilot_error_code = int(job.get("piloterrorcode") or 0)
    except (ValueError, TypeError):
        pass
    pilot_error_diag: str = str(job.get("piloterrordiag") or "")

    # --- Step 2: Download logs (only for failed/holding/cancelled jobs) ---
    fetch_result = _LogFetchResult()

    if jobstatus in ("failed", "holding", "cancelled"):
        # Fetch the file-size index once so both helpers can skip zero-length
        # files.  Returns None on failure; helpers treat None as fail-open.
        file_index = _fetch_file_index(job_id, base_url, timeout)

        if pilot_error_code == 1305:
            fetch_result = _fetch_logs_payload(
                job_id, pilot_error_code, pilot_error_diag,
                file_index, base_url, timeout,
            )
        else:
            fetch_result = _fetch_logs_pilotlog(
                job, job_id, pilot_error_code, pilot_error_diag,
                file_index, base_url, timeout,
            )

    log_excerpt = fetch_result.log_excerpt
    log_url = fetch_result.log_url
    log_available = fetch_result.log_available
    stderr_url = fetch_result.stderr_url
    setup_log_url = fetch_result.setup_log_url
    setup_log_excerpt = fetch_result.setup_log_excerpt

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
        "stderr_url": stderr_url,
        "setup_log_url": setup_log_url,
        "setup_log_excerpt": setup_log_excerpt,
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
    link_lines: list[str] = [f"- [BigPanDA Monitor]({monitor_url})"]
    if setup_log_url:
        link_lines.append(f"- [Setup Log]({setup_log_url})")
    if log_url:
        log_label = (
            "Payload Log"
            if log_available and pilot_error_code == 1305
            else "Pilot Log"
            if log_available
            else "Pilot Log (may not be available yet)"
        )
        link_lines.append(f"- [{log_label}]({log_url})")
    if stderr_url:
        link_lines.append(f"- [Payload stderr]({stderr_url})")
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
            "pilot log and error metadata from BigPanDA, extracts the "
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
        "tags": ["atlas", "panda", "bigpanda", "job", "log", "failure", "diagnosis"],
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class PandaLogAnalysisTool:
    """MCP tool for downloading and analysing PanDA job failure logs.

    Fetches job metadata and pilot/payload logs directly from BigPanDA,
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
    "_fetch_file_index",
    "_file_is_nonempty",
    "_setup_log_has_error",
]
