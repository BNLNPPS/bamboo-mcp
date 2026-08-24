"""ATLAS PanDA pilot source analysis tool — canonical implementation.

Given a job failure whose log contained a Python traceback reaching into
pilot3 code (detected by :mod:`askpanda_atlas.log_analysis_impl`), this tool:

1. Parses the traceback in ``log_excerpt`` to extract the pilot3 source files,
   function names and line numbers involved.
2. Resolves the pilot3 Git ref to fetch: the release tag matching the pilot
   version that ran the job when known, falling back to ``master``.
3. Fetches only those modules from the PanDAWMS/pilot3 GitHub repository
   (raw content API — no clone, no checkout).
4. Extracts the specific functions named in the traceback from each module
   using the ``ast`` module (accurate, handles decorators and nested defs).
5. Verifies that the function found at each traceback line number is the
   function the traceback named, flagging version skew when it is not.
6. Returns structured evidence containing the extracted source snippets and
   the original exception, ready for LLM synthesis.

The tool is intentionally data-driven: it never hardcodes ``psutils.py`` or
``list_processes_and_threads``.  All file paths, function names and line
numbers come directly from the traceback, so it handles any pilot exception
without code changes.

Source selection
----------------
A job's traceback line numbers are only meaningful against the source the job
actually ran, and released and unreleased pilots live in different places:

- **Released builds** are tagged in ``PanDAWMS/pilot3`` after release (e.g. tag
  ``3.14.0.22``).  When a tag matching ``pilot_version`` exists it is used, and
  line numbers correspond exactly.
- **Unreleased builds** have no tag; their code lives on the ``next`` branch of
  the pilot developer's fork (``PalNilsson/pilot3``).  Such a job never ran
  ``master``, so ``master`` is never consulted for it.  ``next`` is a moving
  branch, so line numbers are indicative only.

The two paths are mutually exclusive: a tagged version never reads ``next``, and
an untagged version never reads ``master``.  ``master`` is used only when
``pilot_version`` is unknown, since the build then cannot be classified at all.

``pilot_version`` is resolved from the pilot log (or the ``pilotid`` metadata
field) by ``log_analysis_impl`` and passed through to this tool.  The outcome is
reported in ``ref_kind`` and ``ref_resolution`` so the synthesis prompt can
caveat line references deterministically.

Interface
---------
- ``pilot_source_analysis_tool.get_definition()`` — MCP tool definition
- ``await pilot_source_analysis_tool.call(arguments)`` — returns
  ``list[MCPContent]`` whose ``text`` field is a JSON-serialised dict
  with ``evidence`` and ``text`` keys.

Evidence keys
-------------
job_id, exception, exception_type, exception_message, traceback_frames,
source_snippets, github_base_url, github_urls, files_fetched,
missing_functions, fetch_errors, pilot_version, github_repo, github_ref,
ref_kind, ref_resolution, line_verification.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from askpanda_atlas._traceback_parse import (
    find_traceback_blocks,
    parse_exception,
    parse_frames,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_RAW_ROOT: str = "https://raw.githubusercontent.com"
_GITHUB_BROWSE_ROOT: str = "https://github.com"

# Released pilots: tagged in the PanDAWMS repository after release.  ``master``
# carries released code and is used only when the pilot version is unknown.
_RELEASE_REPO: str = "PanDAWMS/pilot3"
_RELEASE_BRANCH: str = "master"

# Unreleased pilots: development happens on the ``next`` branch of the pilot
# developer's fork.  A version with no release tag ran code from here, so this
# is the only correct source for such a job — never ``master``.
_DEV_REPO: str = "PalNilsson/pilot3"
_DEV_BRANCH: str = "next"

# Overridable via BAMBOO_PILOT3_REPO / BAMBOO_PILOT3_BRANCH /
# BAMBOO_PILOT3_DEV_REPO / BAMBOO_PILOT3_DEV_BRANCH, following the
# BAMBOO_CODE_QUERY_* convention in bamboo.tools.code_query.  The development
# fork is expected to move to a BNLNPPS organisation eventually; the env var
# means that will not require a code change.

# Backwards-compatible alias: the fixed master-pinned raw base some callers and
# tests referenced.  New code should use _raw_url()/_browse_url().
_DEFAULT_REF: str = _RELEASE_BRANCH
_GITHUB_RAW_BASE: str = f"{_GITHUB_RAW_ROOT}/{_RELEASE_REPO}/{_RELEASE_BRANCH}"

# Matches lines of the form:
#   File "/tmp/atlas_xxx/pilot3/pilot/util/psutils.py", line 428, in list_processes_and_threads
# Retained for backwards compatibility; frame extraction now goes through
# askpanda_atlas._traceback_parse.parse_frames, which also captures line numbers
# and non-pilot frames.
_FRAME_RE: re.Pattern[str] = re.compile(
    r'File\s+"[^"]*?(?P<pilot_path>pilot/[^"]+\.py)",\s+line\s+\d+,\s+in\s+(?P<func>\S+)'
)

# Maximum characters per extracted function body sent to the LLM.
_MAX_FUNC_CHARS: int = 4000

# HTTP timeout for each GitHub raw fetch (seconds).
_FETCH_TIMEOUT: int = 15


def _raw_url(repo: str, ref: str, pilot_path: str) -> str:
    """Build the raw.githubusercontent.com URL for a pilot3 module.

    Args:
        repo: GitHub ``owner/name``, e.g. ``"PanDAWMS/pilot3"``.
        ref: Git ref to fetch (release tag or branch name).
        pilot_path: Repo-relative path, e.g. ``"pilot/util/https.py"``.

    Returns:
        Fully qualified raw content URL.
    """
    return f"{_GITHUB_RAW_ROOT}/{repo}/{ref}/{pilot_path}"


def _browse_url(repo: str, ref: str, pilot_path: str) -> str:
    """Build the github.com browse URL for a pilot3 module.

    Args:
        repo: GitHub ``owner/name``, e.g. ``"PanDAWMS/pilot3"``.
        ref: Git ref to link to (release tag or branch name).
        pilot_path: Repo-relative path, e.g. ``"pilot/util/https.py"``.

    Returns:
        Fully qualified GitHub blob URL for developers to follow.
    """
    return f"{_GITHUB_BROWSE_ROOT}/{repo}/blob/{ref}/{pilot_path}"


# ---------------------------------------------------------------------------
# Traceback parsing
# ---------------------------------------------------------------------------

def parse_traceback_frames(log_excerpt: str) -> list[dict[str, str]]:
    """Extract pilot3 file paths and function names from a traceback.

    Thin wrapper over :func:`askpanda_atlas._traceback_parse.parse_frames`,
    filtered to pilot3 frames and de-duplicated on ``(pilot_path, func)`` so
    each module is fetched and each function extracted only once.  The line
    number of the *first* occurrence is retained so callers can verify it
    against the fetched source.

    Args:
        log_excerpt: Log text that may contain a Python traceback.

    Returns:
        List of dicts with ``pilot_path`` (e.g. ``"pilot/util/psutils.py"``),
        ``func`` (e.g. ``"list_processes_and_threads"``) and ``lineno`` (as an
        ``int``).  Empty if no pilot3 frames are found.
    """
    seen: set[tuple[str, str]] = set()
    frames: list[dict[str, Any]] = []
    for frame in parse_frames(log_excerpt):
        if not frame.is_pilot:
            continue
        key = (frame.pilot_path, frame.func)
        if key in seen:
            continue
        seen.add(key)
        frames.append({
            "pilot_path": frame.pilot_path,
            "func": frame.func,
            "lineno": frame.lineno,
        })
    return frames


def parse_exception_line(log_excerpt: str) -> str:
    """Extract the exception line from a log excerpt.

    Returns the terminal ``ExceptionType: message`` line of the traceback when
    one is present.  Falls back to the pilot's ``Exception caught:`` WARNING
    line for excerpts that contain the pilot's own summary but no traceback.

    Args:
        log_excerpt: Log text containing the exception.

    Returns:
        The exception string (e.g. ``"TimeoutError: timed out"``), or an empty
        string if none found.
    """
    blocks = find_traceback_blocks(log_excerpt)
    if blocks:
        info = parse_exception(blocks[-1].text, blocks[-1].level)
        if info.exc_type_full:
            return (
                f"{info.exc_type_full}: {info.message}"
                if info.message else info.exc_type_full
            )

    # Fall back to the pilot "Exception caught:" WARNING line
    caught_re = re.compile(r"Exception caught:\s*(.+?)(?:\s*$)", re.MULTILINE)
    match = caught_re.search(log_excerpt)
    if match:
        return match.group(1).strip().strip("'\"")

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


def fetch_pilot_module(
    pilot_path: str,
    timeout: int,
    ref: str = _DEFAULT_REF,
    repo: str = _RELEASE_REPO,
) -> tuple[str | None, str]:
    """Download a single pilot3 source module from GitHub.

    Args:
        pilot_path: Relative path within the pilot3 repo, e.g.
            ``"pilot/util/psutils.py"``.
        timeout: HTTP timeout in seconds.
        ref: Git ref to fetch from — a release tag such as ``"3.14.0.22"``, or
            a branch such as ``"next"``.
        repo: GitHub ``owner/name`` to fetch from.

    Returns:
        Tuple of (source_text_or_None, error_message).  ``error_message``
        is an empty string on success.
    """
    url = _raw_url(repo, ref, pilot_path)
    status, text = _fetch_raw(url, timeout)
    if text is None:
        return None, f"HTTP {status} fetching {url}"
    return text, ""


@dataclass(frozen=True)
class SourceRef:
    """The pilot3 repository and Git ref chosen to read source from.

    Attributes:
        repo: GitHub ``owner/name`` of the repository to read.
        ref: Git ref within *repo* — a release tag, or a branch name.
        kind: One of ``"release_tag"``, ``"development_branch"`` or
            ``"unknown_version"``.  Consumed by the synthesis prompt so it can
            caveat line-number references deterministically rather than by
            parsing prose.
        resolution: Human-readable explanation of why this repo/ref was chosen.
        probe_text: Source text of the probed file, when the probe succeeded.
            Reused to seed the source cache so the file is not fetched twice.
        reachable: ``True`` when a candidate was confirmed fetchable.
    """

    repo: str
    ref: str
    kind: str
    resolution: str
    probe_text: str | None = None
    reachable: bool = False


def _candidate_refs(pilot_version: str) -> list[tuple[str, str, str]]:
    """Build the ordered list of (repo, ref, kind) candidates to probe.

    Released and unreleased pilots live in different places and the two paths
    are mutually exclusive:

    - A version with a matching release tag is a released build.  Its source is
      the tag in the release repository.  The development branch is never
      consulted, because the job did not run development code.
    - A version with no matching tag is an unreleased build.  Its source is the
      development branch of the pilot developer's fork.  ``master`` is never
      consulted, because ``master`` carries released code that by definition is
      not what this job ran.
    - When the version is unknown the build cannot be classified at all, so the
      release branch is read as the least-wrong generic choice — reaching into
      the development fork on a guess would be worse.

    Args:
        pilot_version: Pilot version string, or an empty string when unknown.

    Returns:
        Ordered candidates as ``(repo, ref, kind)`` tuples.
    """
    release_repo = os.getenv("BAMBOO_PILOT3_REPO", _RELEASE_REPO)
    if not pilot_version:
        return [(
            release_repo,
            os.getenv("BAMBOO_PILOT3_BRANCH", _RELEASE_BRANCH),
            "unknown_version",
        )]

    dev_repo = os.getenv("BAMBOO_PILOT3_DEV_REPO", _DEV_REPO)
    dev_branch = os.getenv("BAMBOO_PILOT3_DEV_BRANCH", _DEV_BRANCH)
    return [
        # Tagging conventions vary, so both the bare and v-prefixed forms are
        # tried.  PanDAWMS/pilot3 uses the bare form (tag "3.14.0.22").
        (release_repo, pilot_version, "release_tag"),
        (release_repo, f"v{pilot_version}", "release_tag"),
        (dev_repo, dev_branch, "development_branch"),
    ]


def _describe_resolution(
    repo: str,
    ref: str,
    kind: str,
    pilot_version: str,
    reachable: bool,
    probe_path: str,
) -> str:
    """Compose the ``ref_resolution`` explanation string.

    Args:
        repo: Chosen repository.
        ref: Chosen Git ref.
        kind: Candidate kind from :func:`_candidate_refs`.
        pilot_version: Pilot version string, possibly empty.
        reachable: Whether the probe confirmed the ref is fetchable.
        probe_path: Path used for the probe.

    Returns:
        Explanation suitable for inclusion in evidence and for the LLM to quote.
    """
    if kind == "release_tag":
        text = (
            f"Pinned to pilot release tag {ref} in {repo}; traceback line "
            "numbers correspond to the source that ran this job."
        )
    elif kind == "development_branch":
        text = (
            f"Pilot version {pilot_version} has no release tag, so this job ran "
            f"an unreleased build. Read the development branch {repo}@{ref}. "
            "That branch moves, so line numbers are indicative only and may "
            "not match the build that ran this job even where function names "
            "agree."
        )
    else:
        text = (
            f"Pilot version unknown, so the build could not be classified as "
            f"released or unreleased. Read {repo}@{ref}; line numbers may not "
            "correspond to the source that ran this job."
        )
    if not reachable:
        text += f" (No candidate ref was reachable when probing {probe_path}.)"
    return text


def resolve_source_ref(
    pilot_version: str,
    probe_path: str,
    timeout: int,
) -> SourceRef:
    """Choose the pilot3 repository and Git ref to fetch source from.

    Candidates from :func:`_candidate_refs` are probed in order with a real
    content fetch of *probe_path* — a HEAD request would not confirm that the
    file exists at that ref — and the first that succeeds is used for every
    subsequent fetch.  The probe response is retained in
    :attr:`SourceRef.probe_text` so the probed file is not downloaded twice.

    If no candidate is reachable the last one is used anyway, with the failure
    recorded in :attr:`SourceRef.resolution`; the per-file fetches will then
    populate ``fetch_errors`` and the LLM is told the source could not be read.

    Args:
        pilot_version: Pilot version string from job evidence, or an empty
            string when unknown.
        probe_path: Repo-relative path to test candidate refs against.
        timeout: HTTP timeout in seconds per probe.

    Returns:
        Populated :class:`SourceRef`.
    """
    candidates = _candidate_refs(pilot_version)
    for repo, ref, kind in candidates:
        status, text = _fetch_raw(_raw_url(repo, ref, probe_path), timeout)
        if 200 <= status < 300 and text is not None:
            return SourceRef(
                repo=repo,
                ref=ref,
                kind=kind,
                resolution=_describe_resolution(
                    repo, ref, kind, pilot_version, True, probe_path,
                ),
                probe_text=text,
                reachable=True,
            )
        logger.debug(
            "Pilot3 candidate %s@%s not usable for %s (HTTP %d)",
            repo, ref, probe_path, status,
        )

    repo, ref, kind = candidates[-1]
    logger.warning(
        "No pilot3 candidate ref was reachable for %s (version=%r); "
        "proceeding with %s@%s.",
        probe_path, pilot_version, repo, ref,
    )
    return SourceRef(
        repo=repo,
        ref=ref,
        kind=kind,
        resolution=_describe_resolution(
            repo, ref, kind, pilot_version, False, probe_path,
        ),
    )


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


def function_at_line(source: str, lineno: int) -> str | None:
    """Return the name of the function enclosing *lineno* in Python source.

    Used to verify that the source fetched from GitHub matches the build that
    produced the traceback.  When the ref could not be pinned to the job's
    pilot release, the module on ``master`` may have shifted by hundreds of
    lines, in which case quoting "line 2301" against the fetched file would be
    actively misleading.

    The innermost enclosing function wins, so a nested helper is reported
    rather than its parent.

    Args:
        source: Full Python source text of the module.
        lineno: Line number to resolve.

    Returns:
        Name of the innermost function containing *lineno*, or ``None`` if the
        source cannot be parsed or the line falls outside any function.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    best: str | None = None
    best_span: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None or not node.lineno <= lineno <= end:
            continue
        span = end - node.lineno
        if best_span is None or span < best_span:
            best, best_span = node.name, span
    return best


def verify_frame_lines(
    frames: list[dict[str, Any]],
    source_cache: dict[str, str | None],
) -> dict[str, Any]:
    """Check traceback line numbers against the fetched source.

    Args:
        frames: Frames from :func:`parse_traceback_frames`, each carrying
            ``pilot_path``, ``func`` and ``lineno``.
        source_cache: Mapping of ``pilot_path`` to fetched source text (or
            ``None`` when the fetch failed).

    Returns:
        Dict with ``checked`` (number of frames verified), ``mismatches``
        (list of dicts describing frames where the fetched source has a
        different function at that line) and ``version_skew`` (``True`` when
        any mismatch was found).  Callers should surface ``version_skew`` to the
        LLM so it avoids quoting line numbers as authoritative.
    """
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for frame in frames:
        source = source_cache.get(frame["pilot_path"])
        lineno = int(frame.get("lineno") or 0)
        if not source or lineno <= 0:
            continue
        checked += 1
        found = function_at_line(source, lineno)
        if found != frame["func"]:
            mismatches.append({
                "pilot_path": frame["pilot_path"],
                "lineno": lineno,
                "expected_func": frame["func"],
                "found_func": found,
            })
    return {
        "checked": checked,
        "mismatches": mismatches,
        "version_skew": bool(mismatches),
    }


# ---------------------------------------------------------------------------
# Core analysis function
# ---------------------------------------------------------------------------

def fetch_and_analyse_pilot_source(
    job_id: int,
    log_excerpt: str,
    pilot_error_diag: str,
    timeout: int = _FETCH_TIMEOUT,
    pilot_version: str = "",
) -> dict[str, Any]:
    """Parse the traceback, fetch relevant pilot modules, extract functions.

    Orchestrates the full analysis:
    1. Parse traceback frames (path, function, line number) from the excerpt.
    2. Resolve the repository and ref — the release tag for a released build, the
       development branch for an unreleased one (see :func:`resolve_source_ref`).
    3. Group unique file paths to minimise HTTP requests.
    4. Fetch each unique module, reusing the ref probe's response for the module
       it already downloaded.
    5. Extract each function named in the traceback from its module.
    6. Verify each traceback line number against the fetched source and flag
       version skew.
    7. Return structured evidence.

    Args:
        job_id: PanDA job ID (for evidence labelling).
        log_excerpt: Log excerpt containing the Python traceback.
        pilot_error_diag: ``piloterrordiag`` string from job metadata, used as
            a fallback exception description when the traceback cannot be
            parsed.
        timeout: HTTP timeout per GitHub fetch.
        pilot_version: Pilot release version that ran the job (e.g.
            ``"3.14.0.22"``), from ``panda_log_analysis`` evidence.  An empty
            string means "unknown", which resolves to ``master``.

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

    # Fetch each unique pilot source file once, from the repository and ref that
    # correspond to the build this job actually ran.
    unique_paths = list(dict.fromkeys(f["pilot_path"] for f in frames))
    source_ref = resolve_source_ref(pilot_version, unique_paths[0], timeout)
    source_cache: dict[str, str | None] = {}
    fetch_errors: dict[str, str] = {}

    # The probe already downloaded unique_paths[0]; reuse it rather than
    # fetching the same file a second time.
    if source_ref.probe_text is not None:
        source_cache[unique_paths[0]] = source_ref.probe_text

    for path in unique_paths:
        if path in source_cache:
            continue
        src, err = fetch_pilot_module(path, timeout, source_ref.ref, source_ref.repo)
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
        p: _browse_url(source_ref.repo, source_ref.ref, p) for p in unique_paths
    }
    line_verification = verify_frame_lines(frames, source_cache)

    evidence: dict[str, Any] = {
        "job_id": job_id,
        "exception": exception_str,
        "traceback_frames": frames,
        "source_snippets": source_snippets,
        "github_base_url": f"{_GITHUB_RAW_ROOT}/{source_ref.repo}/{source_ref.ref}",
        "github_urls": github_urls,
        "files_fetched": files_fetched,
        "missing_functions": missing_funcs,
        "fetch_errors": fetch_errors,
        "pilot_version": pilot_version or None,
        "github_repo": source_ref.repo,
        "github_ref": source_ref.ref,
        "ref_kind": source_ref.kind,
        "ref_resolution": source_ref.resolution,
        "line_verification": line_verification,
    }

    n_snippets = len(source_snippets)
    n_frames = len(frames)
    summary = (
        f"Job {job_id}: fetched {len(files_fetched)} pilot3 module(s) from "
        f"{source_ref.repo}@{source_ref.ref}, extracted "
        f"{n_snippets}/{n_frames} function(s) from the traceback."
    )
    if source_ref.kind == "development_branch":
        summary += (
            f" Pilot {pilot_version} is an unreleased build, so this is "
            "development-branch source and line numbers are indicative only."
        )
    elif source_ref.kind == "unknown_version":
        summary += " Pilot version unknown, so line numbers may not match."
    if fetch_errors:
        summary += f" Fetch errors: {list(fetch_errors.keys())}."
    if missing_funcs:
        summary += f" Functions not found in source: {missing_funcs}."
    if line_verification["version_skew"]:
        summary += (
            f" WARNING: {len(line_verification['mismatches'])} traceback line "
            "number(s) do not match the fetched source — line references may "
            "be unreliable."
        )

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
            "Deep-dive into a pilot exception by fetching the relevant pilot3 "
            "source modules from GitHub and extracting the exact functions "
            "named in the exception traceback. "
            "Use ONLY after panda_log_analysis has returned evidence with "
            "traceback_available=true and a non-null deepest_pilot_frame, and "
            "the user wants to understand why the pilot code raised the "
            "exception or how the affected function could be improved. "
            "Requires the log_excerpt from the prior panda_log_analysis call. "
            "Pass pilot_version from that evidence so the source is fetched at "
            "the release tag the job actually ran."
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
                "pilot_version": {
                    "type": "string",
                    "description": (
                        "Pilot release version from panda_log_analysis evidence "
                        "(evidence.pilot_version), e.g. '3.14.0.22'. Pins the "
                        "GitHub fetch to that release tag so traceback line "
                        "numbers match the fetched source. Omit if unknown."
                    ),
                },
            },
            "required": ["job_id", "log_excerpt"],
            "additionalProperties": False,
        },
        "examples": [
            {
                "job_id": 7261310898,
                "log_excerpt": (
                    "CRITICAL | execute payloads caught an exception "
                    "(cannot recover): timed out, Traceback (most recent call last):\n"
                    "  File \"/tmp/atlas_QCSsk3r1/pilot3/pilot/user/atlas/setup.py\","
                    " line 347, in download_transform\n"
                    "    content = download_file(url)\n"
                    "  File \"/tmp/atlas_QCSsk3r1/pilot3/pilot/util/https.py\","
                    " line 2301, in download_file\n"
                    "    with urllib.request.urlopen(req) as response:\n"
                    "TimeoutError: timed out"
                ),
                "pilot_error_diag": "Exception caught during payload execution",
                "pilot_version": "3.14.0.22",
            }
        ],
        "tags": [
            "atlas", "panda", "pilot", "pilot3", "source",
            "traceback", "exception", "github",
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
        pilot_version = str(arguments.get("pilot_version") or "")

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
                pilot_version,
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
    "fetch_pilot_module",
    "function_at_line",
    "get_definition",
    "extract_function_source",
    "parse_exception_line",
    "parse_traceback_frames",
    "pilot_source_analysis_tool",
    "resolve_source_ref",
    "verify_frame_lines",
]
