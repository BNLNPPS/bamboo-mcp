"""Source code query tool — superuser / developer mode.

Fetches an arbitrary source file from any configured GitHub repository
and returns it (or a named function extracted from it) as structured
evidence for LLM synthesis.

Unlike :mod:`bamboo.tools.pilot_source_analysis`, which is driven by a
job traceback, this tool is *query-driven*: the developer specifies a
file path and an optional function name directly.  The LLM then
answers the user's question about the code, optionally emitting a Mermaid
diagram when the answer describes an algorithm or flow.

Configuration
-------------
``BAMBOO_CODE_QUERY_REPO``
    GitHub repository in ``owner/name`` form.
    Default: ``PanDAWMS/pilot3``.
``BAMBOO_CODE_QUERY_BRANCH``
    Branch or tag to fetch from.
    Default: ``master``.

Security
--------
This tool is tagged ``superuser`` to signal that the Streamlit and TUI
interfaces should hide it from non-authenticated sessions.  However, it
is registered on the MCP server unconditionally so that the tool list
remains stable regardless of UI state.

Example
-------
``code_query(file_path="pilot/util/processes.py")``
    Returns the full source of the file.

``code_query(file_path="pilot/util/processes.py",
                   function_name="get_processes")``
    Returns only the ``get_processes`` function body.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import textwrap
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_REPO: str = "PanDAWMS/pilot3"
_DEFAULT_BRANCH: str = "master"

#: HTTP timeout in seconds for each GitHub raw fetch.
_FETCH_TIMEOUT: int = 20

#: Maximum characters of source text forwarded to the LLM.
#: The full file is fetched; only this many characters are sent.
#: This tool is superuser-only, so we can be generous: 150,000 characters
#: covers even the largest pilot modules (~3,000–4,000 lines) while staying
#: well within the context windows of all supported LLM providers
#: (Gemini 2.0 Flash: 1M tokens; GPT-4o / Mistral Large: 128K tokens;
#: Claude: 200K tokens — 150K chars ≈ 37K tokens, a small fraction of any).
#: If a specific function is requested, the limit applies to that body only.
_MAX_SOURCE_CHARS: int = 150_000


def _truncate_to_line_boundary(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``max_chars`` characters at a line boundary.

    Truncating mid-line leaves the LLM with a broken statement and produces
    incomplete analysis.  This helper finds the last newline within the limit
    and cuts there, appending a note so the LLM knows the file continues.

    Args:
        text: Source text to truncate.
        max_chars: Maximum number of characters to return (excluding the
            truncation note).

    Returns:
        Tuple of ``(truncated_text, was_truncated)``.  When the text fits
        within ``max_chars``, ``was_truncated`` is ``False`` and the text is
        returned unchanged.
    """
    if len(text) <= max_chars:
        return text, False
    # Find the last newline within the limit so we cut at a complete line.
    cut = text.rfind("\n", 0, max_chars)
    if cut == -1:
        # No newline found (single very long line) — hard-cut.
        cut = max_chars
    total_lines = text.count("\n") + 1
    shown_lines = text[:cut].count("\n") + 1
    remaining = total_lines - shown_lines
    note = (
        f"\n\n# --- TRUNCATED: showing {shown_lines} of {total_lines} lines "
        f"({remaining} lines omitted, {len(text):,} chars total) ---\n"
    )
    return text[:cut] + note, True


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _github_raw_url(repo: str, branch: str, file_path: str) -> str:
    """Build the raw.githubusercontent.com URL for a pilot source file.

    Args:
        repo: GitHub repository in ``owner/name`` form.
        branch: Branch or tag name (e.g. ``"master"``).
        file_path: Relative path within the repository
            (e.g. ``"pilot/util/processes.py"``).

    Returns:
        Full raw content URL string.
    """
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{file_path}"


def _github_browse_url(repo: str, branch: str, file_path: str) -> str:
    """Build the github.com browse URL for a pilot source file.

    Args:
        repo: GitHub repository in ``owner/name`` form.
        branch: Branch or tag name.
        file_path: Relative path within the repository.

    Returns:
        Full GitHub browse URL string.
    """
    return f"https://github.com/{repo}/blob/{branch}/{file_path}"


def _fetch_raw(url: str, timeout: int) -> tuple[int, str | None]:
    """Fetch a URL and return ``(http_status, text_or_None)``.

    Args:
        url: URL to fetch.
        timeout: HTTP timeout in seconds.

    Returns:
        Tuple of HTTP status code and decoded response text, or ``None``
        on connection/timeout errors (status 0).
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("HTTP fetch error for %s: %s", url, exc)
        return 0, None


# ---------------------------------------------------------------------------
# AST-based function extraction
# ---------------------------------------------------------------------------


def _extract_function(source: str, func_name: str) -> str | None:
    """Extract a named function body from Python source using the AST.

    Handles top-level functions, class methods, and nested functions.
    Includes any decorator lines above the ``def`` statement.

    Args:
        source: Full Python source text.
        func_name: Name of the function to find.

    Returns:
        Dedented source text of the function body including decorators,
        or ``None`` if the function is not found or the source cannot be
        parsed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        start = (
            node.decorator_list[0].lineno - 1
            if node.decorator_list
            else node.lineno - 1
        )
        end = getattr(node, "end_lineno", None)
        snippet_lines = lines[start:end] if end else lines[start: start + 200]
        return textwrap.dedent("\n".join(snippet_lines))
    return None


# ---------------------------------------------------------------------------
# Core fetch function (blocking — called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def fetch_source_file(
    file_path: str,
    function_name: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    timeout: int = _FETCH_TIMEOUT,
) -> dict[str, Any]:
    """Fetch pilot source code from GitHub and return structured evidence.

    Resolves the repository and branch from arguments, then from the
    ``BAMBOO_CODE_QUERY_REPO`` / ``BAMBOO_CODE_QUERY_BRANCH`` environment variables,
    falling back to the defaults defined in this module.

    Args:
        file_path: Relative path within the repository to fetch
            (e.g. ``"pilot/util/processes.py"``).
        function_name: Optional name of a specific function to extract.
            When ``None``, the full module source is returned (up to
            ``_MAX_SOURCE_CHARS`` characters).
        repo: GitHub repository override (``owner/name``).  When ``None``
            the ``BAMBOO_CODE_QUERY_REPO`` env var is consulted, then the
            built-in default.
        branch: Branch override.  When ``None`` the ``BAMBOO_CODE_QUERY_BRANCH``
            env var is consulted, then the built-in default.
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with keys ``file_path``, ``github_url``, ``repo``,
        ``branch``, ``function_name`` (or ``None``), ``source`` (truncated
        string or ``None``), ``truncated`` (bool), and ``fetch_error``
        (empty string on success).
    """
    resolved_repo = repo or os.getenv("BAMBOO_CODE_QUERY_REPO", _DEFAULT_REPO)
    resolved_branch = branch or os.getenv("BAMBOO_CODE_QUERY_BRANCH", _DEFAULT_BRANCH)

    raw_url = _github_raw_url(resolved_repo, resolved_branch, file_path)
    browse_url = _github_browse_url(resolved_repo, resolved_branch, file_path)

    status, text = _fetch_raw(raw_url, timeout)

    if text is None:
        return {
            "file_path": file_path,
            "github_url": browse_url,
            "repo": resolved_repo,
            "branch": resolved_branch,
            "function_name": function_name,
            "source": None,
            "truncated": False,
            "fetch_error": f"HTTP {status} fetching {raw_url}",
        }

    # Optionally extract a single function.
    if function_name:
        extracted = _extract_function(text, function_name)
        if extracted is None:
            source, truncated = _truncate_to_line_boundary(text, _MAX_SOURCE_CHARS)
            fetch_error = (
                f"Function '{function_name}' not found in {file_path}; "
                "returning full module source instead."
            )
        else:
            source, truncated = _truncate_to_line_boundary(extracted, _MAX_SOURCE_CHARS)
            fetch_error = ""
    else:
        source, truncated = _truncate_to_line_boundary(text, _MAX_SOURCE_CHARS)
        fetch_error = ""

    return {
        "file_path": file_path,
        "github_url": browse_url,
        "repo": resolved_repo,
        "branch": resolved_branch,
        "function_name": function_name,
        "source": source,
        "truncated": truncated,
        "fetch_error": fetch_error,
    }


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for pilot_code_query.

    Returns:
        Dict with ``name``, ``description``, ``inputSchema``,
        ``examples``, and ``tags`` keys.
    """
    repo = os.getenv("BAMBOO_CODE_QUERY_REPO", _DEFAULT_REPO)
    branch = os.getenv("BAMBOO_CODE_QUERY_BRANCH", _DEFAULT_BRANCH)
    return {
        "name": "code_query",
        "description": (
            "SUPERUSER / DEVELOPER TOOL. "
            f"Fetches a source file from the configured GitHub repository ({repo}, branch: {branch}). "
            "Use when the user asks about source code, wants to understand how an algorithm works, "
            "or suspects a bug in a specific file. "
            "Optionally extracts a single named function from the file. "
            "The LLM will explain the code and may generate a Mermaid diagram "
            "for algorithmic or flow-based questions. "
            "Configure the target repository via BAMBOO_CODE_QUERY_REPO. "
            "This tool requires superuser mode in the UI."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Relative path to the source file within the repository, "
                        "e.g. 'pilot/util/processes.py' or 'src/main.py'."
                    ),
                },
                "function_name": {
                    "type": "string",
                    "description": (
                        "Optional: name of a specific function to extract from the file. "
                        "When omitted, the full module source is returned."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "The developer's question about this code, e.g. "
                        "'Can this cause a race condition?' or "
                        "'Explain the looping job detection algorithm'."
                    ),
                },
            },
            "required": ["file_path", "question"],
            "additionalProperties": False,
        },
        "examples": [
            {
                "file_path": "pilot/util/processes.py",
                "question": "Can this cause issues when the UID is not in /etc/passwd?",
            },
            {
                "file_path": "pilot/control/job.py",
                "function_name": "get_job",
                "question": "Explain how the looping job detection algorithm works.",
            },
        ],
        "tags": ["source", "github", "code", "superuser", "developer"],
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class CodeQueryTool:
    """MCP tool for fetching arbitrary pilot source files from GitHub.

    Intended for developer / superuser sessions.  Fetches the requested
    file (or function) from the configured source code repository and
    returns structured evidence for LLM synthesis.  The synthesis prompt
    (:data:`~bamboo.tools.bamboo_executor._SYSTEM_CODE_QUERY`)
    instructs the LLM to explain the code and emit a Mermaid diagram
    when appropriate.
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
        """Fetch pilot source and return structured evidence.

        Delegates the blocking HTTP fetch to a thread pool via
        :func:`asyncio.to_thread`.

        Args:
            arguments: Dict with required ``file_path`` (str) and
                ``question`` (str), plus optional ``function_name`` (str).

        Returns:
            One-element MCP content list containing a JSON-serialised
            dict with ``evidence`` and ``text`` keys.
        """
        from bamboo.tools.base import text_content  # deferred import

        def _err(payload: dict[str, Any]) -> list[Any]:
            return text_content(json.dumps(payload))

        if not isinstance(arguments, dict):
            return _err({"evidence": {"error": "arguments must be a dict"}})

        file_path = str(arguments.get("file_path") or "").strip()
        if not file_path:
            return _err({
                "evidence": {"error": "file_path is required"},
                "text": "file_path is required.",
            })

        function_name: str | None = arguments.get("function_name") or None
        if function_name:
            function_name = str(function_name).strip() or None

        timeout: int = _FETCH_TIMEOUT
        try:
            timeout = int(arguments.get("timeout") or _FETCH_TIMEOUT)
        except (ValueError, TypeError):
            pass

        try:
            evidence = await asyncio.to_thread(
                fetch_source_file,
                file_path,
                function_name,
                None,  # repo from env
                None,  # branch from env
                timeout,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("Unexpected error in pilot_code_query for %s", file_path)
            return _err({
                "evidence": {"file_path": file_path, "error": repr(exc)},
                "text": f"Unexpected error fetching {file_path}: {exc}",
            })

        if evidence.get("fetch_error") and evidence.get("source") is None:
            summary = (
                f"Could not fetch {file_path} from "
                f"{evidence['repo']} ({evidence['branch']}): "
                f"{evidence['fetch_error']}"
            )
        else:
            func_part = (
                f", function '{function_name}'" if function_name else ""
            )
            trunc_note = " (truncated to fit context)" if evidence.get("truncated") else ""
            summary = (
                f"Fetched {file_path}{func_part} from "
                f"{evidence['repo']} ({evidence['branch']}){trunc_note}. "
                f"GitHub: {evidence['github_url']}"
            )

        return text_content(json.dumps({"evidence": evidence, "text": summary}))


code_query_tool = CodeQueryTool()

__all__ = [
    "CodeQueryTool",
    "fetch_source_file",
    "get_definition",
    "code_query_tool",
    "_extract_function",
    "_truncate_to_line_boundary",
    "_github_raw_url",
    "_github_browse_url",
]
