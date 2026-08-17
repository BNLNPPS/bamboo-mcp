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

Extraction strategy
-------------------
Excerpt extraction is *traceback-first*.  Before consulting any error-code
lookup table the log is scanned for a real Python traceback using the format
invariants in :mod:`askpanda_atlas._traceback_parse`; when one is found the
excerpt is built around it and the exception is also returned as structured
evidence (``exception_type``, ``exception_message``, ``deepest_pilot_frame``).

Only when no traceback exists does extraction fall back to the previous
behaviour: the ``_PILOT_CODE_PATTERNS`` anchor for the job's pilot error code,
then ``piloterrordiag`` as a literal regex, then the tail of the log.  Those
fallbacks are unreliable because ``piloterrordiag`` is written by a different
pilot code path than the log record, so the wordings frequently differ — pilot
error code 1310 reports ``"Exception caught during payload execution"`` in
metadata while the log record reads ``"execute payloads caught an exception
(cannot recover): timed out, Traceback ..."``.  When the anchor missed, the
excerpt used to degrade to the tail of the log, which for a failed job is
stage-out and log-archiving boilerplate that contains no diagnostic content.

Evidence keys
-------------
job_id, monitor_url, jobstatus, jobsubstatus, computingsite, cloud,
atlasrelease, jeditaskid, attemptnr, maxattempt, piloterrorcode,
piloterrordiag, exeerrorcode, exeerrordiag, taskbuffererrorcode,
taskbuffererrordiag, ddmerrorcode, ddmerrordiag, starttime, endtime,
duration, failure_type, log_url, log_excerpt, log_available,
traceback_available, exception_type, exception_message, exception_frames,
deepest_pilot_frame, traceback_count, pilot_version, code_analysis_offer_md.
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
from askpanda_atlas._traceback_parse import (
    ExceptionInfo,
    TracebackBlock,
    find_primary_exception,
    parse_pilot_version,
    parse_pilot_version_from_pilotid,
    truncate_traceback,
)

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

# Failure categories produced only by _classify_from_exception (never by the
# substring table above).  Listed here for documentation and for consumers that
# need to enumerate the full category vocabulary.
#
# transform_download_timeout / transform_download_failed
#     The pilot failed while downloading the job's transformation script (e.g.
#     runGen) over HTTP, inside get_analysis_trf -> download_transform ->
#     download_file.  The payload never started, so any diag text mentioning
#     "payload execution" (pilot error 1310 does) is misleading.
# pilot_exception
#     An unrecognised exception raised inside pilot3 code.  Preferred over
#     payload_error, which wrongly implies the user's payload was at fault.
_EXCEPTION_ONLY_CATEGORIES: frozenset[str] = frozenset({
    "transform_download_timeout",
    "transform_download_failed",
    "pilot_exception",
})

# Map pilot error codes to a search string likely to appear near the failure
# in the log, used to anchor the context-window extraction.
#
# This table is now a *fallback*: extract_log_excerpt first looks for a real
# Python traceback (see _traceback_parse), which needs no per-code entry.  The
# table still helps for failures the pilot reports without raising an exception
# (stage-in timeouts, looping-job kills, memory limits), where there is no
# traceback to anchor on.
_PILOT_CODE_PATTERNS: dict[int, str] = {
    1099: "Failed to stage-in file",
    1104: r"work directory .* is too large",
    1150: "pilot has decided to kill looping job",
    1151: "File transfer timed out",
    1201: "caught signal",
    1235: "job has exceeded the memory limit",
    1305: "",          # payload failure — use tail of payload.stdout instead
    # 1310: exception raised while the pilot was preparing or running the
    # payload.  The metadata diag ("Exception caught during payload execution")
    # does not appear in the log; the log record reads "execute payloads caught
    # an exception".  Normally the traceback-first path handles this code, so
    # this anchor only matters for the rare 1310 job whose log lost its
    # traceback (truncated upload, killed pilot).
    1310: "caught an exception",
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
# Maximum log excerpt length sent to the LLM (characters).
# Raised from 6000: tracebacks that pass through the CVMFS standard library are
# long (the runGen transform-download timeout traceback is ~4 kB on its own),
# and a 6000-char budget left too little room for surrounding pilot context.
_MAX_EXCERPT_CHARS: int = 8000
# Characters reserved for payload.stderr within the excerpt budget.
# Guarantees the stderr traceback is always included even when stdout is long.
_STDERR_RESERVED_CHARS: int = 2000
# Characters reserved for the primary traceback within the excerpt budget.
# The traceback is the highest-value content in the excerpt, so it is allocated
# first and the surrounding context gets the remainder — never the other way
# round.
_TRACEBACK_RESERVED_CHARS: int = 5000
# Lines of log context collected immediately after the traceback.  The pilot
# usually logs the resulting error code and state transition here.
_TRACEBACK_TRAILING_LINES: int = 10
# Character cap on that trailing context, so it cannot crowd out the preceding
# context that explains what the pilot was attempting.
_TRACEBACK_TRAILING_CHARS: int = 600
# Retained for backwards compatibility only.  The stderr reservation is now
# applied by the caller that actually appends payload.stderr
# (_fetch_logs_payload), which passes the reduced budget into
# extract_failure_context.  A direct extract_log_excerpt call has no stderr to
# append and therefore gets the full budget.
#
# Char-based (not line-based) slicing is still used for the no-traceback payload
# tail: it guarantees recency regardless of line length, whereas a line count on
# verbose logs pushes the ERROR lines out of the budget.
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


# Matches an `ls -l` long-format entry, e.g.
#   -rw-r--r--. 1 atlasprd000 atlasprd 28218 Aug 17 10:37 remote_open.stderr
# and the "total 72" header that precedes such a block (with or without a pilot
# log record prefix in front of it).
#
# These lines are stripped from the context around a traceback because they are
# never diagnostic and are actively harmful: given a directory listing and no
# real error, an LLM will infer a cause from which files exist and how large
# they are.  That is exactly how job 7261310898 was misdiagnosed as a "remote
# file open failure" — the only signal in the excerpt was that
# remote_open.stderr was 28 kB.
_LISTING_LINE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[-dlbcps][rwxsStT-]{9}[.+]?\s+\d+\s+\S+\s+\S+\s+\d+\s"),
    re.compile(r"^total \d+\s*$"),
    re.compile(r"\|\s*(?:list_work_dir|print_executable)\s*\|"),
    re.compile(r"^\s*$"),
)


def _strip_directory_listing(text: str) -> str:
    """Remove directory-listing and command-echo lines from log context.

    Applied to the context lines surrounding a traceback, never to the traceback
    itself.  See :data:`_LISTING_LINE_RES` for why these lines are worse than
    merely useless.

    Args:
        text: Block of log context lines.

    Returns:
        The same block with listing lines, ``total N`` headers, pilot
        ``list_work_dir``/``print_executable`` records and blank lines removed.
    """
    kept = [
        line for line in text.splitlines()
        if not any(pattern.search(line) for pattern in _LISTING_LINE_RES)
    ]
    return "\n".join(kept)


def _trim_partial_first_line(text: str) -> str:
    """Drop a leading partial line left behind by character-based slicing.

    Slicing a block of log text to a character budget usually cuts through the
    middle of a line.  A half line of pilot log is noise at best and misleading
    at worst, so it is removed.

    Args:
        text: Text whose first line may be a fragment.

    Returns:
        *text* without its first line, or an empty string when *text* is a
        single fragment with no newline.
    """
    if "\n" not in text:
        return ""
    return text.split("\n", 1)[1]


def _build_traceback_excerpt(
    log_text: str,
    block: TracebackBlock,
    max_chars: int,
) -> str:
    """Assemble an excerpt centred on a traceback, within a character budget.

    Allocation order is deliberate: the traceback first (up to
    ``_TRACEBACK_RESERVED_CHARS``), then a short trailing window, then as much
    preceding context as the remaining budget allows.  The preceding context is
    trimmed from its *start* so the lines nearest the traceback survive.

    Args:
        log_text: Full log content the traceback was found in.
        block: The selected traceback block.
        max_chars: Total character budget for the excerpt.

    Returns:
        Excerpt string of at most *max_chars* characters, in log order
        (preceding context, traceback, trailing context).
    """
    tb_text = truncate_traceback(block.text, min(_TRACEBACK_RESERVED_CHARS, max_chars))
    remaining = max_chars - len(tb_text)
    if remaining <= 0:
        return tb_text

    lines = log_text.splitlines()

    trailing = _strip_directory_listing(
        "\n".join(lines[block.end_line:block.end_line + _TRACEBACK_TRAILING_LINES])
    )
    trailing = trailing[:min(_TRACEBACK_TRAILING_CHARS, remaining)]
    remaining -= len(trailing)

    context = ""
    if remaining > 0:
        first = max(0, block.start_line - _CONTEXT_LINES)
        context = _strip_directory_listing("\n".join(lines[first:block.start_line]))
        if len(context) > remaining:
            context = _trim_partial_first_line(context[-remaining:])

    parts = [part for part in (context, tb_text, trailing) if part]
    return "\n".join(parts)


@dataclass
class FailureContext:
    """Excerpt plus structured exception data extracted from a job's logs.

    Attributes:
        excerpt: The log excerpt to send to the LLM.
        exception: Parsed exception when a Python traceback was found in the
            log, otherwise ``None``.
        traceback_count: Number of distinct tracebacks found in the log.  A
            value above 1 means :func:`select_primary_traceback` discarded
            alternatives, which is worth surfacing in evidence.
    """

    excerpt: str = ""
    exception: ExceptionInfo | None = None
    traceback_count: int = 0


def extract_failure_context(
    log_text: str,
    log_filename: str,
    pilot_error_code: int,
    pilot_error_diag: str,
    max_chars: int = _MAX_EXCERPT_CHARS,
) -> FailureContext:
    """Extract the most relevant section of a log, plus any parsed exception.

    Traceback-first: the log is scanned for Python tracebacks and, when one is
    found, the excerpt is built around it via :func:`_build_traceback_excerpt`
    and the exception is parsed into structured form.  This path applies to
    *every* log type — ``pilotlog.txt``, ``payload.stdout``, ``payload.stderr``
    and ``setup.stdout`` — because the traceback format is identical in all of
    them and Athena payload failures benefit as much as pilot failures.

    Only when no traceback is present does the previous behaviour apply: for
    payload logs a character tail, and for ``pilotlog.txt`` the
    ``_PILOT_CODE_PATTERNS`` anchor, then ``piloterrordiag`` as a literal
    regex, then the log tail.

    Args:
        log_text: Full log content as a string.
        log_filename: Name of the log file (used to detect payload logs).
        pilot_error_code: Numeric pilot error code from job metadata.
        pilot_error_diag: Textual pilot error diagnosis from job metadata.
        max_chars: Character budget for the excerpt.  Callers that must reserve
            part of the overall budget for another file (e.g. appending
            ``payload.stderr``) pass the reduced figure here so truncation
            stays traceback-aware rather than happening via a later slice.

    Returns:
        Populated :class:`FailureContext`.  ``excerpt`` is an empty string when
        no relevant section could be found.
    """
    if not log_text:
        return FailureContext()

    is_payload = "payload" in log_filename

    if is_payload or pilot_error_code == 1305:
        # Strip pilot boilerplate (ls listings) first so neither the traceback
        # search nor the char tail spends budget on directory output.
        log_text = _strip_payload_noise(log_text)

    exception, block, count = find_primary_exception(log_text)
    if exception is not None and block is not None:
        logger.info(
            "Traceback-anchored excerpt for %s: %s (%d traceback(s) in log)",
            log_filename,
            exception.exc_type or "unparsed exception",
            count,
        )
        return FailureContext(
            excerpt=_build_traceback_excerpt(log_text, block, max_chars),
            exception=exception,
            traceback_count=count,
        )

    if is_payload or pilot_error_code == 1305:
        # Char-based tail: errors appear at the very end of payload.stdout;
        # a line-count tail on verbose logs would cut them off.
        return FailureContext(excerpt=log_text[-max_chars:])

    search_pattern = _PILOT_CODE_PATTERNS.get(pilot_error_code)
    if search_pattern is None:
        # Unknown code: use first 40 chars of piloterrordiag as pattern.
        # Unreliable (see module docstring) but retained as a last resort.
        search_pattern = re.escape(pilot_error_diag[:40]) if pilot_error_diag else ""

    excerpt = ""
    if search_pattern:
        if pilot_error_code in _TRAILING_CONTEXT_CODES:
            excerpt = _extract_context_window_with_trailing(
                log_text, search_pattern, _CONTEXT_LINES, _TRAILING_LINES
            )
        else:
            excerpt = _extract_context_window(log_text, search_pattern, _CONTEXT_LINES)

    # If no match found, fall back to the tail.
    # WARNING-level so production logs surface gaps in _PILOT_CODE_PATTERNS.
    if not excerpt:
        logger.warning(
            "No traceback found and pattern %r did not match for pilot error "
            "code %d; falling back to tail extraction. The excerpt may contain "
            "only stage-out boilerplate. Consider adding this code to "
            "_PILOT_CODE_PATTERNS.",
            search_pattern,
            pilot_error_code,
        )
        excerpt = _extract_tail(log_text, _CONTEXT_LINES)

    return FailureContext(excerpt=excerpt[:max_chars] if excerpt else "")


def extract_log_excerpt(
    log_text: str,
    log_filename: str,
    pilot_error_code: int,
    pilot_error_diag: str,
    max_chars: int = _MAX_EXCERPT_CHARS,
) -> str:
    """Extract the most relevant section of a log file for LLM analysis.

    Thin wrapper over :func:`extract_failure_context` that returns only the
    excerpt, preserving the original signature for existing callers and tests.

    Args:
        log_text: Full log content as a string.
        log_filename: Name of the log file (used to detect payload logs).
        pilot_error_code: Numeric pilot error code from job metadata.
        pilot_error_diag: Textual pilot error diagnosis from job metadata.
        max_chars: Character budget for the excerpt.

    Returns:
        Extracted context window, at most *max_chars* characters.  Empty
        string if no relevant section is found.
    """
    return extract_failure_context(
        log_text, log_filename, pilot_error_code, pilot_error_diag, max_chars,
    ).excerpt


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

# Exception types that mean "the operation did not complete in time".
_TIMEOUT_EXC_TYPES: frozenset[str] = frozenset({
    "TimeoutError", "timeout", "ReadTimeout", "ReadTimeoutError",
    "ConnectTimeout", "ConnectTimeoutError", "SocketTimeout",
})

# Exception types that mean "the network or peer failed", excluding timeouts.
_NETWORK_EXC_TYPES: frozenset[str] = frozenset({
    "ConnectionError", "ConnectionResetError", "ConnectionRefusedError",
    "ConnectionAbortedError", "BrokenPipeError", "URLError", "HTTPError",
    "SSLError", "SSLEOFError", "CertificateError", "gaierror", "herror",
})

# Pilot functions that identify the transformation-script download path:
#   get_payload_command -> get_analysis_trf -> download_transform -> download_file
# A failure anywhere under get_analysis_trf/download_transform means the payload
# command could not even be assembled, so the payload never ran.
_TRANSFORM_DOWNLOAD_FUNCS: frozenset[str] = frozenset({
    "get_analysis_trf", "download_transform",
})

# Signals for the pilot CPU-monitoring UID lookup failure (pilot error 1354).
# Preserved as its own category because bamboo_answer and planner routing
# reference "pilot_monitoring_error" by name.
_MONITORING_FUNCS: frozenset[str] = frozenset({
    "list_processes_and_threads", "get_process_info",
})
_MONITORING_MESSAGE_SIGNALS: tuple[str, ...] = ("getpwuid", "uid not found")


# Keywords for the metadata-only pre-check in classify_failure.  Kept in sync
# with the "reassigned_by_jedi" entry of _FAILURE_PATTERNS.
_REASSIGNED_KEYWORDS: tuple[str, ...] = ("reassigned by jedi", "toreassign")


def _build_search_text(job: dict[str, Any], log_excerpt: str) -> str:
    """Build the lower-cased search string used by substring classification.

    Args:
        job: The ``job`` dict from the BigPanDA metadata response.
        log_excerpt: Extracted context window, or an empty string to search
            job metadata only.

    Returns:
        Single lower-cased string joining the error diagnosis fields and the
        excerpt.
    """
    return " ".join([
        str(job.get("taskbuffererrordiag") or ""),
        str(job.get("piloterrordiag") or ""),
        str(job.get("exeerrordiag") or ""),
        str(job.get("jobsubstatus") or ""),
        str(job.get("commandtopilot") or ""),
        log_excerpt,
    ]).lower()


def _classify_from_exception(exception: ExceptionInfo) -> str | None:
    """Classify a failure from a parsed exception rather than substring search.

    Preferred over :data:`_FAILURE_PATTERNS` because it reasons about the
    exception type and the pilot call chain instead of matching substrings
    anywhere in the excerpt.  Substring matching over a low-quality excerpt
    produces confident nonsense: job 7261310898 was classified ``"timeout"``
    because the excerpt happened to contain ``"using timeout=90 s"`` from the
    pilot's log-archiving ``tar`` command, not because anything timed out.

    Args:
        exception: Parsed exception from :mod:`askpanda_atlas._traceback_parse`.

    Returns:
        A failure category string, or ``None`` when the exception is not
        recognised *and* contains no pilot frames — in which case the caller
        should fall through to the substring table (payload tracebacks are
        classified there as ``payload_error``).
    """
    exc_type = exception.exc_type
    message = exception.message.lower()
    funcs = {frame.func for frame in exception.pilot_frames}
    paths = {frame.pilot_path for frame in exception.pilot_frames}

    is_timeout = exc_type in _TIMEOUT_EXC_TYPES or "timed out" in message
    in_transform_download = bool(funcs & _TRANSFORM_DOWNLOAD_FUNCS)

    # Order matters: the transform-download checks are more specific than the
    # bare timeout/network checks and must win.
    if in_transform_download:
        return "transform_download_timeout" if is_timeout else "transform_download_failed"

    if funcs & _MONITORING_FUNCS or any(sig in message for sig in _MONITORING_MESSAGE_SIGNALS):
        return "pilot_monitoring_error"
    if any(path.endswith("psutils.py") for path in paths):
        return "pilot_monitoring_error"

    if exc_type == "MemoryError":
        return "memory"
    if "no space left" in message or "disk quota" in message:
        return "disk_full"
    if exc_type in ("FileNotFoundError", "IsADirectoryError"):
        return "input_missing"
    if is_timeout:
        return "timeout"
    if exc_type in _NETWORK_EXC_TYPES:
        return "network"

    # Unrecognised exception, but it was raised inside pilot code: report it as
    # a pilot exception rather than letting the substring table label it
    # "payload_error", which wrongly blames the user's payload.
    if exception.pilot_frames:
        return "pilot_exception"

    return None


def classify_failure(
    job: dict[str, Any],
    log_excerpt: str,
    exception: ExceptionInfo | None = None,
) -> str:
    """Classify a job failure from job metadata, log excerpt and exception.

    When *exception* is supplied, :func:`_classify_from_exception` is consulted
    first; it is far more reliable than substring matching.  The
    ``_FAILURE_PATTERNS`` table is used only when no exception was parsed or
    the exception is unrecognised and originated outside pilot code.

    Args:
        job: The ``job`` dict from the BigPanDA metadata response.
        log_excerpt: Extracted context window from the pilot log.
        exception: Parsed exception from the log, when one was found.

    Returns:
        A short failure category string (e.g. ``"stagein_timeout"``).
        Falls back to ``"unknown"`` if nothing matches.
    """
    # Metadata-level signals that outrank any log content: a job reassigned by
    # JEDI never really "failed" in the pilot, so an exception in its log (if
    # any) is incidental.
    metadata_search = _build_search_text(job, "")
    if any(kw in metadata_search for kw in _REASSIGNED_KEYWORDS):
        return "reassigned_by_jedi"

    if exception is not None:
        category = _classify_from_exception(exception)
        if category is not None:
            return category

    search = _build_search_text(job, log_excerpt)
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
        exception: Parsed exception when a Python traceback was found in any of
            the fetched logs, otherwise ``None``.
        traceback_count: Number of distinct tracebacks found in the log the
            exception came from.
        pilot_version: Pilot release version parsed from the pilot log (e.g.
            ``"3.14.0.22"``), or an empty string when unavailable.  Used to pin
            GitHub source fetches to the tag the job actually ran.
    """

    log_excerpt: str = ""
    log_url: str | None = None
    log_available: bool = False
    stderr_url: str | None = None
    setup_log_url: str | None = None
    setup_log_excerpt: str | None = field(default=None)
    exception: ExceptionInfo | None = None
    traceback_count: int = 0
    pilot_version: str = ""


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
                setup_ctx = extract_failure_context(
                    setup_text, "setup.stdout", pilot_error_code,
                    pilot_error_diag, _MAX_EXCERPT_CHARS,
                )
                # Setup errors are usually shell output rather than tracebacks;
                # when no traceback is present keep the whole (capped) file so
                # the asetup/release diagnostics are not cropped by an anchor.
                result.setup_log_excerpt = (
                    setup_ctx.excerpt if setup_ctx.exception
                    else setup_text[:_MAX_EXCERPT_CHARS]
                )
                result.log_excerpt = result.setup_log_excerpt
                result.exception = setup_ctx.exception
                result.traceback_count = setup_ctx.traceback_count
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
        # Pass the reduced budget into extraction rather than slicing the
        # result afterwards: a post-hoc slice can cut a traceback and discard
        # the terminal exception line, which is the whole point of the excerpt.
        stdout_budget = _MAX_EXCERPT_CHARS - _STDERR_RESERVED_CHARS
        stdout_ctx = extract_failure_context(
            log_text or "", log_filename,
            pilot_error_code, pilot_error_diag, stdout_budget,
        )
        stderr_ctx = FailureContext()
        if stderr_text:
            stderr_ctx = extract_failure_context(
                stderr_text, "payload.stderr",
                pilot_error_code, pilot_error_diag, _STDERR_RESERVED_CHARS,
            )
            result.log_excerpt = (
                stdout_ctx.excerpt
                + "\n\n--- payload.stderr ---\n"
                + stderr_ctx.excerpt
            )
        else:
            result.log_excerpt = stdout_ctx.excerpt

        # Prefer the stderr traceback: Python tracebacks and segfault reports
        # are written to stderr, so when both files contain one, stderr holds
        # the exception that actually terminated the payload.
        chosen = stderr_ctx if stderr_ctx.exception else stdout_ctx
        result.exception = chosen.exception
        result.traceback_count = chosen.traceback_count
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
        context = extract_failure_context(
            log_text, log_filename, pilot_error_code, pilot_error_diag,
            _MAX_EXCERPT_CHARS,
        )
        result.log_excerpt = context.excerpt
        result.exception = context.exception
        result.traceback_count = context.traceback_count
        # Parse the version from the *full* log: the pilot reports it during
        # start-up, so an excerpt taken from the failure point will not have it.
        result.pilot_version = parse_pilot_version(log_text)
    else:
        logger.info(
            "Log unavailable for job %d; proceeding with metadata only.", job_id
        )

    return result


def _build_exception_evidence(
    exception: ExceptionInfo | None,
    traceback_count: int,
) -> dict[str, Any]:
    """Build the exception-related evidence keys.

    Promoting the exception to first-class evidence keys means the synthesis LLM
    does not have to locate it inside ``log_excerpt``, and gives the follow-up
    ``pilot_source_analysis`` route a reliable signal to gate on.

    Args:
        exception: Parsed exception, or ``None`` when the log had no traceback.
        traceback_count: Number of tracebacks found in the log.

    Returns:
        Dict of evidence keys.  All keys are always present (``None``/``False``
        when there is no exception) so downstream consumers can rely on the
        shape rather than probing with ``in``.
    """
    if exception is None:
        return {
            "traceback_available": False,
            "exception_type": None,
            "exception_message": None,
            "exception_frames": None,
            "deepest_pilot_frame": None,
            "traceback_count": 0,
        }

    parsed = exception.as_dict()
    return {
        "traceback_available": True,
        "exception_type": parsed["type"] or None,
        "exception_message": parsed["message"] or None,
        "exception_frames": parsed["frames"],
        "deepest_pilot_frame": parsed["deepest_pilot_frame"],
        "traceback_count": traceback_count,
    }


def _build_code_analysis_offer(exception: ExceptionInfo | None) -> str:
    """Build the Markdown follow-up offer for pilot source code analysis.

    Args:
        exception: Parsed exception, or ``None`` when the log had no traceback.

    Returns:
        Markdown string naming the pilot frame the traceback originates in and
        inviting a source-level follow-up, or an empty string when there is no
        pilot frame to analyse.
    """
    if exception is None:
        return ""
    deepest = exception.deepest_pilot_frame
    if deepest is None:
        return ""
    return (
        f"\n\nThe exception was raised in pilot code at "
        f"`{deepest.pilot_path}:{deepest.lineno}` (`{deepest.func}`). "
        f"Ask me to show the pilot source for a code-level diagnosis."
    )


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
    exception = fetch_result.exception

    # Fall back to the pilotid metadata field when the pilot log was not
    # downloaded (e.g. pilot error 1305 reads payload.stdout instead).
    pilot_version = fetch_result.pilot_version or parse_pilot_version_from_pilotid(
        str(job.get("pilotid") or "")
    )

    # --- Step 3: Classify failure ---
    failure_type = classify_failure(job, log_excerpt, exception)

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
        "pilot_version": pilot_version or None,
    }

    evidence.update(_build_exception_evidence(exception, fetch_result.traceback_count))

    summary = f"Job {job_id} ({jobstatus}): failure type '{failure_type}'."
    if exception is not None and exception.exc_type:
        # The parsed exception is more trustworthy than piloterrordiag, which is
        # a summary written elsewhere in the pilot and can contradict the log.
        summary += f" Exception: {exception.exc_type}: {exception.message[:120]}."
        deepest = exception.deepest_pilot_frame
        if deepest is not None:
            summary += (
                f" Raised in pilot code at {deepest.pilot_path}:{deepest.lineno}"
                f" ({deepest.func})."
            )
    elif job.get("taskbuffererrordiag"):
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

    # Deterministic follow-up offer, appended verbatim by bamboo_executor so the
    # LLM cannot garble the frame reference.  Only offered when the traceback
    # actually reaches pilot3 code, since pilot_source_analysis has nothing to
    # fetch for a pure payload traceback.
    evidence["code_analysis_offer_md"] = _build_code_analysis_offer(exception)

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
    "FailureContext",
    "PandaLogAnalysisTool",
    "classify_failure",
    "extract_failure_context",
    "extract_log_excerpt",
    "fetch_and_analyse",
    "get_definition",
    "panda_log_analysis_tool",
    "_build_code_analysis_offer",
    "_build_exception_evidence",
    "_classify_from_exception",
    "_fetch_file_index",
    "_file_is_nonempty",
    "_setup_log_has_error",
]
