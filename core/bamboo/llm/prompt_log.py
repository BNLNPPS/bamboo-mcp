r"""Fire-and-forget prompt/response logger for OpenSearch.

Every call to :func:`~bamboo.tools.bamboo_executor.call_llm` can be logged to
an OpenSearch index for observability and analysis.  Logging is **opt-in**:
the module is a no-op unless the environment variable
``BAMBOO_OPENSEARCH_PROMPTLOG`` is set to a non-empty value (used as the HTTP
Basic-auth password for the OpenSearch cluster).

Privacy / GDPR
--------------
All text content is passed through :func:`redact_names` **before** the
document is built.  The redactor replaces potential personal identifiers
(CERN/ATLAS usernames, real names, values of known PanDA name fields) with the
token ``user_<XXXXXXXX>`` where ``XXXXXXXX`` is an 8-character lowercase hex
CRC32 digest of the original identifier.  The same identifier always maps to
the same token, so log entries remain joinable without storing the raw name.

.. warning::
    CRC32 is a non-cryptographic checksum.  An attacker who possesses the
    full CERN username list (~10 k entries) could reverse the mapping by
    exhaustive lookup in under a second.  If the OpenSearch index is ever
    accessible outside the CERN network, upgrade the hash to HMAC-SHA256
    keyed by a secret stored in a new env var (e.g.
    ``BAMBOO_PROMPTLOG_HASH_KEY``).  The code is structured to make that a
    one-line change in :func:`_crc32_token`.

Environment variables
---------------------
``BAMBOO_OPENSEARCH_PROMPTLOG``
    HTTP Basic-auth **password** for the OpenSearch cluster.  **Must be set**
    for logging to activate.  When absent the module is entirely passive.

``BAMBOO_OPENSEARCH_PROMPTLOG_INDEX``
    Base index name.  Defaults to ``bamboomcp-promptlog``.  A date suffix
    ``-YYYY.MM.DD`` is appended automatically, giving daily rollover that
    matches the ``atlas_harvesterworkers-*`` convention.

``ASKPANDA_OPENSEARCH_HOST``
    Base URL of the OpenSearch cluster.
    Default: ``https://os-atlas.cern.ch/os``

``ASKPANDA_OPENSEARCH_USER``
    HTTP Basic-auth username.  Default: ``pilot-monitor-agent``

``ASKPANDA_OPENSEARCH_CA``
    Path to the CA certificate bundle.
    Default: ``/etc/pki/tls/certs/CERN-bundle.pem``

``ASKPANDA_OPENSEARCH_VERIFY_CERTS``
    Set to ``"false"`` to disable TLS certificate verification (local
    development without the CERN CA bundle).

Document schema
---------------
Each indexed document contains::

    {
        "@timestamp":    "2026-04-17T14:33:01.123456Z",
        "session_id":    "uuid4 — stable for the process lifetime",
        "turn_number":   1,
        "provider":      "gemini",
        "model":         "gemini-2.0-flash",
        "max_tokens":    2048,
        "system_prompt": "You are AskPanDA...",
        "user_prompt":   "User question:\njobs at BNL...\n\nEvidence:...",
        "response":      "There are 42 running jobs...",
        "tools_used":    ["cric_query"],
        "input_tokens":  null,    # int when provider returns usage, else null
        "output_tokens": null,
    }

Only the current turn is stored.  Chat history is intentionally excluded —
``session_id`` + ``turn_number`` let you reconstruct the full conversation
in order, without any redundancy.  ``turn_number`` is a 1-based integer
incremented once per ``log_prompt()`` call within the process lifetime.
"""
from __future__ import annotations

import asyncio
import binascii
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-wide session ID — set once at import time.
# ---------------------------------------------------------------------------

_SESSION_ID: str = str(uuid.uuid4())

#: 1-based turn counter, incremented once per :func:`log_prompt` call.
#: Combined with ``session_id`` this gives a stable, human-readable reference
#: for any individual exchange (e.g. "session abc123, turn 3").
_turn_counter: int = 0

# ---------------------------------------------------------------------------
# Circuit breaker — disables logging after repeated write failures.
# ---------------------------------------------------------------------------

#: Number of consecutive write failures that trip the circuit breaker.
_CIRCUIT_BREAKER_THRESHOLD: int = 3

#: Consecutive failure counter.  Reset to zero on any successful write.
_consecutive_failures: int = 0

#: Set to True once the threshold is reached; cleared only on process restart.
_circuit_open: bool = False

# ---------------------------------------------------------------------------
# OpenSearch connection constants (shared with harvester_timeseries_impl.py).
# ---------------------------------------------------------------------------

_DEFAULT_HOST: str = "https://os-atlas.cern.ch/os"
_DEFAULT_USER: str = "pilot-monitor-agent"
_DEFAULT_CA: str = "/etc/pki/tls/certs/CERN-bundle.pem"
_DEFAULT_INDEX_BASE: str = "bamboomcp-promptlog"

# ---------------------------------------------------------------------------
# Redaction — privacy-preserving name pseudonymisation
# ---------------------------------------------------------------------------

# PanDA / BigPanDA JSON field names that are known to carry personal
# identifiers.  Values of these fields are *always* redacted regardless of
# their format.
_PANDA_NAME_FIELDS: frozenset[str] = frozenset({
    "prodUserName",
    "produsername",
    "username",
    "userName",
    "user_name",
    "owner",
    "submittedBy",
    "submitted_by",
    "createdBy",
    "created_by",
    "modifiedBy",
    "modified_by",
    "assignedTo",
    "assigned_to",
    "lockedBy",
    "locked_by",
    "requestedBy",
    "requested_by",
    "account",
    "dn",           # Distinguished Name — always personal
    "fullName",
    "full_name",
    "firstName",
    "first_name",
    "lastName",
    "last_name",
    "email",
    "mail",
})

# Regex: key-value pair where the key is a known PanDA name field.
# Uses \b word boundary (Python re does not support variable-width lookbehinds).
# Matches:  "prodUserName": "jsmith"
#           prodUserName: jsmith
_RE_PANDA_FIELD: re.Pattern[str] = re.compile(
    r'\b(' + "|".join(re.escape(f) for f in sorted(_PANDA_NAME_FIELDS)) + r')'
    r'("?\s*:\s*"?)([A-Za-z0-9._@/=-]{2,64})("?)',
    re.IGNORECASE,
)

# Capitalised word pairs: two consecutive title-case words not in the safe
# whitelist.  Run BEFORE contextual triggers so "John Smith" is matched as
# a unit rather than "John" being consumed alone by a trigger like "by John".
_RE_NAME_PAIR: re.Pattern[str] = re.compile(
    r'\b([A-Z][a-z]{1,20})\s+([A-Z][a-z]{1,20})\b',
)

# Contextual triggers: token that immediately follows a trigger phrase.
# The optional (?:\s+user)? handles "for user jsmith" as a single match.
_RE_CONTEXTUAL: re.Pattern[str] = re.compile(
    r'\b(?:user|for|by|owner|account|submitted\s+by|created\s+by|modified\s+by'
    r'|owned\s+by|assigned\s+to|locked\s+by)'
    r'(?:\s+user)?\s+([A-Za-z][A-Za-z0-9._-]{1,63})\b',
    re.IGNORECASE,
)

# Technical term pairs that look like title-case word pairs but are NOT names.
_SAFE_PAIRS: frozenset[str] = frozenset({
    # PanDA / ATLAS
    "Monte Carlo", "Big Panda", "BigPanda", "Grid Job", "Computing Site",
    "Task Status", "Job Status", "Error Code", "Pilot Error", "Queue Status",
    "Site Name", "Cloud Name", "Task Name", "Job Name", "Work Queue",
    "Input File", "Output File", "Log File", "Data Set", "Dataset Name",
    "Job Type", "Task Type", "Job Queue", "Job Retry",
    # Physics
    "Standard Model", "Higgs Boson", "Dark Matter", "Large Hadron",
    "Atlas Detector", "Inner Detector", "Liquid Argon",
    # General technical
    "True False", "None None",
})

# Tokens that must never be pseudonymised regardless of context.
# Note: "user" is intentionally absent — it is a contextual *trigger* word
# in _RE_CONTEXTUAL and must not be treated as a safe identifier value.
_SAFE_TOKENS: frozenset[str] = frozenset({
    # PanDA statuses
    "running", "finished", "failed", "pending", "activated", "submitted",
    "starting", "holding", "merging", "transferring", "cancelled", "broken",
    "aborted", "done", "online", "offline", "test", "brokeroff",
    # Technical tokens
    "true", "false", "null", "none", "error", "warning", "info",
    "atlas", "panda", "cern", "grid", "wlcg", "adcops",
    "mcore", "score", "managed",
    # Calendar words (avoid "submitted by April" style false positives)
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})


def _crc32_token(value: str) -> str:
    """Return a pseudonym token for *value* using CRC32.

    The same *value* always produces the same token, so pseudonymised log
    entries remain joinable.  The token format is ``user_XXXXXXXX`` where
    ``XXXXXXXX`` is the 8-character zero-padded lowercase hex CRC32.

    Args:
        value: The raw identifier to pseudonymise.

    Returns:
        Pseudonym string in the form ``user_XXXXXXXX``.
    """
    checksum = binascii.crc32(value.encode("utf-8")) & 0xFFFFFFFF
    return f"user_{checksum:08x}"


def redact_names(text: str) -> str:
    """Replace potential personal identifiers in *text* with CRC32 pseudonyms.

    Applies three redaction passes in order:

    1. **PanDA field values** — values of known name-carrying JSON fields
       (``prodUserName``, ``owner``, ``email``, etc.).
    2. **Capitalised word pairs** — two consecutive title-case words not in
       the technical-term whitelist (catches "John Smith" as a unit before
       any contextual pass can split it).
    3. **Contextual triggers** — tokens that immediately follow words such as
       ``"user"``, ``"for"``, ``"submitted by"``, ``"owned by"``, etc.
       Handles the pattern ``"for user jsmith"`` via an optional intermediate
       ``user`` token in the regex.

    Tokens present in ``_SAFE_TOKENS`` are never replaced.  Tokens already
    in ``user_[0-9a-f]{8}`` form are left unchanged.

    Args:
        text: Raw text string to redact (may be serialised JSON, a plain-text
              prompt, or a response string).

    Returns:
        Copy of *text* with personal identifiers replaced by ``user_XXXXXXXX``
        tokens.
    """
    if not text:
        return text

    # Pass 1: structured PanDA field values.
    def _replace_panda_field(m: re.Match[str]) -> str:
        field, sep, value, close = m.group(1), m.group(2), m.group(3), m.group(4)
        if value.lower() in _SAFE_TOKENS:
            return m.group(0)
        return f"{field}{sep}{_crc32_token(value)}{close}"

    text = _RE_PANDA_FIELD.sub(_replace_panda_field, text)

    # Pass 2: capitalised word pairs — run before contextual so "John Smith"
    # is matched as a whole before "by John" can consume "John" alone.
    def _replace_name_pair(m: re.Match[str]) -> str:
        first, last = m.group(1), m.group(2)
        pair = f"{first} {last}"
        if pair in _SAFE_PAIRS:
            return pair
        if first.lower() in _SAFE_TOKENS or last.lower() in _SAFE_TOKENS:
            return pair
        return f"{_crc32_token(first)} {_crc32_token(last)}"

    text = _RE_NAME_PAIR.sub(_replace_name_pair, text)

    # Pass 3: contextual triggers ("user jsmith", "for jsmith", …).
    def _replace_contextual(m: re.Match[str]) -> str:
        prefix = m.group(0)[: m.start(1) - m.start(0)]
        value = m.group(1)
        if value.lower() in _SAFE_TOKENS:
            return m.group(0)
        if re.fullmatch(r"user_[0-9a-f]{8}", value):
            return m.group(0)
        return prefix + _crc32_token(value)

    text = _RE_CONTEXTUAL.sub(_replace_contextual, text)

    return text

# ---------------------------------------------------------------------------
# OpenSearch helpers
# ---------------------------------------------------------------------------


def _is_logging_enabled() -> bool:
    """Return True when the prompt-log password env var is set.

    Returns:
        True if ``BAMBOO_OPENSEARCH_PROMPTLOG`` is set to a non-empty value.
    """
    return bool(os.environ.get("BAMBOO_OPENSEARCH_PROMPTLOG", ""))


def _build_index_name() -> str:
    """Return today's prompt-log index name with a UTC date suffix.

    Format: ``<base>-YYYY.MM.DD``, e.g. ``bamboomcp-promptlog-2026.04.17``.

    Returns:
        Index name string for today's UTC date.
    """
    base = os.environ.get("BAMBOO_OPENSEARCH_PROMPTLOG_INDEX", _DEFAULT_INDEX_BASE)
    today = datetime.now(tz=timezone.utc).strftime("%Y.%m.%d")
    return f"{base}-{today}"


def _create_os_client() -> Any:
    """Create an authenticated OpenSearch client for prompt logging.

    Reads connection parameters from the same environment variables used by
    :mod:`~askpanda_atlas.harvester_timeseries_impl` so that operators only
    need one set of credentials.

    Returns:
        An :class:`opensearchpy.OpenSearch` client instance.

    Raises:
        ImportError: If ``opensearch-py`` is not installed.
        RuntimeError: If ``BAMBOO_OPENSEARCH_PROMPTLOG`` is not set.
    """
    from opensearchpy import OpenSearch  # optional dep — guarded at call site

    password = os.environ.get("BAMBOO_OPENSEARCH_PROMPTLOG", "")
    if not password:
        raise RuntimeError(
            "BAMBOO_OPENSEARCH_PROMPTLOG is not set — prompt logging is disabled."
        )

    host = os.environ.get("ASKPANDA_OPENSEARCH_HOST", _DEFAULT_HOST)
    user = os.environ.get("ASKPANDA_OPENSEARCH_USER", _DEFAULT_USER)
    ca = os.environ.get("ASKPANDA_OPENSEARCH_CA", _DEFAULT_CA)
    verify_raw = os.environ.get("ASKPANDA_OPENSEARCH_VERIFY_CERTS", "true").lower()
    verify = verify_raw != "false"

    client_kwargs: dict[str, Any] = {
        "hosts": [host],
        "http_auth": (user, password),
        "use_ssl": True,
        "verify_certs": verify,
    }
    if verify and os.path.exists(ca):
        client_kwargs["ca_certs"] = ca

    return OpenSearch(**client_kwargs)


def _write_document(doc: dict[str, Any]) -> None:
    """Write *doc* to OpenSearch synchronously.

    Intended to be called from a background thread via
    :func:`asyncio.to_thread` so it never blocks the event loop.

    Implements a simple circuit breaker: after
    :data:`_CIRCUIT_BREAKER_THRESHOLD` consecutive failures the circuit is
    opened and all subsequent calls return immediately with a single ``ERROR``
    log line.  The counter resets to zero on any successful write.

    Args:
        doc: Fully-built document dict to index.
    """
    global _consecutive_failures, _circuit_open  # pylint: disable=global-statement

    if _circuit_open:
        return

    try:
        client = _create_os_client()
        index = _build_index_name()
        client.index(index=index, body=doc)
        # Success — reset failure counter.
        _consecutive_failures = 0
    except ImportError:
        logger.debug("prompt_log: opensearch-py not installed — skipping log write")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _consecutive_failures += 1
        if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            _circuit_open = True
            logger.error(
                "prompt_log: circuit breaker tripped after %d consecutive "
                "write failures — prompt logging disabled for this session. "
                "Check BAMBOO_OPENSEARCH_PROMPTLOG credentials and write "
                "access to index '%s'. Last error: %s",
                _consecutive_failures,
                _build_index_name(),
                exc,
            )
        else:
            logger.warning(
                "prompt_log: write failure %d/%d — %s",
                _consecutive_failures,
                _CIRCUIT_BREAKER_THRESHOLD,
                exc,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def log_prompt(
    system_prompt: str,
    user_prompt: str,
    response: str,
    tools_used: list[str],
    provider: str,
    model: str,
    max_tokens: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Fire-and-forget: build a redacted document and ship it to OpenSearch.

    Returns immediately after scheduling the write as an
    :func:`asyncio.create_task`.  The main request pipeline is never blocked
    by the OpenSearch write.

    Only the current turn is stored — chat history is deliberately excluded.
    ``session_id`` + ``turn_number`` are sufficient to reconstruct a full
    conversation in order.

    If prompt logging is disabled (``BAMBOO_OPENSEARCH_PROMPTLOG`` not set)
    this function is a no-op and returns in microseconds.

    Args:
        system_prompt: The system prompt string for this call, before redaction.
        user_prompt: The synthesised user prompt for this call (contains the
            question and injected evidence), before redaction.
        response: Raw LLM response text, before redaction.
        tools_used: Names of the MCP tools called during this turn (e.g.
            ``["cric_query"]``).
        provider: LLM provider string (e.g. ``"gemini"``).
        model: LLM model string (e.g. ``"gemini-2.0-flash"``).
        max_tokens: ``max_tokens`` value passed to the LLM for this call.
        input_tokens: Input token count from the LLM usage object, or
            ``None`` when unavailable.
        output_tokens: Output token count from the LLM usage object, or
            ``None`` when unavailable.
    """
    if not _is_logging_enabled():
        return

    global _turn_counter  # pylint: disable=global-statement
    _turn_counter += 1
    turn_number = _turn_counter
    timestamp = datetime.now(tz=timezone.utc).isoformat()

    doc: dict[str, Any] = {
        "@timestamp": timestamp,
        "session_id": _SESSION_ID,
        "turn_number": turn_number,
        "provider": provider,
        "model": model,
        "max_tokens": max_tokens,
        "system_prompt": redact_names(system_prompt),
        "user_prompt": redact_names(user_prompt),
        "response": redact_names(response),
        "tools_used": tools_used,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

    # asyncio.to_thread: synchronous OS client never blocks the event loop.
    # create_task: caller gets control back immediately (fire-and-forget).
    asyncio.create_task(  # noqa: RUF006 — intentional fire-and-forget
        asyncio.to_thread(_write_document, doc),
        name=f"prompt_log_{_SESSION_ID[:8]}_{turn_number}",
    )


__all__ = [
    "log_prompt",
    "redact_names",
    "_SESSION_ID",
    "_DEFAULT_INDEX_BASE",
    "_CIRCUIT_BREAKER_THRESHOLD",
    "_turn_counter",
]
