"""Superuser question detection for Bamboo MCP user interfaces.

Both the Streamlit and Textual interfaces import from this module so that
the guard logic is defined exactly once.

Detection strategy
------------------
The guard fires when **all** of the following are true:

1. ``BAMBOO_SUPERUSER_PASSWORD`` is set (superuser mode is enabled).
2. At least one superuser-gated tool is registered on the connected server.
3. The question matches one or more routing-signal patterns.

Routing signals are compiled from two sources:

* **Built-in defaults** — patterns that cover the common ways a user would
  phrase a source-code query: bare ``*.py`` filenames with inspection verbs,
  path-style references (``word/word.py``), and code-inspection phrases
  combined with a source-repository keyword.

* **``BAMBOO_SUPERUSER_PATTERNS``** — a comma-separated list of additional
  regex patterns supplied at deployment time.  Each pattern is compiled with
  ``re.IGNORECASE``.  Invalid patterns are skipped with a warning.

Superuser tool names
--------------------
The built-in set covers ``pilot_code_query`` and its namespaced form.
Additional names can be injected via ``BAMBOO_SUPERUSER_TOOLS`` (comma-
separated).  Both interfaces also maintain their own ``_SUPERUSER_TOOL_NAMES``
frozenset for the evidence-panel visibility gate; the set here is used only
for the pre-dispatch guard.

Configuration
-------------
``BAMBOO_SUPERUSER_PASSWORD``
    Plain-text password.  When absent the guard never fires (short-circuits
    at the call site before ``_is_superuser_question`` is reached).

``BAMBOO_SUPERUSER_TOOLS``
    Extra comma-separated tool names to treat as superuser-gated.
    Example: ``bamboo_code_query,atlas.bamboo_code_query``

``BAMBOO_SUPERUSER_PATTERNS``
    Extra comma-separated Python regex patterns (``re.IGNORECASE`` applied).
    Example: ``bamboo/.*\\.py,core/bamboo/.*``
"""
from __future__ import annotations

import logging
import os
import re
from typing import List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in defaults
# ---------------------------------------------------------------------------

#: Tool names that are superuser-gated by default.
_DEFAULT_SUPERUSER_TOOLS: frozenset[str] = frozenset({
    "code_query",
    "atlas.code_query",
})

# Built-in routing-signal patterns (compiled below).
# Each pattern is matched case-insensitively against the full question text.
_DEFAULT_PATTERN_STRINGS: list[str] = [
    # Any slash-delimited source path ending in .py: "pilot/util/foo.py",
    # "core/bamboo/tools/bar.py", "some/path.py"
    r"\b[\w][\w/]*\.py\b",
    # Inspection verbs combined with a .py mention anywhere in the question
    # (catches "look at pilot.py", "explain foo.py", etc.)
    # Handled structurally below — this pattern list covers path detection;
    # the verb+file combination is in _CODE_INSPECTION_VERBS.
]

#: Verbs / phrases that signal code inspection intent.
_CODE_INSPECTION_VERBS: frozenset[str] = frozenset({
    "look at",
    "read",
    "explain",
    "review",
    "analyse",
    "analyze",
    "show me",
    "check",
    "debug",
    "inspect",
    "examine",
    "walk me through",
    "walk through",
    "describe",
    "understand",
    "how does",
    "how do",
    "what does",
    "what do",
    "download",
    "fetch",
    "get",
    "get me",
    "grab",
    "pull",
    "open",
    "load",
    "display",
    "print",
    "list",
})

#: Keywords that identify a source-code repository context when combined
#: with a .py filename and an inspection verb.
_REPO_KEYWORDS: frozenset[str] = frozenset({
    "pilot",
    "bamboo",
    "panda",
    "plugin",
    "module",
    "function",
    "class",
    "source",
    "code",
    "script",
    "file",
})

# ---------------------------------------------------------------------------
# Compiled pattern cache (built once at module import time + env overrides)
# ---------------------------------------------------------------------------


def _compile_patterns() -> list[re.Pattern[str]]:
    """Build the compiled pattern list from defaults and env overrides.

    Returns:
        List of compiled regex patterns.  Any pattern that raises
        ``re.error`` is skipped and a warning is logged.
    """
    raw: list[str] = list(_DEFAULT_PATTERN_STRINGS)
    extra = os.getenv("BAMBOO_SUPERUSER_PATTERNS", "").strip()
    if extra:
        for pat in extra.split(","):
            pat = pat.strip()
            if pat:
                raw.append(pat)
    compiled: list[re.Pattern[str]] = []
    for pat in raw:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            logger.warning("BAMBOO_SUPERUSER_PATTERNS: invalid pattern %r (%s)", pat, exc)
    return compiled


def _build_tool_set() -> frozenset[str]:
    """Build the superuser tool name set from defaults and env overrides.

    Returns:
        Frozenset of tool names that are superuser-gated.
    """
    names: set[str] = set(_DEFAULT_SUPERUSER_TOOLS)
    extra = os.getenv("BAMBOO_SUPERUSER_TOOLS", "").strip()
    if extra:
        for name in extra.split(","):
            name = name.strip()
            if name:
                names.add(name)
    return frozenset(names)


#: Compiled patterns — built once at module load.
_COMPILED_PATTERNS: list[re.Pattern[str]] = _compile_patterns()

#: Full superuser tool name set — built once at module load.
SUPERUSER_TOOL_NAMES: frozenset[str] = _build_tool_set()


# ---------------------------------------------------------------------------
# Public guard function
# ---------------------------------------------------------------------------


def _has_py_filename(question_lower: str) -> bool:
    """Return True if the question contains any token ending in ``.py``.

    Matches bare filenames (``pilot.py``), path-style references
    (``pilot/util/processes.py``), and anything in between.

    Args:
        question_lower: Question text already lowercased.

    Returns:
        ``True`` when a ``*.py`` token is present.
    """
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(question_lower):
            return True
    return False


def _has_inspection_intent(question_lower: str) -> bool:
    """Return True when the question expresses code-inspection intent.

    Checks whether the question contains at least one inspection verb
    AND at least one repository keyword, indicating that the user wants
    to read or understand source code rather than just asking a general
    question that happens to mention a tool name.

    Args:
        question_lower: Question text already lowercased.

    Returns:
        ``True`` when both an inspection verb and a repo keyword are present.
    """
    has_verb = any(verb in question_lower for verb in _CODE_INSPECTION_VERBS)
    has_repo = any(kw in question_lower for kw in _REPO_KEYWORDS)
    return has_verb and has_repo


def is_superuser_question(question: str, tool_names: List[str]) -> bool:
    """Detect whether a question would likely route to a superuser-only tool.

    Used as a pre-dispatch guard in both the Streamlit and Textual interfaces.
    The call returns ``False`` immediately when no superuser tool is registered
    on the server, so the function is safe to call unconditionally.

    A question is flagged as superuser-routed when it contains **either**:

    * A ``*.py`` filename or path reference (unambiguous code-file mention), or
    * Both an inspection verb (``look at``, ``explain``, ``review``, …) and a
      repository keyword (``pilot``, ``bamboo``, ``source``, …).

    Additional patterns can be supplied via ``BAMBOO_SUPERUSER_PATTERNS``
    (see module docstring).

    Args:
        question: Raw question text from the user.
        tool_names: Tool names currently registered on the MCP server.

    Returns:
        ``True`` when the question matches a superuser routing signal and at
        least one superuser tool is registered on the server.
    """
    if not any(t in SUPERUSER_TOOL_NAMES for t in tool_names):
        return False

    q = question.lower()

    # Signal 1: any *.py filename or path present.
    if _has_py_filename(q):
        return True

    # Signal 2: inspection verb + repository keyword (catches phrasing like
    # "explain how the pilot works", "review the bamboo source").
    if _has_inspection_intent(q):
        return True

    return False


__all__ = [
    "is_superuser_question",
    "SUPERUSER_TOOL_NAMES",
    "_compile_patterns",
    "_build_tool_set",
]
