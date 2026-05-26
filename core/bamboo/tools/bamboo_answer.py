"""Bamboo answer tool — ATLAS-focused orchestration.

Routing (delegated to LLM planner)
------------------------------------
The original regex-based ``_route()`` dispatch has been replaced with a call
to :mod:`bamboo.tools.planner` (``bamboo_plan`` with ``execute=True``).

The planner receives:
  * the user question,
  * structured *hint* values extracted by the legacy regex helpers, and
  * the full conversation history,

and returns a synthesised natural-language answer via
:mod:`bamboo.tools.bamboo_executor`.

The regex hint-extractors (``_extract_task_id``, ``_extract_job_id``,
``_is_log_analysis_request``) are kept because they improve planner accuracy —
they do **not** drive routing decisions any more.
"""
from __future__ import annotations

import os
import re
from typing import Any, Sequence

from bamboo.llm.exceptions import LLMError
from bamboo.llm.types import Message
from bamboo.tools.base import MCPContent, coerce_messages, text_content
from bamboo.tools.llm_passthrough import bamboo_llm_answer_tool
from bamboo.tools.bamboo_executor import execute_plan, get_last_pilot_monitoring_evidence
from bamboo.tools.planner import (
    bamboo_plan_tool,
    Plan,
    PlanRoute,
    ReusePolicy,
    ToolCall,
)
from bamboo.tools.topic_guard import check_topic
from bamboo.tracing import EVENT_GUARD, span

# Matches \"task 123\", \"task:123\", \"task-123\" etc. (4-12 digits)
_TASK_PATTERN = re.compile(r"(?i)\btask[:#/\-\s]+([0-9]{4,12})\b")
# Matches \"job 123\", \"job:123\", \"pandaid 123\", \"panda id 123\" etc.
_JOB_PATTERN = re.compile(r"(?i)\b(?:job|pandaid|panda[\s_-]?id)[:#/\-\s]+([0-9]{4,12})\b")
# Matches \"analyse/analyze/why did ... job 123 fail\"
_LOG_PATTERN = re.compile(
    r"(?i)(?:analyz?e|analys[ei]|why|fail|log|diagnos)[^.]{0,60}"
    r"\bjob[:#/\-\s]+([0-9]{4,12})\b"
)
# Matches questions that are conceptual/definitional in nature — the user is
# asking what something *means* or *is*, not requesting fresh job/task data.
# A job or task ID in the same question is incidental context from a prior
# turn and must not trigger an operational tool call.
# Examples that should match:
#   "what does it mean that a job is looping?"
#   "what is a looping job?"
#   "what does stagein_timeout mean?"
#   "can you explain what pilot error 1305 is?"
#   "what's a reassigned job?"
_CONCEPTUAL_RE: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:"
    # "what does it/that/this mean"
    r"what\s+does\s+(?:it|that|this)\s+mean"
    r"|"
    # "what is a/an X" — requires indefinite article so "what is the status" is excluded
    r"what\s+(?:is|are)\s+(?:a\s+|an\s+)"
    r"|"
    # "what's a/an X"
    r"what'?s\s+(?:a\s+|an\s+)"
    r"|"
    # "can you explain/describe/define/tell me what"
    r"can\s+you\s+(?:explain|describe|define|tell\s+me\s+what)"
    r"|"
    # "explain/define/describe what"
    r"(?:explain|define|describe)\s+what"
    r"|"
    # "what does/do X mean" (X = one or more words up to ~30 chars)
    r"what\s+do(?:es)?\s+\w[\w\s]{0,30}mean"
    r")"
)
# Matches PanDA server liveness questions — \"is panda alive\", \"is the panda server ok\", etc.
# Deliberately avoids matching task/job/site questions that mention \"panda\" incidentally.
# Signal phrases that indicate the user wants source-level analysis of a
# pilot_monitoring_error.  Only consulted after confirming the last tool call
# was panda_log_analysis with failure_type='pilot_monitoring_error'.
_PILOT_SOURCE_SIGNALS: frozenset[str] = frozenset({
    "pilot code",
    "pilot source",
    "source code",
    "show me the source",
    "show the source",
    "the source",
    "why did the pilot",
    "why the pilot",
    "pilot raise",
    "pilot threw",
    "pilot exception",
    "fix the pilot",
    "fix this",
    "can it be fixed",
    "can this be fixed",
    "how to fix",
    "patch",
    "workaround",
    "the function",
    "that function",
    "the code",
    "that code",
    "deeper",
    "deep dive",
    "deep-dive",
    "drill down",
    "more detail",
    "more details",
    "list_processes",
    "getpwuid",
    "psutils",
})


def _is_pilot_source_request(question: str) -> bool:
    """Return True if the question is asking for pilot source-level analysis.

    Only meaningful when the last panda_log_analysis call returned
    failure_type='pilot_monitoring_error'.  The function is intentionally
    permissive — it is always guarded by the evidence check so false positives
    cannot misfire when there is no prior pilot_monitoring_error in context.

    Args:
        question: User question text.

    Returns:
        True if the question contains a pilot-source signal phrase.
    """
    q = question.lower()
    return any(sig in q for sig in _PILOT_SOURCE_SIGNALS)


_PANDA_HEALTH_RE: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:"
    r"is\s+(?:the\s+)?panda\s+(?:server\s+)?(?:alive|ok(?:ay)?|up|running|fine|healthy)"
    r"|panda\s+(?:server\s+)?(?:alive|ok(?:ay)?|up|running|status|health)"
    r"|(?:server|panda)\s+(?:liveness|heartbeat)"
    r"|is\s+panda\s+(?:server\s+)?(?:down|available|reachable|responding)"
    r"|panda\s+server\s+check"
    r")"
)

# ---------------------------------------------------------------------------
# Social routing — greetings and acknowledgements handled with zero LLM cost.
# Intercepted in _route() before the topic guard runs so "hello" and "thanks"
# never reach the LLM or produce a refusal.
# ---------------------------------------------------------------------------

_GREETING_RE: re.Pattern[str] = re.compile(
    r"^\s*("
    r"h+e+l+l*o+|"
    r"h+i+[!]*|"
    r"hey+[!]*|"
    r"good\s+(?:morning|afternoon|evening|day)|"
    r"howdy|greetings|sup|yo"
    r")[!.,\s]*$",
    re.IGNORECASE,
)

_ACK_RE: re.Pattern[str] = re.compile(
    r"^\s*("
    r"thanks?(?:\s+(?:a\s+lot|so\s+much|very\s+much|for\s+that))?|"
    r"thank\s+you(?:\s+(?:so\s+much|very\s+much))?|"
    r"thx|cheers|great|perfect|awesome|sounds?\s+good|got\s+it|"
    r"ok(?:ay)?|cool|nice|brilliant|excellent|wonderful|"
    r"understood|noted|roger(?:\s+that)?|good\s+to\s+know|"
    r"that(?:'s|\s+is)\s+(?:helpful|great|perfect|useful)|"
    r"bye|goodbye|see\s+you(?:\s+later)?"
    r")(?:\s*[,!.]\s*(?:thanks?|cheers|please|much\s+appreciated)?)?[!.\s]*$",
    re.IGNORECASE,
)

_GREETING_RESPONSE: str = (
    "Hello! I'm AskPanDA — ask me about PanDA tasks, jobs, pilots, "
    "computing sites, or ATLAS grid workflows. "
    "Try asking about a task ID, a failed job, or a site's current status."
)

_ACK_RESPONSE: str = (
    "You're welcome — let me know if there's anything else I can help with."
)


def _is_greeting(text: str) -> bool:
    """Return True if *text* is a standalone greeting with no content query.

    Args:
        text: The raw user message string.

    Returns:
        True when the entire message matches a common greeting pattern.
    """
    return bool(_GREETING_RE.match(text.strip()))


def _is_ack(text: str) -> bool:
    """Return True if *text* is a standalone acknowledgement or sign-off.

    Args:
        text: The raw user message string.

    Returns:
        True when the entire message matches a common acknowledgement pattern.
    """
    return bool(_ACK_RE.match(text.strip()))


def _is_panda_health_question(text: str) -> bool:
    """Return True if *text* is asking about PanDA server liveness or health.

    Matches phrases such as "Is the PanDA server alive?", "Is PanDA OK?",
    "PanDA server status", or "Is PanDA up?".  Deliberately avoids matching
    incidental mentions of "panda" in task, job, or site questions.

    Args:
        text: The raw user message string.

    Returns:
        True when the message is a PanDA server health/liveness query.
    """
    return bool(_PANDA_HEALTH_RE.search(text.strip()))


def _extract_task_id(text: str) -> int | None:
    """Extract a task ID from text.

    Args:
        text: Input text.

    Returns:
        The extracted task ID, or None if no task ID is found.
    """
    m = _TASK_PATTERN.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_job_id(text: str) -> int | None:
    """Extract a job (PanDA) ID from text.

    Args:
        text: Input text.

    Returns:
        The extracted job ID, or None if not found.
    """
    m = _JOB_PATTERN.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _is_log_analysis_request(text: str) -> bool:
    """Return True if the question is asking for log/failure analysis.

    Args:
        text: User question text.

    Returns:
        True if analysis keywords are present alongside a job reference.
    """
    return bool(_LOG_PATTERN.search(text or ""))


def _is_conceptual_question(text: str) -> bool:
    """Return True if the question is definitional/conceptual rather than operational.

    A conceptual question asks what something *means* or *is* — e.g. "what
    does it mean that a job is looping?" or "what is a stagein_timeout?".
    Any job or task ID present in the question is incidental context from a
    prior turn and must not cause the router to call an operational tool
    (``panda_job_status``, ``panda_log_analysis``, etc.).

    Args:
        text: User question text.

    Returns:
        True if the question matches a definitional/conceptual phrasing.
    """
    return bool(_CONCEPTUAL_RE.search(text or ""))


def _extract_history(messages: list[Message], current_question: str) -> list[Message]:
    """Extract prior conversation turns from a full messages list.

    The current question (last user message) is excluded so it is not
    duplicated when the synthesised user prompt is appended by the executor.
    Only ``"user"`` and ``"assistant"`` role messages are kept; ``"system"``
    messages from the client are dropped because the executor builds its own
    system prompt.

    Only the **last** user turn whose content matches ``current_question`` is
    stripped — earlier turns with the same text (repeated questions) are
    preserved.

    Args:
        messages: Full coerced chat history including the current turn.
        current_question: The question derived from the last user message,
            used to identify and strip the final user turn.

    Returns:
        List of prior ``{role, content}`` Message dicts in chronological
        order, suitable for passing as the ``history`` argument to the
        synthesis LLM call.
    """
    allowed_roles = {"user", "assistant"}
    # Find the index of the *last* user turn that matches current_question.
    tail_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if (
            msg.get("role") == "user" and
            str(msg.get("content", "")).strip() == current_question.strip()
        ):
            tail_idx = i
            break

    prior: list[Message] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = str(msg.get("content", "")).strip()
        if role not in allowed_roles or not content:
            continue
        if i == tail_idx:
            continue  # skip only the one identified current-question turn
        prior.append({"role": role, "content": content})  # type: ignore[typeddict-item]
    return prior


def _friendly_llm_error_import(exc: ImportError) -> str:
    """Return a user-readable message for a bare ``ImportError`` from a provider.

    This is a safety-net handler for LLM provider clients that omit the
    ``try/except ImportError`` guard around their lazy SDK import.  Well-behaved
    providers convert ``ImportError`` to
    :class:`~bamboo.llm.exceptions.LLMConfigError` themselves; this function
    ensures the user still sees an actionable message rather than a raw
    traceback if one slips through.

    Args:
        exc: The ``ImportError`` raised by a provider's lazy SDK import.

    Returns:
        A plain-text string suitable for display in the TUI or returned as
        tool output.
    """
    return f"\u2699\ufe0f  A required LLM provider package is not installed: {exc}"


def _friendly_llm_error(exc: LLMError) -> str:
    """Return a concise, user-readable explanation of an LLM provider error.

    For :class:`~bamboo.llm.exceptions.LLMConfigError`, the message is
    specialised based on its content:

    * If the error mentions a missing package (``"not installed"`` /
      ``"no module named"``), the original exception text is surfaced
      verbatim so the user sees the ``pip install`` hint rather than a
      misleading API-key message.
    * Otherwise the generic API-key configuration hint is shown.

    Args:
        exc: An :class:`~bamboo.llm.exceptions.LLMError` subclass instance.

    Returns:
        A plain-text string suitable for display in the TUI or returned as
        tool output.
    """
    from bamboo.llm.exceptions import (  # pylint: disable=import-outside-toplevel
        LLMConfigError, LLMRateLimitError, LLMTimeoutError,
    )

    raw = str(exc)

    if isinstance(exc, LLMConfigError):
        raw_lower = raw.lower()
        # Missing-package errors contain "not installed" — surface the exact
        # message so the user sees the pip install command rather than a
        # misleading hint about API keys.
        if "not installed" in raw_lower or "no module named" in raw_lower:
            return f"\u2699\ufe0f  {raw}"
        return (
            "\u2699\ufe0f  LLM not configured — check your API key environment variables "
            "(e.g. MISTRAL_API_KEY, OPENAI_API_KEY) and restart the server."
        )

    if isinstance(exc, LLMRateLimitError):
        return (
            "\u23f3  Rate limit reached on the LLM provider. "
            "Please wait a moment and try again."
        )

    if isinstance(exc, LLMTimeoutError):
        return (
            "\u23f1\ufe0f  The LLM provider did not respond in time. "
            "This is usually transient — please try again in a moment."
        )

    raw_lower = raw.lower()
    _overload_signals = ("503", "502", "overflow", "overloaded",
                         "upstream connect error", "reset before headers",
                         "reset reason")
    if any(s in raw_lower for s in _overload_signals):
        return (
            "\U0001f504  The LLM provider is temporarily overloaded (service unavailable). "
            "This is not a problem with your question — please try again in a few seconds."
        )
    if any(s in raw_lower for s in ("429", "rate limit", "rate_limit", "too many requests")):
        return (
            "\u23f3  Rate limit reached on the LLM provider. "
            "Please wait a moment and try again."
        )
    _auth_signals = ("401", "403", "unauthorized", "forbidden", "invalid api key",
                     "authentication")
    if any(s in raw_lower for s in _auth_signals):
        return (
            "\U0001f511  Authentication failed with the LLM provider. "
            "Check that your API key is correct and has not expired."
        )
    if any(s in raw_lower for s in ("timeout", "timed out", "deadline")):
        return (
            "\u23f1\ufe0f  The request to the LLM provider timed out. "
            "This is usually transient — please try again."
        )

    _known_prefixes = (
        "mistral error after retries: ",
        "openai error after retries: ",
        "openai-compatible error after retries: ",
        "anthropic error after retries: ",
        "gemini error after retries: ",
        "llm provider error: ",
        "provider error: ",
    )
    for prefix in _known_prefixes:
        if raw_lower.startswith(prefix):
            raw = raw[len(prefix):]
            break
    excerpt = raw[:200] + ("\u2026" if len(raw) > 200 else "")
    return (
        f"\u26a0\ufe0f  The LLM provider returned an error: {excerpt}\n"
        "This may be transient — please try again. "
        "If the problem persists, check the server logs."
    )


# ---------------------------------------------------------------------------
# Multi-database registry
# ---------------------------------------------------------------------------

#: Registry of queryable databases.  Key is the canonical name used in
#: routing; value is the human-readable description shown to the user when
#: clarification is needed.  Add a new entry here when a new database
#: comes online (e.g. CRIC).  The jobs DB entry must always be present.
QUERYABLE_DATABASES: dict[str, str] = {
    "jobs": "PanDA jobs database (computing site job statistics, error counts)",
    "cric": "CRIC (Computing Resource Information Catalogue — queues, sites, copytools)",
}

#: Words that unambiguously identify a specific database in the question.
#: Each key must match a key in :data:`QUERYABLE_DATABASES`.
#:
#: Design note: ``"queue"`` and ``"computing site"`` are intentionally absent
#: from the jobs keyword set.  In the single-DB era they were useful synonyms
#: for PanDA computing sites, but now that CRIC is registered these terms are
#: strongly associated with CRIC queue objects.  Keeping them in the jobs set
#: would make almost every CRIC question hit both keyword sets, producing a
#: spurious disambiguation prompt instead of routing to CRIC.
#: Similarly ``"site"`` alone is too generic to pin to either DB and is absent
#: from both sets — CRIC site questions are caught by ``_is_cric_question()``
#: (via ``"copytool"``, ``"maxwalltime"``, etc.) before disambiguation runs.
_DB_KEYWORDS: dict[str, frozenset[str]] = {
    "jobs": frozenset({
        "job", "jobs", "failed", "failing", "running", "finished",
        "starting", "waiting", "error", "errors", "pilot", "pandaid",
        "bnl", "cern", "aglt2", "slac",
        "swt2", "triumf", "in2p3", "nikhef", "pic", "sara",
    }),
    "cric": frozenset({
        "cric", "copytool", "online", "offline", "pledge", "resource",
        "capacity", "maxwalltime", "maxmemory", "corecount", "cpu slots",
        "queue status", "queue online", "queue offline",
        "queue", "queues",
    }),
}


def _resolve_target_database(question: str) -> str | None:
    """Return the unambiguous target database name, or ``None`` if unclear.

    Scans the question for keywords from :data:`_DB_KEYWORDS`.  If exactly
    one database matches, returns its name.  If zero or multiple match
    (ambiguous), returns ``None``.

    When only one database is registered in :data:`QUERYABLE_DATABASES`,
    always returns that database — no disambiguation needed.

    Args:
        question: The user's question text (before any normalisation).

    Returns:
        Canonical database name string, or ``None`` if ambiguous.
    """
    if len(QUERYABLE_DATABASES) <= 1:
        # Only one database registered — no ambiguity possible.
        return next(iter(QUERYABLE_DATABASES), None)

    q = question.lower()
    matches = {
        db
        for db, keywords in _DB_KEYWORDS.items()
        if db in QUERYABLE_DATABASES and any(kw in q for kw in keywords)
    }

    if len(matches) == 1:
        return next(iter(matches))
    return None


def _build_clarification_response(question: str) -> str:
    """Build a clarification message asking which database the user means.

    Args:
        question: The original user question.

    Returns:
        A plain-text clarification prompt listing the available databases.
    """
    db_list = "\n".join(
        f"  • **{name}** — {desc}"
        for name, desc in QUERYABLE_DATABASES.items()
    )
    return (
        f"I can query multiple databases. Which one did you mean?\n\n"
        f"{db_list}\n\n"
        f"Please rephrase your question mentioning the database name "
        f"(e.g. \"in the jobs database\" or \"in CRIC\")."
    )


# Signal phrases that, when present in a question without a task/job ID, suggest
# the user is asking about live job statistics from the ingestion database
# rather than a documentation or task-level question.
_JOBS_DB_SIGNALS: frozenset[str] = frozenset({
    "how many",
    "count",
    "failed at",
    "failing at",
    "running at",
    "finished at",
    "starting at",
    "errors at",
    "top errors",
    "failures at",
    "top failures",
    "job failure",
    "job failures",
    "job error",
    "job errors",
    "common failure",
    "common error",
    "job status at",
    "which jobs",
    "jobs at",
    "jobs failed at",
    # Cross-queue / ranking questions
    "most failed",
    "most errors",
    "most jobs",
    "queues with",
    "which queues",
    "which sites",
    "across queues",
    "across sites",
    # Status breakdown
    "each status",
    "by status",
    "status breakdown",
    "status count",
    # Database freshness / metadata
    "last updated",
    "last fetched",
    "database last",
    "db last",
    "when was the",
    "how fresh",
    "how old is",
    "how recent",
    # Common verb forms not covered above
    "ran at",
    "ran on",
    "running on",
    "failed on",
    "finished on",
})

# Job-specific signals for site-health detection: a subset of _JOBS_DB_SIGNALS
# that excludes generic counting phrases like "how many" and "count", and also
# excludes status-at phrases like "ran at" / "failed at" that can appear in
# pure pilot questions ("how many pilots failed at BNL?").  The signals here
# must unambiguously refer to jobs, not pilots.
_JOBS_DB_SPECIFIC_SIGNALS: frozenset[str] = frozenset({
    "errors at",
    "top errors",
    "job status at",
    "which jobs",
    "jobs at",
    "jobs failed at",
    "failed jobs",
    "failing jobs",
    "job failures",
    "job errors",
    "job error",
    "most failed",
    "most errors",
    "most jobs",
    "each status",
    "by status",
    "status breakdown",
    "failures at",
    "top failures",
    "common failure",
    "common error",
})


def _is_jobs_db_question(question: str) -> bool:
    """Return ``True`` when the question looks like a live jobs DB lookup.

    Detects questions about job counts, statuses, or error frequencies at a
    specific computing site that are best answered by querying the ingestion
    DuckDB database rather than the documentation index.

    The heuristic is intentionally conservative: it requires at least one
    signal phrase from :data:`_JOBS_DB_SIGNALS` and the absence of the word
    "task" (task-level questions route to ``panda_task_status`` instead).

    The LLM planner catches anything this heuristic misses, so false negatives
    are acceptable; false positives would cause incorrect routing.

    Args:
        question: User question text (before any normalisation).

    Returns:
        ``True`` if the question should be routed to ``panda_jobs_query``.
    """
    q = question.lower()
    if "task" in q:
        return False
    return any(sig in q for sig in _JOBS_DB_SIGNALS)


# Signal phrases that unambiguously indicate a CRIC queue/resource question.
# These bypass the topic guard for clearly on-topic CRIC questions.
# Phrases are matched against the lowercased question string.
# Note: "queue" and "site" alone are intentionally absent — they are also
# present in _DB_KEYWORDS["jobs"], making them ambiguous triggers that should
# invoke the disambiguation flow rather than routing directly to CRIC.
_CRIC_SIGNALS: frozenset[str] = frozenset({
    # Unambiguous CRIC-only terminology
    "cric",
    "copytool",
    "maxwalltime",
    "maxmemory",
    "corecount",
    "cpu slots",
    "brokeroff",
    # Copytool names and object-store vocabulary — only appear in CRIC context
    "objectstore",
    "object store",
    "gfalcopy",
    "rucio copytool",
    "using rucio",
    "using objectstore",
    "using gfal",
    # Unambiguous list-all-queues phrasing — no status/site filter,
    # clearly asking for the full queue inventory from CRIC.
    "all queues",
    "list all queues",
    "show all queues",
    "every queue",
    "full queue list",
    "complete queue list",
    "give me all queues",
    "get all queues",
    # Queue-state phrasing: "queue(s) <status>" or "<status> queue(s)"
    # These are always CRIC because CRIC is the queue catalogue.
    "queue online",
    "queue offline",
    "queue status",
    "queues online",
    "queues offline",
    "queues active",
    "queues are online",
    "queues are offline",
    "queues are active",
    "queues are not online",
    "queues are not active",
    "queues that are not online",
    "queues that are not active",
    "queues that are offline",
    "queues not online",
    "queues not active",
    "active queues",
    "online queues",
    "offline queues",
    "inactive queues",
    "is the queue",
    "is the bnl queue",
    "is the cern queue",
    "cric queues",
    "panda queues",
    # Site-capacity / resource questions
    "cpu pledge",
    "disk pledge",
    "site capacity",
    "resource information",
})


def _is_cric_question(question: str) -> bool:
    """Return ``True`` when the question looks like a CRIC resource lookup.

    Detects questions about queue status, copytools, or site capacity that
    are best answered by querying the CRIC DuckDB database rather than the
    documentation index.

    Two detection strategies:

    1. **Signal phrase match** — any phrase in :data:`_CRIC_SIGNALS` appears
       as a substring of the lowercased question.
    2. **Queue + status combo** — the question contains a queue-reference word
       (``"queue"`` or ``"queues"``) AND a queue-status word (``"active"``,
       ``"online"``, ``"offline"``, ``"brokeroff"``, ``"test"``).  This catches
       patterns like ``"Which queues at BNL are active?"`` where the status
       word appears after a site name, so no single ``_CRIC_SIGNALS`` substring
       would match.

    Args:
        question: User question text (before any normalisation).

    Returns:
        ``True`` if the question should be routed to ``cric_query``.
    """
    q = question.lower()
    if any(sig in q for sig in _CRIC_SIGNALS):
        return True
    # Strategy 2: queue-reference + status word anywhere in the sentence.
    has_queue_word = "queue" in q or "queues" in q
    if has_queue_word:
        _QUEUE_STATUS_WORDS = ("active", "online", "offline", "brokeroff")
        if any(w in q for w in _QUEUE_STATUS_WORDS):
            return True
    return False


# Signal phrases that unambiguously indicate a Harvester pilot/worker question
# requesting *live operational data* (counts, status, activity at a site).
# These bypass the topic guard because they are clearly on-topic and a guard
# LLM call would add ~3 s of latency.
# Phrases are matched against the lowercased question string.
#
# NOTE: bare "pilot" and "pilots" are intentionally absent.  Those single words
# also appear in conceptual/documentation questions such as "How does the panda
# pilot work?" or "Explain the pilot lifecycle."  Only operational, data-seeking
# phrases are listed here so that documentation questions fall through to the
# RAG retrieval path instead of calling panda_harvester_workers.
_PILOT_SIGNALS: frozenset[str] = frozenset({
    # Harvester-specific terminology (always operational)
    "harvester worker",
    "harvester workers",
    "worker count",
    "worker status",
    "nworkers",
    # Status-specific pilot questions (unambiguously requesting live counts)
    "pilots running",
    "pilots idle",
    "pilots failed",
    "pilots submitted",
    "pilots finished",
    "running pilots",
    "idle pilots",
    "failed pilots",
    "submitted pilots",
    # Explicit count / activity requests
    "pilot count",
    "pilot counts",
    "pilot activity",
    "pilot statistics",
    "pilot stats",
    "pilot monitor",
    "pilot health",
    # "how many pilots" — operational count question
    "how many pilots",
    # Resource-typed pilot questions (MCORE/SCORE/HCORE + pilots)
    "mcore pilots",
    "score pilots",
    "hcore pilots",
    # "pilot failure rate" — live metric
    "pilot failure rate",
    "pilot error rate",
    "pilots at",
    "pilots for",
})

# Documentation-intent prefixes that indicate the user wants a conceptual
# explanation rather than live operational data.  When any of these appear at
# the start of the lowercased question, _is_pilot_question() returns False so
# the question falls through to RAG retrieval even if a more specific signal
# phrase also matches (e.g. "How does pilot health monitoring work?").
_PILOT_DOC_PREFIXES: tuple[str, ...] = (
    "how does",
    "how do",
    "how is",
    "how are",
    "what is",
    "what are",
    "what does",
    "explain",
    "describe",
    "tell me about",
    "tell me how",
    "give me an overview",
    "give an overview",
    "overview of",
    "what's the",
    "what's a",
    "can you explain",
    "can you describe",
    "could you explain",
    "could you describe",
    "i want to understand",
    "help me understand",
    "walk me through",
)


def _is_pilot_question(question: str) -> bool:
    """Return ``True`` when the question is about Harvester pilots/workers.

    Checks for unambiguous pilot/Harvester signal phrases that route to
    ``panda_harvester_workers`` rather than the jobs DB or documentation
    index.  Questions that also contain a task or job ID are excluded here
    (they route through the normal ID-based path first).

    The heuristic requires at least one signal from :data:`_PILOT_SIGNALS`.
    False negatives are acceptable — the LLM planner will catch them.

    Questions that begin with a documentation-intent prefix from
    :data:`_PILOT_DOC_PREFIXES` (e.g. "How does the pilot work?", "What is
    the Harvester?") are excluded even when a signal phrase is present, so
    they fall through to RAG retrieval rather than calling the live API.

    Args:
        question: User question text (before any normalisation).

    Returns:
        ``True`` if the question should be routed to ``panda_harvester_workers``.
    """
    q = question.lower().strip()
    # Exclude conceptual / documentation questions regardless of signal matches.
    if any(q.startswith(prefix) for prefix in _PILOT_DOC_PREFIXES):
        return False
    return any(sig in q for sig in _PILOT_SIGNALS)


def _is_site_health_question(question: str) -> bool:
    """Return ``True`` when the question requires both pilot and job statistics.

    Detects questions that contain a pilot signal from :data:`_PILOT_SIGNALS`
    alongside either:

    - a phrase from :data:`_JOBS_DB_SPECIFIC_SIGNALS` (e.g. ``"job failures"``,
      ``"failed jobs"``, ``"job error"``), or
    - any bare occurrence of the word ``"job"`` or ``"jobs"`` from
      :data:`_JOB_WORDS`.

    The two-tier check handles both explicit job-stat phrasing
    (``"job failure rate"``) and natural co-occurrence phrasing
    (``"pilots and jobs"``, ``"job failure rates"``).

    Status-at phrases like ``"ran at"`` / ``"failed at"`` are intentionally
    absent from both signal sets to avoid false positives on pure pilot
    questions such as ``"how many pilots failed at BNL?"``.

    Questions with a ``"task"`` keyword are excluded: they likely refer to a
    specific task rather than live site statistics.

    Args:
        question: User question text (before any normalisation).

    Returns:
        ``True`` if the question should call both harvester and jobs tools.
    """
    q = question.lower()
    if "task" in q:
        return False
    # Use a broader pilot check here: bare "pilot"/"pilots" is safe because
    # the site-health function already requires a co-occurring jobs reference,
    # so conceptual doc questions like "How does the pilot work?" can't reach
    # this branch (they have no \bjobs?\b match).
    _PILOT_BROAD = _PILOT_SIGNALS | frozenset({"pilot", "pilots"})
    has_pilot = any(sig in q for sig in _PILOT_BROAD)
    if not has_pilot:
        return False
    # Use word-boundary matching for "job"/"jobs" to avoid false matches
    # on substrings (e.g. "panda_job_status" or "jobtype").
    has_jobs = (
        any(sig in q for sig in _JOBS_DB_SPECIFIC_SIGNALS) or
        bool(re.search(r"\bjobs?\b", q))
    )
    return has_jobs


def _extract_site_from_question(question: str) -> str | None:
    """Extract a computing site name from a question, if present.

    Uses two strategies in order:

    1. **Contextual pattern** — matches an uppercase token that follows a
       site-indicator word (``at``, ``for``, ``site``, ``queue``,
       ``from``, ``in``).  This handles the vast majority of real
       questions ("pilots at MWT2", "jobs at AGLT2", "site BNL").

    2. **Known-site fallback** — a short list of very common sites that
       appear as plain keywords without a preposition (e.g. "BNL
       summary").

    Site names are returned in uppercase as they appear in BigPanDA.

    Args:
        question: User question text.

    Returns:
        Site name string (uppercase), or ``None`` if not found.
    """
    # Strategy 1: token after a site-indicator preposition/keyword.
    # Handles: "at MWT2", "at BNL", "for AGLT2", "for site X",
    # "for queue X", "site X", "queue X", "from X".
    # An optional bridge word (site/queue) is allowed between the preposition
    # and the actual site token.
    _STOP_WORDS = frozenset({
        "the", "a", "an", "this", "that", "my", "your", "our", "their",
        "site", "queue", "all", "any", "each", "now", "here", "there",
    })
    _ctx = re.search(
        r"\b(?:at|for|from)\s+(?:site\s+|queue\s+)?([A-Za-z][A-Za-z0-9_\-\.]{1,19})"
        r"|"
        r"\b(?:site|queue)\s+([A-Za-z][A-Za-z0-9_\-\.]{1,19})\b",
        question,
    )
    if _ctx:
        token = _ctx.group(1) or _ctx.group(2)
        if token and token.lower() not in _STOP_WORDS:
            token_upper = token.upper()
            # Accept if: has a digit, or has separator chars, or is all-uppercase ≥2 chars.
            if (any(c.isdigit() for c in token) or
                    re.search(r"[_\-\.]", token) or
                    (token.isupper() and len(token) >= 2)):
                return token_upper

    # Strategy 2: short fallback list for sites used without a preposition.
    _KNOWN_SITES: tuple[str, ...] = (
        "BNL", "CERN", "AGLT2", "SLAC", "SWT2", "TRIUMF", "IN2P3",
        "NIKHEF", "PIC", "SARA", "MWT2", "NET2", "TOKYO", "BEIJING",
        "TAIWAN", "GRIF", "IFIC", "INFN", "JINR", "KIAE", "SIGNET",
    )
    q_upper = question.upper()
    for site in _KNOWN_SITES:
        if re.search(r"\b" + re.escape(site) + r"\b", q_upper):
            return site

    return None


def _extract_time_window_from_question(
    question: str,
) -> tuple[str, str] | None:
    """Extract an explicit time window from a pilot question, if present.

    Translates natural-language temporal expressions into ISO-8601
    ``(from_dt, to_dt)`` pairs (UTC, no timezone suffix) suitable for
    passing directly to the Harvester API.  Returns ``None`` when no
    recognised expression is found, in which case the tool falls back to
    its own default (the last hour).

    Recognised patterns (case-insensitive):
    - "last N hours" / "past N hours" / "in the last N hours"
    - "last N minutes" / "past N minutes"
    - "last N days" / "past N days"
    - "yesterday" / "since yesterday"
    - "today"
    - "last 24 hours" (handled by the N-hours rule above)
    - "right now" / "now" / "currently" → ``None`` (use tool default)
    - "between YYYY-MM-DDTHH:MM:SS and YYYY-MM-DDTHH:MM:SS" → verbatim

    Args:
        question: User question text.

    Returns:
        ``(from_dt, to_dt)`` ISO-8601 strings (UTC), or ``None`` if no
        temporal expression was found or the expression means "now".
    """
    import re
    from datetime import datetime, timedelta, timezone

    q = question.lower()
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)

    def _fmt(dt: datetime) -> str:
        """Format a datetime as a bare ISO-8601 string without timezone suffix."""
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Explicit ISO range: "between 2026-03-24T00:00:00 and 2026-03-25T00:00:00"
    _iso_range = re.search(
        r"between\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
        r"\s+and\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})",
        question,
        re.IGNORECASE,
    )
    if _iso_range:
        return _iso_range.group(1), _iso_range.group(2)

    # "last/past N hours/minutes/days"
    _window = re.search(
        r"(?:last|past|in\s+the\s+last|in\s+the\s+past)\s+(\d+)\s+"
        r"(hour|hours|hr|hrs|minute|minutes|min|mins|day|days)",
        q,
    )
    if _window:
        n = int(_window.group(1))
        unit = _window.group(2)
        if unit.startswith("min"):
            delta = timedelta(minutes=n)
        elif unit.startswith("day"):
            delta = timedelta(days=n)
        else:
            delta = timedelta(hours=n)
        return _fmt(now - delta), _fmt(now)

    # "yesterday" / "since yesterday"
    if re.search(r"\b(?:since\s+)?yesterday\b", q):
        midnight_today = now.replace(hour=0, minute=0, second=0)
        midnight_yesterday = midnight_today - timedelta(days=1)
        return _fmt(midnight_yesterday), _fmt(now)

    # "today" / "since today" / "so far today"
    if re.search(r"\b(?:since\s+)?today\b", q):
        midnight_today = now.replace(hour=0, minute=0, second=0)
        return _fmt(midnight_today), _fmt(now)

    # "right now" / "currently" / "now" → use tool default (last hour)
    return None


def _is_code_query_question(question: str) -> bool:
    """Return True when the question is asking to inspect a source code file.

    Matches the same signals as the superuser pre-dispatch guard in the UI
    (``interfaces.shared.superuser_guard``) so that routing is consistent:
    questions blocked by the guard are also what the planner would route to
    ``code_query``.

    Signals:
    - Any ``*.py`` filename or path reference (e.g. ``pilot.py``,
      ``pilot/util/processes.py``, ``core/bamboo/tools/foo.py``).
    - An inspection verb (``look at``, ``explain``, ``review``, …) combined
      with a repository keyword (``pilot``, ``bamboo``, ``source``, ``code``,
      …) — covers phrasing like *"explain how the pilot works"*.

    Args:
        question: User question text.

    Returns:
        bool: ``True`` when the question matches a code-query routing signal.
    """
    q = question.lower()
    # Signal 1: any *.py token (bare filename or slash-path) — always code_query
    if re.search(r"\b[\w][\w/]*\.py\b", q):
        return True
    # Exclusion: diagram/visualisation requests without a file path route to
    # RAG instead, where _MERMAID_GUIDANCE handles diagram generation directly.
    _diagram_kws = {"diagram", "state machine", "flowchart", "mermaid", "chart", "visuali"}
    if any(d in q for d in _diagram_kws):
        return False
    # Signal 2: inspection verb + repository/code keyword
    _verbs = {
        "look at", "read", "explain", "review", "analyse", "analyze",
        "show me", "check", "debug", "inspect", "examine",
        "walk me through", "walk through", "describe",
        "understand", "how does", "how do", "what does", "what do",
        "download", "fetch", "get", "get me", "grab", "pull",
        "open", "load", "display", "print", "list",
    }
    _repo_kws = {
        "pilot", "bamboo", "panda", "plugin", "module",
        "function", "class", "source", "code", "script", "file",
    }
    has_verb = any(v in q for v in _verbs)
    has_kw = any(k in q for k in _repo_kws)
    return has_verb and has_kw


# Signal phrases that indicate the user is querying Bamboo's own prompt log
# (self-observability queries rather than PanDA/ATLAS domain questions).
# Must be checked before the doc-search fallback so that "show me all
# questions asked today" or "what are the most frequently asked questions?"
# route to opensearch_promptlog_query rather than RAG.
_PROMPTLOG_SIGNALS: frozenset[str] = frozenset({
    # Turn / session vocabulary
    "turns", "turn number", "my last session", "last session",
    "replay session", "replay my session", "session id",
    "how many turns", "how many sessions",
    # Self-query vocabulary
    "questions asked", "questions today", "asked today",
    "frequently asked", "most asked", "faq", "faqs",
    "most frequent", "common questions", "popular questions",
    "what did i ask", "what have i asked",
    # Tool-usage analytics
    "which tools", "tools used", "tool usage", "tool calls",
    "tools called", "how many times was", "opensearch_promptlog",
    # Model / provider introspection
    "which model", "token count", "token usage",
    "input tokens", "output tokens",
    # Prompt-log index
    "prompt log", "prompt logging", "bamboomcp-promptlog",
    # Ratings
    "rating", "ratings", "rated", "star rating", "star ratings",
    "lowest rated", "highest rated", "average rating",
})

# Multi-word phrase signals for promptlog queries (checked via substring match).
_PROMPTLOG_PHRASES: tuple[str, ...] = (
    "questions asked",
    "asked today",
    "asked this week",
    "frequently asked",
    "most asked",
    "most frequent",
    "common question",
    "popular question",
    "what did i ask",
    "what have i asked",
    "show me all questions",
    "all questions",
    "replay session",
    "replay my",
    "my last session",
    "last session",
    "how many turns",
    "how many sessions",
    "which tools",
    "tools used",
    "tool usage",
    "tool calls",
    "how many times was",
    "prompt log",
    "token count",
    "token usage",
    "which model",
    "all the rates",
    "all rates",
    "show rates",
    "my ratings",
    "all ratings",
    "rated today",
    "rated this",
    "lowest rated",
    "highest rated",
    "average rating",
    "star rating",
)


def _is_promptlog_question(question: str) -> bool:
    """Return True when the question targets Bamboo's own prompt/session logs.

    Covers self-observability queries such as:
    - "show me all questions asked today"
    - "what are the frequently asked questions?"
    - "how many turns did my last session have?"
    - "which tools were used most often this week?"

    These questions should route to ``opensearch_promptlog_query`` rather
    than the RAG doc-search fallback.

    Args:
        question: User question text.

    Returns:
        bool: ``True`` when the question matches a prompt-log routing signal.
    """
    q = question.lower()
    tokens = set(re.findall(r"\b\w+\b", q))
    if tokens & _PROMPTLOG_SIGNALS:
        return True
    return any(phrase in q for phrase in _PROMPTLOG_PHRASES)


def _build_promptlog_plan(question: str, reuse: ReusePolicy) -> Plan:
    """Build a fast-path Plan routing to ``opensearch_promptlog_query``.

    Args:
        question: User question text.
        reuse: Reuse policy forwarded from the calling plan builder.

    Returns:
        A :class:`Plan` routing to ``opensearch_promptlog_query``.
    """
    return Plan(
        route=PlanRoute.FAST_PATH,
        confidence=0.95,
        tool_calls=[ToolCall(
            tool="opensearch_promptlog_query",
            arguments={"query": question},
        )],
        reuse_policy=reuse,
        explain="Deterministic: prompt-log signals → opensearch_promptlog_query.",
    )


def _build_code_query_plan(question: str, reuse: ReusePolicy) -> Plan:
    """Build a fast-path Plan routing to ``code_query``.

    Extracts the first ``*.py`` file path from the question (if present) and
    constructs the tool arguments.  When no path is found the question itself
    is passed as context so the tool can return a clear error.

    Args:
        question: User question text.
        reuse: Reuse policy forwarded from the calling plan builder.

    Returns:
        A :class:`Plan` routing to ``code_query``.
    """
    path_match = re.search(r"\b([\w][\w/]*\.py)\b", question, re.IGNORECASE)
    file_path = path_match.group(1) if path_match else None
    cq_args: dict[str, str] = {"question": question}
    if file_path:
        cq_args["file_path"] = file_path
    explain = (
        f"Deterministic: source code file signal "
        f"({'file_path=' + file_path if file_path else 'no path extracted'}) "
        f"\u2192 code_query."
    )
    return Plan(
        route=PlanRoute.FAST_PATH,
        confidence=0.88,
        tool_calls=[ToolCall(tool="code_query", arguments=cq_args)],
        reuse_policy=reuse,
        explain=explain,
    )


def _build_deterministic_plan(  # noqa: C901
    question: str,
    task_id: int | None,
    job_id: int | None,
    plugin_id: str = "atlas",
) -> "Plan | None":
    """Build a Plan without an LLM call for unambiguous routing cases.

    Returns a validated Plan for the six clear-cut routes, or ``None`` when
    the question is ambiguous enough to need the LLM planner.

    Fast-path rules (in priority order):
    1b. Job ID + pilot-source signals + stored pilot_monitoring_error → ``pilot_source_analysis`` FAST_PATH
    1. Job ID + analysis keywords   → ``panda_log_analysis``       FAST_PATH
    2. Job ID (no task ID)          → ``panda_job_status``         FAST_PATH
    3. Task ID                      → ``panda_task_status``        FAST_PATH
    4. Pilot/Harvester signals      → ``panda_harvester_workers``  FAST_PATH
    5. Jobs DB signals (no IDs)     → ``panda_jobs_query``         FAST_PATH
    6. Source code signals          → ``code_query``               FAST_PATH
    7. Prompt-log signals           → ``opensearch_promptlog_query`` FAST_PATH
    8. No IDs                       → doc_search + doc_bm25        RETRIEVE

    Args:
        question: User question text.
        task_id: Extracted task ID, or None.
        job_id: Extracted job ID, or None.
        plugin_id: Active plugin identifier; determines which doc tools to use
            for the fallback RAG retrieval route.

    Returns:
        A validated :class:`~bamboo.tools.planner.Plan`, or ``None`` to
        signal that the LLM planner should be used instead.
    """
    reuse = ReusePolicy()

    # PanDA-specific routing rules (job status, log analysis, task status,
    # pilot workers) only apply to PanDA-family plugins.  Non-PanDA plugins
    # (e.g. "cgsim") skip this entire block: numeric IDs in their questions
    # are domain identifiers (simulation job IDs, etc.), not PanDA job IDs,
    # and routing them to panda_* tools would produce nonsensical responses.
    # Adding a new non-PanDA plugin: include its plugin_id in _PANDA_PLUGINS
    # only if it should use PanDA job/task/pilot routing; otherwise leave it
    # out and it will fall through to its own fast-path below.
    _PANDA_PLUGINS: frozenset[str] = frozenset({"atlas", "epic"})
    if plugin_id in _PANDA_PLUGINS:
        # Rule 1b: follow-up pilot source analysis — checked FIRST.
        # If the last panda_log_analysis returned pilot_monitoring_error and the
        # question contains pilot-source signals, route to pilot_source_analysis
        # using the stored log_excerpt.  This must come before rule 1 (log analysis)
        # because questions like "Why did the pilot code raise that? job 7099503721"
        # match _is_log_analysis_request ("why" + job ID) and would otherwise
        # re-run panda_log_analysis instead of fetching the pilot source.
        if job_id and _is_pilot_source_request(question):
            monitoring_evidence = get_last_pilot_monitoring_evidence()
            if monitoring_evidence is not None:
                return Plan(
                    route=PlanRoute.FAST_PATH,
                    confidence=1.0,
                    tool_calls=[ToolCall(
                        tool="pilot_source_analysis",
                        arguments={
                            "job_id": job_id,
                            "log_excerpt": monitoring_evidence.get("log_excerpt", ""),
                            "pilot_error_diag": monitoring_evidence.get("piloterrordiag", ""),
                        },
                    )],
                    reuse_policy=reuse,
                    explain=(
                        "Deterministic: job ID + pilot-source keywords + prior "
                        "pilot_monitoring_error evidence → pilot source analysis."
                    ),
                )

        # Rule 1: job ID + analysis keywords → log analysis.
        if job_id and _is_log_analysis_request(question):
            return Plan(
                route=PlanRoute.FAST_PATH,
                confidence=1.0,
                tool_calls=[ToolCall(
                    tool="panda_log_analysis",
                    arguments={"job_id": job_id, "query": question, "context": ""},
                )],
                reuse_policy=reuse,
                explain="Deterministic: job ID + analysis keywords → log analysis.",
            )

        # Rule 2: job ID (no task ID) → job status.
        # Guard: if the question is conceptual/definitional ("what does X mean",
        # "what is a looping job") the job ID is incidental context from a prior
        # turn.  Fall through to the LLM planner so it can answer from docs/RAG
        # instead of fetching fresh (and irrelevant) job status data.
        if job_id and not task_id and not _is_conceptual_question(question):
            return Plan(
                route=PlanRoute.FAST_PATH,
                confidence=1.0,
                tool_calls=[ToolCall(
                    tool="panda_job_status",
                    arguments={"job_id": job_id, "query": question},
                )],
                reuse_policy=reuse,
                explain="Deterministic: job ID, no task ID → job status.",
            )

        # Rule 3: task ID → task status.
        if task_id:
            return Plan(
                route=PlanRoute.FAST_PATH,
                confidence=1.0,
                tool_calls=[ToolCall(
                    tool="panda_task_status",
                    arguments={"task_id": task_id, "query": question, "include_jobs": True},
                )],
                reuse_policy=reuse,
                explain="Deterministic: task ID present → task status.",
            )

    # Pilot / Harvester fast-path: pilot-specific signal phrases are unambiguously
    # on-topic and resolve to panda_harvester_workers without a topic-guard LLM call.
    # Checked before the jobs DB path because "pilot" can co-occur with jobs signals.
    if _is_pilot_question(question):
        pilot_args: dict[str, str] = {"question": question}
        site = _extract_site_from_question(question)
        if site:
            pilot_args["site"] = site
        window = _extract_time_window_from_question(question)
        if window:
            pilot_args["from_dt"], pilot_args["to_dt"] = window
        return Plan(
            route=PlanRoute.FAST_PATH,
            confidence=0.95,
            tool_calls=[ToolCall(
                tool="panda_harvester_workers",
                arguments=pilot_args,
            )],
            reuse_policy=reuse,
            explain="Deterministic: pilot/Harvester signals, no task/job ID → harvester workers.",
        )

    # CRIC fast-path: checked before jobs DB because CRIC signals are more
    # specific (copytool, maxwalltime, queue online/offline, etc.) and should
    # win when both _is_cric_question and _is_jobs_db_question fire together —
    # e.g. "which queues are using the rucio copytool?" hits _JOBS_DB_SIGNALS
    # via "which queues" but is unambiguously a CRIC question.
    if _is_cric_question(question):
        return Plan(
            route=PlanRoute.FAST_PATH,
            confidence=0.9,
            tool_calls=[ToolCall(
                tool="cric_query",
                arguments={"question": question},
            )],
            reuse_policy=reuse,
            explain="Deterministic: CRIC signals, no task/job ID → CRIC query.",
        )

    # CGSim fast-path: for the CGSim plugin, all non-conceptual questions
    # route to cgsim.sim_query regardless of whether they also match jobs DB
    # signal phrases.  This must come before the jobs DB check so that
    # questions like "which site had the most jobs?" route to the simulation
    # database rather than panda_jobs_query.
    if plugin_id == "cgsim" and not _is_conceptual_question(question):
        return Plan(
            route=PlanRoute.FAST_PATH,
            confidence=0.85,
            tool_calls=[ToolCall(
                tool="cgsim.sim_query",
                arguments={"question": question},
            )],
            reuse_policy=reuse,
            explain="Deterministic: CGSim plugin, no task/job ID, non-conceptual → sim_query.",
        )

    # Jobs DB fast-path: no IDs but the question is about live job stats.
    if _is_jobs_db_question(question):
        jobs_args: dict[str, str] = {"question": question}
        site = _extract_site_from_question(question)
        if site:
            jobs_args["queue"] = site
        return Plan(
            route=PlanRoute.FAST_PATH,
            confidence=0.9,
            tool_calls=[ToolCall(
                tool="panda_jobs_query",
                arguments=jobs_args,
            )],
            reuse_policy=reuse,
            explain="Deterministic: jobs DB signals, no task/job ID → jobs query.",
        )

    # Prompt-log fast-path: Bamboo self-observability queries.
    # Must come BEFORE the doc-search fallback so that questions like
    # "show me all questions asked today" or "what are the most frequently
    # asked questions?" route to opensearch_promptlog_query rather than RAG.
    # Checked after all PanDA-domain rules so numeric IDs still win.
    if _is_promptlog_question(question):
        return _build_promptlog_plan(question, reuse)

    # Code query fast-path: source code file inspection question.
    # Must come after all ID-driven and domain-specific rules so a question
    # like "why did job 123 fail?" that also mentions "pilot.py" in the log
    # still routes to panda_log_analysis, not code_query.
    if _is_code_query_question(question):
        return _build_code_query_plan(question, reuse)

    # No IDs: general knowledge / documentation question → always retrieve.
    # top_k=5 for both to keep synthesis prompt within ~2500 input tokens,
    # well clear of the 30s TUI timeout even on follow-up turns with history.
    from bamboo.tools.bamboo_executor import _PLUGIN_DOC_TOOLS, _DEFAULT_DOC_TOOLS  # noqa: PLC0415
    _doc_tools = list(_PLUGIN_DOC_TOOLS.get(plugin_id, _DEFAULT_DOC_TOOLS))
    doc_search = _doc_tools[0] if _doc_tools else "panda_doc_search"
    doc_bm25 = _doc_tools[1] if len(_doc_tools) > 1 else "panda_doc_bm25"
    return Plan(
        route=PlanRoute.RETRIEVE,
        confidence=1.0,
        tool_calls=[
            ToolCall(
                tool=doc_search,
                arguments={"query": question, "top_k": 5},
            ),
            ToolCall(
                tool=doc_bm25,
                arguments={"query": question, "top_k": 5},
            ),
        ],
        reuse_policy=reuse,
        explain=f"Deterministic: no task/job ID → RAG retrieval ({doc_search}, {doc_bm25}).",
    )


# Matches content-free follow-up phrases that carry no domain information.
# When matched (and history is present), we skip the LLM guard and substitute
# the last meaningful user question as the RAG query.
_FOLLOWUP_PATTERN = re.compile(
    r"^(please\s+)?(tell me more|explain more|more details?|elaborate|"
    r"go on|continue|explain further|can you expand|"
    r"more information|more info|say more|more|"
    r"yes\s+please|yes|yeah|yep|ok|okay|sure|go ahead|"
    r"do it|do that|fetch it|get it|fetch the file|get the file)"
    r"(\s+please)?\s*[.!?]*$",
    re.IGNORECASE,
)

# Matches questions that refer back to a previous result by pronoun or
# demonstrative — i.e. they have no ID of their own but are clearly
# about the most recently discussed task or job.
_CONTEXTUAL_FOLLOWUP_RE = re.compile(
    r"\b("
    r"those|them|they|their|"
    r"that task|that job|the task|the job|the jobs|the results?|"
    r"of those|of them|of the|"
    r"corresponding|"
    r"it|its"
    r")\b",
    re.IGNORECASE,
)

# Domain words that, when present in a short question with no ID, signal the
# question is about the most recently discussed task or job rather than a
# general PanDA documentation query.
# Deliberately excludes "task" and "job" alone (too common in doc questions)
# in favour of status-specific terms that are unambiguous in follow-up context.
_DOMAIN_WORD_RE = re.compile(
    r"\b("
    r"failed|fail|failing|"
    r"finished|finish|finishing|"
    r"running|started|starting|"
    r"transferring|transferred|"
    r"activated|activat(?:ed|ing)|"
    r"panda[\s_-]?ids?|pandaid|"
    r"piloterror(?:code|diag)|"
    r"error\s+code|error\s+codes|"
    r"top\s+errors?|"
    r"how\s+many\s+(?:jobs?|are|were|did|errors?)|"
    r"which\s+sites?|"
    r"any\s+(?:errors?|failures?)"
    r")\b",
    re.IGNORECASE,
)

# Questions at or below this word count are treated as implicit follow-ups
# without requiring a domain word — they are almost never general doc queries.
_SHORT_FOLLOWUP_WORD_LIMIT: int = 6

# Questions above _SHORT_FOLLOWUP_WORD_LIMIT but at or below this limit are
# treated as implicit follow-ups only when a domain word is also present.
_MEDIUM_FOLLOWUP_WORD_LIMIT: int = 10


def _is_content_free_followup(question: str) -> bool:
    """Return True if the question carries no domain-specific content.

    Content-free follow-ups like "Tell me more please" or "Elaborate" cannot
    be used as meaningful RAG queries and should not trigger the LLM topic
    guard — they are trivially on-topic when history is present.

    Args:
        question: The user's question text.

    Returns:
        True if the question matches a known content-free follow-up pattern.
    """
    return bool(_FOLLOWUP_PATTERN.match(question.strip()))


def _is_contextual_followup(question: str) -> bool:
    """Return True if the question contains an explicit back-reference to prior context.

    Only detects *explicit* pronoun/demonstrative back-references ("those",
    "them", "it", "that task" etc.).  Implicit short questions without a
    back-reference are handled separately in :func:`_route` using the history
    context to decide whether to re-use a prior ID.

    Args:
        question: The user's question text (caller has already verified
            that no task or job ID is present).

    Returns:
        True when the question contains an explicit contextual back-reference.
    """
    q = question.strip()
    return bool(_CONTEXTUAL_FOLLOWUP_RE.search(q)) if q else False


def _is_implicit_contextual_followup(question: str) -> bool:
    """Return True if the question is a short, domain-specific follow-up.

    Used when the question has no explicit back-reference but is short and
    contains status-specific terminology that makes sense only in the context
    of a previously discussed task or job.  Always called *after* confirming
    that history contains a recent task/job ID.

    Returns ``False`` immediately when the question contains unambiguous
    routing signals of its own — pilot phrases, a recognisable site name, or
    jobs-DB signal phrases.  Those questions are self-contained fresh queries
    that must not inherit a task/job ID from history even if they happen to
    be short and contain domain words like ``"running"`` or ``"failed"``.

    Example of the false-positive this guards against: after a task query,
    ``"How many pilots are running at BNL right now?"`` (9 words, contains
    ``"running"``) must *not* inherit the prior task ID.

    Args:
        question: The user's question text (caller has verified no ID present).

    Returns:
        True when the question is ≤ :data:`_MEDIUM_FOLLOWUP_WORD_LIMIT` words
        and contains a status-specific domain term.
    """
    q = question.strip()
    if not q:
        return False

    # Exclude questions that are self-contained fresh queries about pilots,
    # site health, live job statistics, or any named computing site.
    # For pilot and site-health questions, any mention of pilots is
    # unambiguous enough to exclude even without a site name.
    # For jobs-DB questions, only exclude when a site is explicitly named —
    # bare questions like "how many jobs failed?" or "top errors?" are
    # genuinely ambiguous and may be follow-ups to a task query.
    if _is_pilot_question(question):
        return False
    if _is_site_health_question(question):
        return False
    site = _extract_site_from_question(question)
    if site is not None:
        return False

    word_count = len(q.split())
    return word_count <= _MEDIUM_FOLLOWUP_WORD_LIMIT and bool(_DOMAIN_WORD_RE.search(q))


def _extract_id_from_history(
    history: Sequence[Any],
) -> tuple[int | None, int | None]:
    """Scan history backwards for the most recent task or job ID.

    Searches both user and assistant turns so that IDs mentioned in the
    assistant's answer (e.g. "Task 49375514 has 84 jobs") are also found.

    Args:
        history: Prior conversation turns in chronological order.

    Returns:
        ``(task_id, job_id)`` where each is the most recently seen integer
        ID of that type, or ``None`` if not found.
    """
    task_id: int | None = None
    job_id: int | None = None
    for msg in reversed(history):
        content = str(msg.get("content", ""))
        if task_id is None:
            task_id = _extract_task_id(content)
        if job_id is None:
            job_id = _extract_job_id(content)
        if task_id is not None and job_id is not None:
            break
    return task_id, job_id


def _last_user_question(history: list[Message]) -> str | None:
    """Return the most recent user message from history, or None.

    Args:
        history: Prior conversation turns (user/assistant pairs).

    Returns:
        Content of the last user-role message, or None if history is empty
        or contains no user messages.
    """
    for msg in reversed(history):
        if msg.get("role") == "user":
            content = str(msg.get("content", "")).strip()
            if content:
                return content
    return None


async def _bypass_response(
    question: str,
    history: list[Message],
) -> list[MCPContent]:
    """Delegate directly to the LLM passthrough tool, bypassing all routing.

    Args:
        question: The user question.
        history: Prior conversation turns.

    Returns:
        One-element MCP content list with the LLM answer.
    """
    msgs: list[Message] = []
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": question})
    delegated = await bamboo_llm_answer_tool.call(
        {"messages": msgs} if msgs else {"question": question}
    )
    if delegated and isinstance(delegated[0], dict):
        return text_content(str(delegated[0].get("text", "")))
    return text_content(str(delegated))


async def _run_topic_guard(
    question: str,
    history: list[Message],
) -> tuple[str, bool]:
    """Run the topic guard and content-free followup check.

    Returns the effective RAG query to use and whether the question was
    blocked.  The caller should return the rejection message when blocked.

    Args:
        question: The current user question.
        history: Prior conversation turns.

    Returns:
        ``(rag_query, blocked)`` where ``blocked`` is True when the topic
        guard rejected the question.  ``rag_query`` may differ from
        ``question`` when a content-free followup was reformulated.
    """
    rag_query = question
    if history and _is_content_free_followup(question):
        prior = _last_user_question(history)
        if prior:
            rag_query = prior
        async with span(EVENT_GUARD, tool="topic_guard") as _guard_span:
            _guard_span.set(allowed=True, reason="followup_allow", llm_used=False)
        return rag_query, False

    async with span(EVENT_GUARD, tool="topic_guard") as _guard_span:
        guard = await check_topic(question)
        _guard_span.set(
            allowed=guard.allowed,
            reason=guard.reason,
            llm_used=guard.llm_used,
        )
    if not guard.allowed:
        return guard.rejection_message, True
    return rag_query, False


def _resolve_contextual_ids(
    question: str,
    task_id: int | None,
    job_id: int | None,
    history: list[Message],
) -> tuple[int | None, int | None]:
    """Resolve task/job IDs for contextual follow-up questions.

    When the current question has no ID of its own but refers back to a
    prior result, scan history for the most recent ID and return it.

    Args:
        question: The current user question.
        task_id: ID already extracted from the question, or None.
        job_id: ID already extracted from the question, or None.
        history: Prior conversation turns.

    Returns:
        ``(task_id, job_id)`` — may be updated from history.
    """
    if task_id is not None or job_id is not None or not history:
        return task_id, job_id
    if _is_contextual_followup(question):
        return _extract_id_from_history(history)
    if _is_implicit_contextual_followup(question):
        hist_task, hist_job = _extract_id_from_history(history)
        if hist_task is not None or hist_job is not None:
            return hist_task, hist_job
    return task_id, job_id


# Patterns that indicate a short status-check follow-up about a specific queue.
# Matched against the lowercased question string.  All are short enough that
# they can only make sense in the context of a prior CRIC exchange.
_CRIC_FOLLOWUP_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)^\s*(?:"
    r"is\s+\S+\s+(?:active|online|offline|available|ok|up|down|brokeroff|test)"
    r"|(?:what|show)\s+(?:is|are)\s+(?:the\s+)?(?:status|state)\s+of\s+\S+"
    r"|(?:status|state)\s+of\s+\S+"
    r"|is\s+(?:that|this|it)\s+(?:queue|site)?\s*(?:active|online|offline|up|down)?"
    r"|(?:what\s+about|and)\s+\S+"
    r")\s*\??\s*$"
)

#: CRIC-specific vocabulary that, when present in any history turn, signals
#: the prior exchange involved the CRIC tool.
_CRIC_HISTORY_SIGNALS: frozenset[str] = frozenset({
    "copytool", "cric", "queuedata", "atlas_site", "brokeroff",
    "objectstore", "gfalcopy", "rucio copytool",
})


# Signals in the last assistant message that indicate a code_query response.
# All are specific enough that they only appear in code analysis answers.
_CODE_QUERY_HISTORY_SIGNALS: tuple[str, ...] = (
    "github.com/",
    "raw.githubusercontent.com",
    "source code",
    "function body",
    "def ",       # Python function definition quoted verbatim
    "import ",    # Python import quoted verbatim
    "truncated: showing",  # our truncation note
    "pilot.py",   # common enough to be specific in this context
    "pilot3",
)

# Continuation words that, combined with a repo keyword, indicate the user
# wants to continue or extend a code review already in progress.
_CODE_REVIEW_CONTINUATION_RE: re.Pattern[str] = re.compile(
    r"\b("
    r"verify|verif(?:y|ied|ication)|"
    r"full(?:\s+file|\s+source|\s+code)?|"
    r"complet(?:e|ed?|ion)|"
    r"rest(?:\s+of)?|remaining|"
    r"continu(?:e|ing|ation)|"
    r"whole(?:\s+file|\s+source|\s+code)?|"
    r"all(?:\s+of\s+it)?|"
    r"more(?:\s+of\s+it)?|"
    r"the\s+(?:rest|remaining|full|complete|whole|entire)"
    r")\b",
    re.IGNORECASE,
)

_CODE_REVIEW_REPO_KW_RE: re.Pattern[str] = re.compile(
    r"\b(pilot|bamboo|panda|file|source|code|module|script|function)\b",
    re.IGNORECASE,
)


def _is_code_review_continuation(question: str) -> bool:
    """Return True when the question is continuing an in-progress code review.

    Matches phrases like *"Please verify the full file"*, *"Show the remaining
    code"*, or *"Can I see the complete source?"* — questions that reference
    an ongoing code review but are not content-free affirmatives.  Always
    requires both a continuation word **and** a repository keyword so common
    non-code phrases like *"Full queue list please"* are not matched.

    Args:
        question: User question text.

    Returns:
        ``True`` when the question matches the code-review continuation pattern.
    """
    return (
        bool(_CODE_REVIEW_CONTINUATION_RE.search(question))
        and bool(_CODE_REVIEW_REPO_KW_RE.search(question))
    )


def _last_tool_was_code_query(history: Sequence[Any]) -> bool:
    """Return True when the most recent assistant turn was a code_query response.

    Scans the last assistant message for vocabulary that is specific to
    ``code_query`` responses — GitHub URLs, Python source fragments, or the
    truncation note — to determine whether a content-free affirmative like
    *"yes please"* should re-route to ``code_query``.

    Args:
        history: Prior conversation turns in chronological order.

    Returns:
        ``True`` if the most recent assistant message looks like a code_query
        response.
    """
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = str(msg.get("content", "")).lower()
            return any(sig.lower() in content for sig in _CODE_QUERY_HISTORY_SIGNALS)
    return False


def _extract_file_path_from_history(history: Sequence[Any]) -> str | None:
    """Extract the most recently mentioned ``*.py`` file path from history.

    Scans both user and assistant messages in reverse order and returns the
    first ``*.py`` token found.  Used to re-supply the ``file_path`` argument
    when routing a content-free affirmative follow-up back to ``code_query``.

    Args:
        history: Prior conversation turns in chronological order.

    Returns:
        The file path string (e.g. ``"pilot.py"`` or
        ``"pilot/util/processes.py"``), or ``None`` when none is found.
    """
    for msg in reversed(history):
        content = str(msg.get("content", ""))
        m = re.search(r"\b([\w][\w/]*\.py)\b", content, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _last_tool_was_cric(history: Sequence[Any]) -> bool:
    """Return True when the most recent assistant turn contains CRIC evidence.

    Scans the last assistant message in *history* for vocabulary that
    indicates the prior response came from the ``cric_query`` tool — e.g.
    copytool names, the word "cric", or queue-status terminology that only
    appears in CRIC answers.

    Args:
        history: Prior conversation turns in chronological order.

    Returns:
        ``True`` if the most recent assistant message looks like a CRIC
        query response.
    """
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = str(msg.get("content", "")).lower()
            return any(sig in content for sig in _CRIC_HISTORY_SIGNALS)
    return False


def _is_cric_followup(question: str) -> bool:
    """Return True when *question* is a short queue-status follow-up.

    Detects questions like "Is BNL-PTEST active?", "What is the status of
    CERN-PROD?", or "And that queue?" that carry no CRIC-specific keywords
    but are unambiguously about a queue status when history shows the prior
    turn was a CRIC response.

    Only matches short questions (≤ :data:`_MEDIUM_FOLLOWUP_WORD_LIMIT` + 2
    words) to avoid capturing general knowledge questions that happen to
    mention a site name.

    Args:
        question: The current user question text.

    Returns:
        ``True`` if the question looks like a CRIC status follow-up.
    """
    q = question.strip()
    if not q:
        return False
    if len(q.split()) > _MEDIUM_FOLLOWUP_WORD_LIMIT + 2:
        return False
    return bool(_CRIC_FOLLOWUP_PATTERNS.match(q))


async def _run_db_query_fast_path(
    question: str,
    history: list[Message],
    plugin_id: str = "atlas",
) -> "list[MCPContent] | None":
    """Route jobs-DB or CRIC questions to the appropriate query tool.

    Called from :func:`_run_fast_path_intercepts` when neither pilot nor
    site-health signals are present.  Handles four cases:

    1. **CRIC contextual follow-up** — the previous exchange used ``cric_query``
       and the current question is a short follow-up about a queue or site that
       appeared in that response (e.g. "Is BNL-PTEST active?" after a copytool
       query) → route to ``cric_query``.
    2. **CRIC-only signals** (``_is_cric_question`` true, ``_is_jobs_db_question``
       false) → route directly to ``cric_query``.
    3. **Jobs-DB signals with disambiguation** (``_is_jobs_db_question`` true,
       multiple DBs registered) → call ``_resolve_target_database``.  If the
       result is ``None`` (ambiguous) return a clarification response; if it
       resolves to ``"cric"`` fall through to the CRIC path; otherwise build a
       jobs plan.
    4. **Jobs-DB signals, single DB** → build a jobs plan directly.

    Args:
        question: The current user question.
        history: Prior conversation turns.
        plugin_id: Active plugin identifier; passed to
            :func:`_build_deterministic_plan` so CGSim questions route to
            ``cgsim.sim_query`` instead of ``panda_jobs_query``.

    Returns:
        A synthesised MCP content list, a clarification response, or ``None``
        if no fast-path matched.
    """
    is_jobs = _is_jobs_db_question(question)
    is_cric = _is_cric_question(question)

    # When the user replies to a clarification prompt with just a database
    # name (e.g. "cric" or "jobs"), reconstruct the original question from
    # the last user turn in history and route to the named database.
    bare_db_reply = question.strip().lower()
    if bare_db_reply in QUERYABLE_DATABASES and history:
        original = _last_user_question(history)
        if original and original.strip().lower() not in QUERYABLE_DATABASES:
            if bare_db_reply == "cric":
                cric_plan = Plan(
                    route=PlanRoute.FAST_PATH,
                    confidence=0.95,
                    tool_calls=[ToolCall(
                        tool="cric_query",
                        arguments={"question": original},
                    )],
                    reuse_policy=ReusePolicy(),
                    explain="Deterministic: user selected 'cric' after clarification → cric_query.",
                )
                return await execute_plan(cric_plan, original, history)
            if bare_db_reply == "jobs":
                jobs_plan = Plan(
                    route=PlanRoute.FAST_PATH,
                    confidence=0.95,
                    tool_calls=[ToolCall(
                        tool="panda_jobs_query",
                        arguments={"question": original},
                    )],
                    reuse_policy=ReusePolicy(),
                    explain="Deterministic: user selected 'jobs' after clarification → jobs query.",
                )
                return await execute_plan(jobs_plan, original, history)

    # CRIC contextual follow-up: if the prior exchange used cric_query, a
    # short follow-up about a queue or site should stay in CRIC even if it
    # contains no CRIC-specific keywords (e.g. "Is BNL-PTEST active?").
    if not is_jobs and not is_cric and history:
        if _last_tool_was_cric(history) and _is_cric_followup(question):
            cric_plan = Plan(
                route=PlanRoute.FAST_PATH,
                confidence=0.85,
                tool_calls=[ToolCall(
                    tool="cric_query",
                    arguments={"question": question},
                )],
                reuse_policy=ReusePolicy(),
                explain="Deterministic: CRIC contextual follow-up → cric_query.",
            )
            return await execute_plan(cric_plan, question, history)

    if is_jobs:
        if len(QUERYABLE_DATABASES) > 1:
            target_db = _resolve_target_database(question)
            if target_db is None:
                return text_content(_build_clarification_response(question))
            if target_db == "jobs":
                fast_plan = _build_deterministic_plan(question, None, None, plugin_id=plugin_id)
                if fast_plan is not None:
                    return await execute_plan(fast_plan, question, history)
                return None
            # target_db is some other DB (e.g. "cric") — fall through below.
        else:
            fast_plan = _build_deterministic_plan(question, None, None, plugin_id=plugin_id)
            if fast_plan is not None:
                return await execute_plan(fast_plan, question, history)
            return None

    if is_cric:
        fast_plan = _build_deterministic_plan(question, None, None, plugin_id=plugin_id)
        if fast_plan is not None:
            return await execute_plan(fast_plan, question, history)

    return None


async def _run_fast_path_intercepts(
    question: str,
    history: list[Message],
    plugin_id: str = "atlas",
) -> "list[MCPContent] | None":
    """Run fast-path intercepts that bypass the topic guard.

    Performs early contextual ID resolution and, when no ID is present,
    checks for unambiguous signal phrases in priority order:

    1. **PanDA server health** — liveness/health question → ``panda_server_health``.
    2. **Site health** — both pilot AND jobs signals present → calls
       ``panda_harvester_workers`` + ``panda_jobs_query`` in one plan.
    3. **Pilot only** → ``panda_harvester_workers``.
    4. **Jobs DB only** → ``panda_jobs_query``.

    The PanDA health check fires first so "is PanDA alive?" is never
    confused with a site-health or jobs question.  The combined site-health
    check must come before the pilot-only check so both tools are called.

    Returns the synthesised answer when a fast-path fires, or ``None``
    when no intercept matches and normal routing should continue.

    Args:
        question: The current user question.
        history: Prior conversation turns.
        plugin_id: Active plugin identifier; passed to
            :func:`_build_deterministic_plan` and
            :func:`_run_db_query_fast_path` so the correct plugin tools
            are selected (e.g. ``cgsim.sim_query`` for CGSim questions).

    Returns:
        ``list[MCPContent]`` if a fast-path was taken, else ``None``.
    """
    task_id_early = _extract_task_id(question)
    job_id_early = _extract_job_id(question)
    task_id_early, job_id_early = _resolve_contextual_ids(
        question, task_id_early, job_id_early, history
    )

    if not task_id_early and not job_id_early:
        # PanDA server health fast-path — highest priority before site/pilot/jobs.
        if _is_panda_health_question(question):
            plan = Plan(
                route=PlanRoute.FAST_PATH,
                confidence=0.97,
                tool_calls=[
                    ToolCall(
                        tool="panda_server_health",
                        arguments={"query": question},
                    ),
                ],
                reuse_policy=ReusePolicy(),
                explain=(
                    "Deterministic: PanDA server liveness question "
                    "→ panda_server_health."
                ),
            )
            return await execute_plan(plan, question, history)

        # Combined site-health fast-path — must be checked before the
        # individual pilot/jobs checks so both tools are called.
        if _is_site_health_question(question):
            site = _extract_site_from_question(question)
            window = _extract_time_window_from_question(question)
            pilot_args: dict[str, str] = {"question": question}
            if site:
                pilot_args["site"] = site
            if window:
                pilot_args["from_dt"], pilot_args["to_dt"] = window
            jobs_args: dict[str, str] = {"question": question}
            if site:
                jobs_args["queue"] = site
            plan = Plan(
                route=PlanRoute.FAST_PATH,
                confidence=0.95,
                tool_calls=[
                    ToolCall(
                        tool="panda_harvester_workers",
                        arguments=pilot_args,
                    ),
                    ToolCall(
                        tool="panda_jobs_query",
                        arguments=jobs_args,
                    ),
                ],
                reuse_policy=ReusePolicy(),
                explain=(
                    "Deterministic: pilot + jobs DB signals, no task/job ID "
                    "→ site health (harvester workers + jobs query)."
                ),
            )
            return await execute_plan(plan, question, history)

        # Pilot-only fast-path.
        if _is_pilot_question(question):
            fast_plan = _build_deterministic_plan(question, None, None, plugin_id=plugin_id)
            if fast_plan is not None:
                return await execute_plan(fast_plan, question, history)

        # Jobs DB fast-path and CRIC fast-path — handled by shared helper
        # that also performs multi-DB disambiguation.
        db_result = await _run_db_query_fast_path(question, history, plugin_id=plugin_id)
        if db_result is not None:
            return db_result

        # Code query follow-up: a content-free affirmative OR a code-review
        # continuation phrase after a code_query response re-routes to
        # code_query with the same file path from history.
        # Bypasses the topic guard because these carry no PanDA domain words.
        if history and _last_tool_was_code_query(history) and (
            _is_content_free_followup(question)
            or _is_code_review_continuation(question)
        ):
            file_path = _extract_file_path_from_history(history)
            cq_args: dict[str, str] = {"question": question}
            if file_path:
                cq_args["file_path"] = file_path
            plan = Plan(
                route=PlanRoute.FAST_PATH,
                confidence=0.90,
                tool_calls=[ToolCall(tool="code_query", arguments=cq_args)],
                reuse_policy=ReusePolicy(),
                explain=(
                    f"Deterministic: content-free affirmative after code_query "
                    f"→ re-fetch code_query "
                    f"({'file_path=' + file_path if file_path else 'no path'})."
                ),
            )
            return await execute_plan(plan, question, history)

    return None


class BambooAnswerTool:
    """MCP tool that answers questions about ATLAS PanDA tasks and jobs.

    Uses the LLM planner (``bamboo_plan`` with ``execute=True``) for
    routing and synthesis, replacing the previous regex-dispatch approach.
    The topic guard and ``bypass_routing`` path are preserved intact.
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for ``bamboo_answer``.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "bamboo_answer",
            "description": (
                "Answer questions about PanDA tasks, jobs, and ATLAS workflows. "
                "Automatically identifies whether the question concerns a specific "
                "task ID, job ID, log failure, or general documentation, calls the "
                "appropriate tool, and returns a synthesised natural-language answer. "
                "Use this as the single entry point for all PanDA/ATLAS questions."
            ),
            "inputSchema": {
                "type": "object",
                "anyOf": [
                    {"required": ["question"]},
                    {"required": ["messages"]}
                ],
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "User question. Required if messages is empty.",
                    },
                    "messages": {
                        "type": "array",
                        "description": "Optional full chat history as a list of {role, content}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["role", "content"],
                        },
                    },
                    "bypass_routing": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, skip task-ID extraction and send directly to LLM.",
                    },
                    "bypass_fast_path": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, skip the deterministic fast-path intercepts "
                            "(pilot, jobs DB, site-health) and fall through to the "
                            "topic guard and LLM planner.  Useful for testing planner "
                            "routing on questions that would normally be short-circuited."
                        ),
                    },
                    "include_jobs": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include job records when fetching task status (adds ?jobs=1).",
                    },
                    "include_raw": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, include a raw response preview in error output.",
                    },
                },
                "additionalProperties": False,
            },
        }

    async def call(self, arguments: dict[str, Any]) -> list[MCPContent]:
        """Handle bamboo_answer tool invocation.

        LLM provider errors are caught and returned as a friendly user-readable
        message rather than propagated as exceptions — tools must always return
        a result.

        Args:
            arguments: Tool arguments.

        Returns:
            List[MCPContent]: One-element MCP text content list.

        Raises:
            ValueError: If neither question nor messages is provided.
        """
        question: str = str(arguments.get("question", "") or "").strip()
        messages_raw: list[Any] = arguments.get("messages") or []
        messages: list[Message] = coerce_messages(messages_raw) if messages_raw else []
        bypass_routing: bool = bool(arguments.get("bypass_routing", False))
        bypass_fast_path: bool = bool(arguments.get("bypass_fast_path", False))
        include_jobs: bool = bool(arguments.get("include_jobs", True))
        include_raw: bool = bool(arguments.get("include_raw", False))

        # Derive question from the last user message if not supplied directly.
        if not question and messages:
            for msg in reversed(messages):
                if msg.get("role") == "user" and msg.get("content"):
                    question = str(msg.get("content", "")).strip()
                    break

        if not question and not messages:
            raise ValueError("Either 'question' or non-empty 'messages' must be provided.")

        try:
            plugin_id: str = os.getenv("ASKPANDA_PLUGIN", "atlas").strip().lower()
            history = _extract_history(messages, question) if messages else []
            return await self._route(
                question=question,
                history=history,
                bypass_routing=bypass_routing,
                bypass_fast_path=bypass_fast_path,
                include_jobs=include_jobs,
                include_raw=include_raw,
                plugin_id=plugin_id,
            )
        except LLMError as exc:
            return text_content(_friendly_llm_error(exc))
        except ImportError as exc:
            return text_content(_friendly_llm_error_import(exc))

    async def _route(
        self,
        question: str,
        history: list[Message],
        bypass_routing: bool,
        bypass_fast_path: bool,
        include_jobs: bool,
        include_raw: bool,
        plugin_id: str = "atlas",
    ) -> list[MCPContent]:
        """Route the question to the appropriate synthesis path.

        Args:
            question: Extracted or derived user question string.
            history: Prior conversation turns (user/assistant pairs) excluding
                the current question.
            bypass_routing: If True, skip routing and delegate directly to the
                LLM passthrough tool.
            bypass_fast_path: If True, skip the deterministic fast-path
                intercepts so the question falls through to the topic guard
                and LLM planner.  Useful for testing planner routing on
                questions that would normally be short-circuited.
            include_jobs: Passed as a hint to the planner for task-status calls.
            include_raw: Passed as a hint to the planner for error formatting.
            plugin_id: Active plugin identifier for doc tool selection and
                synthesis prompt.

        Returns:
            List[MCPContent]: One-element MCP text content list.
        """
        if bypass_routing:
            return await _bypass_response(question, history)

        # Social intercept — zero LLM cost for greetings and acknowledgements.
        if _is_greeting(question):
            return text_content(_GREETING_RESPONSE)
        if _is_ack(question):
            return text_content(_ACK_RESPONSE)

        # Fast-path intercepts — pilot and jobs DB — bypass the topic guard
        # for clearly on-topic questions.  Skipped when bypass_fast_path is
        # set so the question falls through to the topic guard and LLM planner.
        if not bypass_fast_path:
            intercept = await _run_fast_path_intercepts(question, history, plugin_id=plugin_id)
            if intercept is not None:
                return intercept

        # Topic guard + content-free followup reformulation.
        rag_query, blocked = await _run_topic_guard(question, history)
        if blocked:
            return text_content(rag_query)  # rag_query holds the rejection message

        # Extract IDs, falling back to history for contextual follow-ups.
        task_id = _extract_task_id(question)
        job_id = _extract_job_id(question)
        task_id, job_id = _resolve_contextual_ids(question, task_id, job_id, history)

        # Deterministic fast-path for ID-based and signal-based routing.
        # Skipped when bypass_fast_path is set so the LLM planner handles all
        # routing — useful for testing planner coverage.
        if not bypass_fast_path:
            fast_plan = _build_deterministic_plan(rag_query, task_id, job_id, plugin_id=plugin_id)
            if fast_plan is not None:
                original_question = question if rag_query != question else None
                return await execute_plan(
                    fast_plan, rag_query, history,
                    original_question=original_question,
                    plugin_id=plugin_id,
                )

        # LLM planner fallback for ambiguous or multi-step questions.
        hints: dict[str, Any] = {}
        if task_id:
            hints["task_id"] = task_id
        if job_id:
            hints["job_id"] = job_id
        if include_jobs:
            hints["include_jobs"] = True
        if include_raw:
            hints["include_raw"] = True

        plan_args: dict[str, Any] = {
            "question": question,
            "execute": True,
            "messages": [*history, {"role": "user", "content": question}],
            "plugin_id": plugin_id,
        }
        # Restrict the planner tool catalog to the active plugin's namespace
        # so the LLM cannot select tools from other plugins.  Without this,
        # the full catalog (including panda_job_status, panda_log_analysis,
        # etc.) is visible and the LLM picks PanDA tools for questions that
        # contain job IDs, even when the CGSim plugin is active.
        if plugin_id and plugin_id not in ("atlas", ""):
            plan_args["namespaces"] = [plugin_id]
        if hints:
            plan_args["hints"] = hints

        return await bamboo_plan_tool.call(plan_args)


bamboo_answer_tool = BambooAnswerTool()

__all__ = [
    "BambooAnswerTool",
    "bamboo_answer_tool",
    "_extract_history",
    "QUERYABLE_DATABASES",
    "_resolve_target_database",
    "_build_clarification_response",
    "_is_jobs_db_question",
    "_is_cric_question",
    "_is_conceptual_question",
    "_CONCEPTUAL_RE",
    "_is_pilot_question",
    "_is_site_health_question",
    "_is_panda_health_question",
    "_is_pilot_source_request",
    "_PILOT_SOURCE_SIGNALS",
    "_extract_site_from_question",
    "_extract_time_window_from_question",
    "_run_db_query_fast_path",
    "_last_tool_was_cric",
    "_last_tool_was_code_query",
    "_extract_file_path_from_history",
    "_is_code_query_question",
    "_is_code_review_continuation",
    "_build_code_query_plan",
    "_is_cric_followup",
    "_PILOT_SIGNALS",
    "_PILOT_DOC_PREFIXES",
    "_CRIC_SIGNALS",
    "_CRIC_HISTORY_SIGNALS",
]
