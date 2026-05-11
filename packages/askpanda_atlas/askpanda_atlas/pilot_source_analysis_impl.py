"""ATLAS PanDA pilot source analysis tool — canonical implementation.

Given a ``pilot_monitoring_error`` evidence dict produced by
:mod:`askpanda_atlas.log_analysis_impl`, this tool:

1. Parses the Python traceback in ``log_excerpt`` to extract the unique
   pilot3 source files and function names involved.
2. Fetches only those modules from the PanDAWMS/pilot3 GitHub repository
   (raw content API — no clone, no checkout).
3. Extracts the specific functions named in the traceback from each module
   using the ``ast`` module (accurate, handles decorators and nested defs).
4. Returns structured evidence containing the extracted source snippets and
   the original exception, ready for LLM synthesis.

The tool is intentionally data-driven: it never hardcodes ``psutils.py`` or
``list_processes_and_threads``.  All file paths and function names come
directly from the traceback, so it handles any future ``pilot_monitoring_error``
without code changes.

Interface
---------
- ``pilot_source_analysis_tool.get_definition()`` — MCP tool definition
- ``await pilot_source_analysis_tool.call(arguments)`` — returns
  ``list[MCPContent]`` whose ``text`` field is a JSON-serialised dict
  with ``evidence`` and ``text`` keys.

Evidence keys
-------------
job_id, exception, traceback_frames, source_snippets, github_base_url,
fetch_errors, files_fetched.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import textwrap
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_RAW_BASE: str = (
    "https://raw.githubusercontent.com/PanDAWMS/pilot3/master"
)

# Matches lines of the form:
#   File "/tmp/atlas_xxx/pilot3/pilot/util/psutils.py", line 428, in list_processes_and_threads
_FRAME_RE: re.Pattern[str] = re.compile(
    r'File\s+"[^"]*?(?P<pilot_path>pilot/[^"]+\.py)",\s+line\s+\d+,\s+in\s+(?P<func>\S+)'
)

# Maximum characters per extracted function body sent to the LLM.
_MAX_FUNC_CHARS: int = 4000

# HTTP timeout for each GitHub raw fetch (seconds).
_FETCH_TIMEOUT: int = 15


# ---------------------------------------------------------------------------
# Traceback parsing
# ---------------------------------------------------------------------------

def parse_traceback_frames(log_excerpt: str) -> list[dict[str, str]]:
    """Extract pilot3 file paths and function names from a traceback.

    Scans ``log_excerpt`` for Python traceback ``File "..."`` lines that
    reference a path containing ``pilot/``.  Returns one entry per unique
    ``(pilot_path, function)`` pair in the order they appear, preserving
    the call-chain context.

    Args:
        log_excerpt: Log text that may contain a Python traceback.

    Returns:
        List of dicts with ``pilot_path`` (e.g. ``"pilot/util/psutils.py"``)
        and ``func`` (e.g. ``"list_processes_and_threads"``).  Empty if no
        pilot3 frames are found.
    """
    seen: set[tuple[str, str]] = set()
    frames: list[dict[str, str]] = []
    for m in _FRAME_RE.finditer(log_excerpt):
        key = (m.group("pilot_path"), m.group("func"))
        if key not in seen:
            seen.add(key)
            frames.append({"pilot_path": m.group("pilot_path"), "func": m.group("func")})
    return frames


def parse_exception_line(log_excerpt: str) -> str:
    """Extract the exception line from a log excerpt.

    Looks for the last ``KeyError:``, ``ValueError:``, ``Exception caught:``
    etc. line that is not a generic ``WARNING`` preamble.

    Args:
        log_excerpt: Log text containing the exception.

    Returns:
        The exception string, or an empty string if none found.
    """
    # Prefer the bare "ExceptionType: message" line at the end of a traceback
    exc_re = re.compile(
        r"^(?:[A-Za-z][A-Za-z0-9_]*Error|KeyError|ValueError|RuntimeError"
        r"|Exception|OSError|IOError|AttributeError|TypeError)\s*:.*$",
        re.MULTILINE,
    )
    matches = exc_re.findall(log_excerpt)
    if matches:
        return matches[-1].strip()

    # Fall back to the pilot "Exception caught:" WARNING line
    caught_re = re.compile(
        r"Exception caught:\s*(.+?)(?:\s*$)", re.MULTILINE
    )
    m = caught_re.search(log_excerpt)
    if m:
        return m.group(1).strip().strip("'\"")

    return ""


# ---------------------------------------------------------------------------
# GitHub source fetching
# ---------------------------------------------------------------------------

def _fetch_raw(url: str, timeout: int) -> tuple[int, str | None]:
    """Fetch a URL and return (status_code, text_or_None).

    Args:
        url: URL to fetch.
        timeout: HTTP timeout in seconds.

    Returns:
        Tuple of HTTP status code and response text (or ``None`` on error).
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("HTTP fetch error for %s: %s", url, exc)
        return 0, None


def fetch_pilot_module(pilot_path: str, timeout: int) -> tuple[str | None, str]:
    """Download a single pilot3 source module from GitHub.

    Args:
        pilot_path: Relative path within the pilot3 repo, e.g.
            ``"pilot/util/psutils.py"``.
        timeout: HTTP timeout in seconds.

    Returns:
        Tuple of (source_text_or_None, error_message).  ``error_message``
        is an empty string on success.
    """
    url = f"{_GITHUB_RAW_BASE}/{pilot_path}"
    status, text = _fetch_raw(url, timeout)
    if text is None:
        return None, f"HTTP {status} fetching {url}"
    return text, ""


# ---------------------------------------------------------------------------
# AST-based function extraction
# ---------------------------------------------------------------------------

def extract_function_source(source: str, func_name: str) -> str | None:
    """Extract a top-level or class-method function body from Python source.

    Uses the ``ast`` module to locate the function definition, then slices
    the source lines to return the exact source text including decorators.

    Args:
        source: Full Python source text of the module.
        func_name: Name of the function to extract (e.g.
            ``"list_processes_and_threads"``).

    Returns:
        Source text of the function (dedented), or ``None`` if not found or
        the source cannot be parsed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    source_lines = source.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                # Include decorator lines above the def
                start = node.decorator_list[0].lineno - 1 if node.decorator_list else node.lineno - 1
                # end_lineno available from Python 3.8+
                end = getattr(node, "end_lineno", None)
                if end is None:
                    # Fallback: take 200 lines from start
                    snippet_lines = source_lines[start:start + 200]
                else:
                    snippet_lines = source_lines[start:end]
                return textwrap.dedent("\n".join(snippet_lines))

    return None


# ---------------------------------------------------------------------------
# Core analysis function
# ---------------------------------------------------------------------------

def fetch_and_analyse_pilot_source(
    job_id: int,
    log_excerpt: str,
    pilot_error_diag: str,
    timeout: int = _FETCH_TIMEOUT,
) -> dict[str, Any]:
    """Parse the traceback, fetch relevant pilot modules, extract functions.

    Orchestrates the full analysis:
    1. Parse traceback frames from the log excerpt.
    2. Group unique file paths to minimise HTTP requests.
    3. Fetch each unique module from GitHub.
    4. Extract each function named in the traceback from its module.
    5. Return structured evidence.

    Args:
        job_id: PanDA job ID (for evidence labelling).
        log_excerpt: Log excerpt from a ``pilot_monitoring_error`` job.
        pilot_error_diag: ``piloterrordiag`` string from job metadata.
        timeout: HTTP timeout per GitHub fetch.

    Returns:
        Dict with ``evidence`` and ``text`` keys.
    """
    frames = parse_traceback_frames(log_excerpt)
    exception_str = parse_exception_line(log_excerpt) or pilot_error_diag

    if not frames:
        return {
            "evidence": {
                "job_id": job_id,
                "exception": exception_str,
                "error": (
                    "No pilot3 traceback frames found in log_excerpt. "
                    "Ensure the log excerpt contains a Python traceback with "
                    "File lines referencing paths under pilot/."
                ),
            },
            "text": (
                f"Job {job_id}: could not find pilot3 traceback frames in the "
                "provided log excerpt."
            ),
        }

    # Fetch each unique pilot source file once.
    unique_paths = list(dict.fromkeys(f["pilot_path"] for f in frames))
    source_cache: dict[str, str | None] = {}
    fetch_errors: dict[str, str] = {}

    for path in unique_paths:
        src, err = fetch_pilot_module(path, timeout)
        if err:
            fetch_errors[path] = err
            source_cache[path] = None
        else:
            source_cache[path] = src

    # Extract each named function from its module.
    source_snippets: dict[str, str] = {}
    missing_funcs: list[str] = []

    for frame in frames:
        path = frame["pilot_path"]
        func = frame["func"]
        key = f"{path}::{func}"
        if key in source_snippets:
            continue  # already extracted

        src = source_cache.get(path)
        if src is None:
            missing_funcs.append(key)
            continue

        extracted = extract_function_source(src, func)
        if extracted:
            source_snippets[key] = extracted[:_MAX_FUNC_CHARS]
        else:
            missing_funcs.append(key)
            logger.warning(
                "Function %r not found in %s (may be a lambda or inner function).",
                func,
                path,
            )

    files_fetched = [p for p in unique_paths if source_cache.get(p) is not None]
    github_urls = {
        p: f"https://github.com/PanDAWMS/pilot3/blob/master/{p}"
        for p in unique_paths
    }

    evidence: dict[str, Any] = {
        "job_id": job_id,
        "exception": exception_str,
        "traceback_frames": frames,
        "source_snippets": source_snippets,
        "github_base_url": _GITHUB_RAW_BASE,
        "github_urls": github_urls,
        "files_fetched": files_fetched,
        "missing_functions": missing_funcs,
        "fetch_errors": fetch_errors,
    }

    n_snippets = len(source_snippets)
    n_frames = len(frames)
    summary = (
        f"Job {job_id}: fetched {len(files_fetched)} pilot3 module(s), "
        f"extracted {n_snippets}/{n_frames} function(s) from the traceback."
    )
    if fetch_errors:
        summary += f" Fetch errors: {list(fetch_errors.keys())}."
    if missing_funcs:
        summary += f" Functions not found in source: {missing_funcs}."

    return {"evidence": evidence, "text": summary}


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for pilot_source_analysis.

    Returns:
        Dict with ``name``, ``description``, ``inputSchema``,
        ``examples``, and ``tags`` keys.
    """
    return {
        "name": "pilot_source_analysis",
        "description": (
            "Deep-dive into a pilot_monitoring_error by fetching the relevant "
            "pilot3 source modules from GitHub and extracting the exact "
            "functions named in the exception traceback. "
            "Use ONLY after panda_log_analysis has returned "
            "failure_type='pilot_monitoring_error' and the user wants to "
            "understand why the pilot code raised the exception or how the "
            "affected function could be improved. "
            "Requires the log_excerpt from the prior panda_log_analysis call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": "PanDA job ID (pandaid) — used for evidence labelling.",
                },
                "log_excerpt": {
                    "type": "string",
                    "description": (
                        "Log excerpt from panda_log_analysis evidence containing "
                        "the Python traceback (evidence.log_excerpt)."
                    ),
                },
                "pilot_error_diag": {
                    "type": "string",
                    "description": (
                        "piloterrordiag string from job metadata "
                        "(evidence.piloterrordiag). Used as fallback exception "
                        "description if the traceback cannot be parsed."
                    ),
                },
            },
            "required": ["job_id", "log_excerpt"],
            "additionalProperties": False,
        },
        "examples": [
            {
                "job_id": 7099503721,
                "log_excerpt": (
                    "WARNING | Exception caught: 'getpwuid(): uid not found: 6435'\n"
                    "WARNING | Traceback (most recent call last):\n"
                    "  File \"/tmp/atlas_8GX3ynDr/pilot3/pilot/util/psutils.py\","
                    " line 428, in list_processes_and_threads\n"
                    "    current_user = getpass.getuser()\n"
                    "KeyError: 'getpwuid(): uid not found: 6435'"
                ),
                "pilot_error_diag": "Exception caught: 'getpwuid(): uid not found: 6435'",
            }
        ],
        "tags": [
            "atlas", "panda", "pilot", "pilot3", "source",
            "monitoring", "exception", "github",
        ],
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class PilotSourceAnalysisTool:
    """MCP tool for fetching and analysing pilot3 source involved in errors.

    Parses a Python traceback from a ``pilot_monitoring_error`` log excerpt,
    fetches the relevant pilot3 modules from GitHub, extracts the named
    functions, and returns structured evidence for LLM synthesis.
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
        """Fetch pilot source and return structured analysis.

        All blocking HTTP calls are offloaded to a thread pool via
        ``asyncio.to_thread`` so the async event loop is not blocked.

        Args:
            arguments: Dict with required ``job_id`` (int) and
                ``log_excerpt`` (str), plus optional ``pilot_error_diag``
                (str).

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence dict.
        """
        from bamboo.tools.base import text_content  # deferred — no bamboo dep at import time

        def _err(payload: dict[str, Any]) -> list[Any]:
            return text_content(json.dumps(payload))

        if not isinstance(arguments, dict):
            return _err({"evidence": {"error": "arguments must be a dict"}})

        job_id = arguments.get("job_id")
        if job_id is None:
            return _err({"evidence": {"error": "missing job_id"}})
        try:
            job_id_int = int(job_id)
        except (ValueError, TypeError):
            return _err({"evidence": {"error": "job_id must be an integer"}})

        log_excerpt = str(arguments.get("log_excerpt") or "")
        if not log_excerpt:
            return _err({
                "evidence": {
                    "job_id": job_id_int,
                    "error": (
                        "log_excerpt is required. Pass evidence.log_excerpt "
                        "from the prior panda_log_analysis call."
                    ),
                }
            })

        pilot_error_diag = str(arguments.get("pilot_error_diag") or "")

        timeout: int = _FETCH_TIMEOUT
        try:
            timeout = int(arguments.get("timeout") or _FETCH_TIMEOUT)
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
            logger.exception("Unexpected error in pilot_source_analysis for job %d", job_id_int)
            return _err({
                "evidence": {
                    "job_id": job_id_int,
                    "error": repr(exc),
                },
                "text": f"Unexpected error analysing pilot source for job {job_id_int}: {exc}",
            })

        return text_content(json.dumps(result))


pilot_source_analysis_tool = PilotSourceAnalysisTool()

__all__ = [
    "PilotSourceAnalysisTool",
    "fetch_and_analyse_pilot_source",
    "get_definition",
    "parse_exception_line",
    "parse_traceback_frames",
    "extract_function_source",
    "fetch_pilot_module",
    "pilot_source_analysis_tool",
]
