"""Implementation of ``panda_jobs_query`` — natural-language to SQL for the jobs DB.

Translates a natural-language question into a DuckDB SQL query, validates the
generated SQL through the AST guard in :mod:`askpanda_atlas.jobs_query_schema`,
executes it against the read-only ingestion DuckDB file, and returns a compact
evidence dict structured for LLM synthesis.

Public surface (mirrors ``task_status_impl`` pattern exactly):

- ``get_definition()``         — MCP tool definition dict
- ``PandaJobsQueryTool``       — MCP tool class with ``get_definition()`` and
  async ``call()``
- ``panda_jobs_query_tool``    — singleton instance

Design rules (per Bamboo architecture):

* All ``bamboo.tools.base`` and ``bamboo.llm.*`` imports are **deferred inside
  ``call()`` and helpers** — never at module level.  This keeps every pure
  helper importable without bamboo installed.
* The LLM call is async (``await client.generate(...)``); DuckDB execution
  runs synchronously on the event loop thread — queries are fast (2-15ms)
  and this avoids thread-pool conflicts with DuckDB on macOS.
* ``call()`` never raises — errors are returned as ``text_content`` payloads.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

try:
    import duckdb
except ImportError as _duckdb_import_error:  # pragma: no cover
    raise ImportError(
        "panda_jobs_query requires the 'duckdb' package. "
        "Install it with: pip install duckdb"
    ) from _duckdb_import_error

logger = logging.getLogger(__name__)


def _serialise_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DuckDB result row to a JSON-safe dict.

    DuckDB returns Python ``datetime``, ``date``, and ``Decimal`` objects
    for the corresponding column types.  These are not JSON-serialisable by
    default, so they are converted to strings here before the evidence dict
    is passed to ``json.dumps``.

    Args:
        row: Raw row dict from DuckDB with Python-typed values.

    Returns:
        Row dict with all values converted to JSON-safe types.
    """
    import datetime
    import decimal

    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
            out[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _db_mtime(duckdb_path: str) -> str | None:
    """Return the database file's last-modified time as a compact UTC string.

    Used to populate ``db_last_modified`` in evidence dicts so the TUI can
    display a "Database last updated" footnote without an extra SQL query.

    Args:
        duckdb_path: Filesystem path to the DuckDB file.

    Returns:
        ISO-style UTC timestamp string (e.g. ``"2026-04-07 10:31 UTC"``), or
        ``None`` when the path is ``":memory:"``, empty, or the file is absent.
    """
    import datetime  # deferred — only called at query time
    if not duckdb_path or duckdb_path == ":memory:":
        return None
    try:
        mtime = os.path.getmtime(duckdb_path)
        return datetime.datetime.fromtimestamp(
            mtime, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except OSError:
        return None


def _execute_query(
    duckdb_path: str,
    sql: str,
    timeout_secs: int,
    max_rows: int,
) -> dict[str, Any]:
    """Open the database, execute *sql*, and return rows plus metadata.

    Opens a fresh connection on the calling thread (required for DuckDB thread
    safety — connections must not be shared across threads), executes the query
    with a timeout, caps results at *max_rows*, and closes the connection before
    returning.

    On-disk paths are opened with ``read_only=True`` so this process can coexist
    with the jobs-ingestion agent writer without triggering DuckDB's single-writer
    lock.  In-memory paths (``":memory:"``) are opened read-write so tests can
    populate fixture data before querying.

    Args:
        duckdb_path: Filesystem path to the DuckDB file, or ``":memory:"``.
        sql: Validated SQL string ready for execution.
        timeout_secs: Maximum execution time in seconds.
        max_rows: Maximum rows to return.

    Returns:
        Dict with keys ``columns``, ``rows``, ``row_count``, ``truncated``,
        and ``execution_time_ms``.

    Raises:
        Exception: Any DuckDB error is re-raised so the caller can wrap it
            in a structured error response.
    """
    conn = duckdb.connect(database=duckdb_path, read_only=(duckdb_path != ":memory:"))
    try:
        try:
            conn.execute(f"SET statement_timeout='{timeout_secs}s'")
        except duckdb.Error:
            pass

        t0 = time.monotonic()
        result = conn.execute(sql)
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        columns: list[str] = [d[0] for d in (result.description or [])]
        raw_rows = result.fetchmany(max_rows + 1)
        truncated = len(raw_rows) > max_rows
        rows = [_serialise_row(dict(zip(columns, row))) for row in raw_rows[:max_rows]]

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "execution_time_ms": round(elapsed_ms, 2),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SQL string helpers (synchronous, no I/O)
# ---------------------------------------------------------------------------


def _strip_sql_fences(raw: str) -> str:
    """Remove markdown code fences from *raw*, returning the inner SQL.

    Handles both ````sql ... ```` and plain ```` ``` ... ```` wrappers.
    Leading and trailing whitespace is stripped from the result.

    Args:
        raw: Raw string as returned by the LLM.

    Returns:
        SQL string with code fences removed.
    """
    text = raw.strip()
    for fence_open in ("```sql", "```SQL", "```"):
        if text.startswith(fence_open):
            text = text[len(fence_open):]
            if text.endswith("```"):
                text = text[:-3]
            break
    return text.strip()


def _looks_like_cannot_answer(text: str) -> bool:
    """Return ``True`` when the LLM signals it cannot produce SQL.

    Args:
        text: Stripped LLM reply text.

    Returns:
        ``True`` if the reply matches the cannot-answer sentinel or a
        plausible natural-language refusal.
    """
    from askpanda_atlas.jobs_query_schema import CANNOT_ANSWER_SENTINEL

    if text.upper() == CANNOT_ANSWER_SENTINEL:
        return True
    lower = text.lower()
    refusal_phrases = (
        "i cannot", "i can't", "i don't know", "i do not know",
        "unable to", "cannot generate", "cannot answer", "not possible",
        "no sql", "i'm sorry",
    )
    return any(phrase in lower for phrase in refusal_phrases)


# ---------------------------------------------------------------------------
# Async LLM call
# ---------------------------------------------------------------------------


async def _call_llm_for_sql(
    question: str,
    schema_context: str,
) -> str:
    """Call the configured Bamboo LLM and return its raw reply.

    Uses the ``"synthesize"`` task profile (default model) at temperature 0.0
    with a tight token cap to minimise hallucination.

    Args:
        question: Natural-language question from the user.
        schema_context: Pre-built schema context string for the prompt.

    Returns:
        Raw reply string from the LLM (may contain SQL, fences, or a refusal).

    Raises:
        RuntimeError: If the LLM manager or selector is not initialised.
    """
    from bamboo.llm.runtime import get_llm_manager, get_llm_selector  # deferred
    from bamboo.llm.types import GenerateParams, Message  # deferred
    from askpanda_atlas.jobs_query_schema import build_sql_prompt

    selector = get_llm_selector()
    manager = get_llm_manager()

    registry = getattr(selector, "registry", None)
    if registry is None:
        raise RuntimeError("LLM selector does not expose a registry.")

    default_profile = getattr(selector, "default_profile", "default")
    model_spec = registry.get(default_profile)
    client = await manager.get_client(model_spec)

    messages_raw = build_sql_prompt(question, schema_context)
    messages: list[Message] = [
        {"role": m["role"], "content": m["content"]}
        for m in messages_raw
    ]

    resp = await client.generate(
        messages=messages,
        params=GenerateParams(temperature=0.0, max_tokens=512),
    )
    return resp.text


# ---------------------------------------------------------------------------
# Main pipeline (async)
# ---------------------------------------------------------------------------


async def fetch_and_analyse(
    question: str,
    duckdb_path: str,
) -> dict[str, Any]:
    """Translate *question* to SQL, guard it, execute it, and return evidence.

    This is the end-to-end pipeline:

    1. Build schema context (cached).
    2. Call the LLM (async) to generate SQL.
    3. Strip markdown fences.
    4. Detect "cannot answer" replies.
    5. Run :func:`~askpanda_atlas.jobs_query_schema.validate_and_guard`.
    6. Execute the sanitised SQL in a thread (blocking DuckDB call).
    7. Build and return the evidence dict.

    Every failure mode produces a structured evidence dict (never an
    exception) so the Bamboo executor always receives a usable payload.

    Args:
        question: Natural-language question from the user.
        duckdb_path: Filesystem path to the DuckDB file (or ``":memory:"``
            for tests).

    Returns:
        Evidence dictionary with keys: ``question``, ``sql``, ``columns``,
        ``rows``, ``row_count``, ``truncated``, ``execution_time_ms``,
        ``db_path``, ``error``, ``guard_rejection``, ``raw_payload``.
    """
    from askpanda_atlas.jobs_query_schema import (
        MAX_ROWS,
        QUERY_TIMEOUT_SECS,
        build_schema_context,
        validate_and_guard,
    )

    schema_context = build_schema_context()

    # --- Stage 2-4: async LLM SQL generation --------------------------------
    try:
        raw_reply = await _call_llm_for_sql(question, schema_context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("panda_jobs_query: LLM call failed")
        return _execution_error_evidence(
            question=question,
            sql=None,
            duckdb_path=duckdb_path,
            detail=f"LLM call failed: {exc}",
        )

    raw_sql = _strip_sql_fences(raw_reply)
    if not raw_sql or _looks_like_cannot_answer(raw_sql):
        logger.debug("panda_jobs_query: LLM declined to generate SQL: %r", raw_reply[:120])
        return _unable_to_answer_evidence(question, duckdb_path)

    logger.debug("panda_jobs_query: generated SQL: %s", raw_sql)

    # --- Stage 5: guard -----------------------------------------------------
    guard = validate_and_guard(raw_sql)
    if not guard.passed:
        logger.warning(
            "panda_jobs_query: guard rejected SQL (rule=%s): %s",
            guard.triggered_rule,
            raw_sql,
        )
        return _guard_rejected_evidence(
            question=question,
            raw_sql=raw_sql,
            reason=guard.rejection_reason or "Unknown guard violation.",
            rule=guard.triggered_rule or "unknown",
            duckdb_path=duckdb_path,
        )

    sanitised_sql: str = guard.sanitised_sql  # type: ignore[assignment]

    # --- Stage 6: execute synchronously ------------------------------------
    # DuckDB queries against the ingestion database are fast (typically 2-15ms)
    # so running them directly on the event loop thread is safe and avoids
    # asyncio.to_thread thread-pool issues with DuckDB on macOS.
    try:
        exec_result = _execute_query(
            duckdb_path, sanitised_sql, QUERY_TIMEOUT_SECS, MAX_ROWS
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("panda_jobs_query: execution error: %s", exc)
        return _execution_error_evidence(
            question=question,
            sql=sanitised_sql,
            duckdb_path=duckdb_path,
        )

    logger.debug(
        "panda_jobs_query: query returned %d rows (truncated=%s) in %.1f ms",
        exec_result["row_count"],
        exec_result["truncated"],
        exec_result["execution_time_ms"],
    )

    return {
        "question": question,
        "sql": sanitised_sql,
        "columns": exec_result["columns"],
        "rows": exec_result["rows"],
        "row_count": exec_result["row_count"],
        "truncated": exec_result["truncated"],
        "execution_time_ms": exec_result["execution_time_ms"],
        "db_path": duckdb_path,
        "db_last_modified": _db_mtime(duckdb_path),
        "error": None,
        "guard_rejection": None,
        "raw_payload": None,
    }


# ---------------------------------------------------------------------------
# Structured error constructors
# ---------------------------------------------------------------------------


def _unable_to_answer_evidence(question: str, duckdb_path: str) -> dict[str, Any]:
    """Return a structured evidence dict for the 'LLM cannot answer' case.

    Args:
        question: The original user question.
        duckdb_path: Path to the DuckDB file.

    Returns:
        Evidence dict with a user-safe error message.
    """
    return {
        "question": question,
        "sql": None,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "execution_time_ms": 0.0,
        "db_path": duckdb_path,
        "error": (
            "I wasn't able to translate that question into a database query. "
            "Try rephrasing with a specific site name or job status."
        ),
        "guard_rejection": None,
        "raw_payload": None,
    }


def _guard_rejected_evidence(
    question: str,
    raw_sql: str,
    reason: str,
    rule: str,
    duckdb_path: str,
) -> dict[str, Any]:
    """Return a structured evidence dict for a guard rejection.

    Args:
        question: The original user question.
        raw_sql: The SQL string that triggered the rejection.
        reason: Human-readable rejection reason.
        rule: Short rule identifier that triggered rejection.
        duckdb_path: Path to the DuckDB file.

    Returns:
        Evidence dict with a user-safe error message and guard details.
    """
    return {
        "question": question,
        "sql": raw_sql,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "execution_time_ms": 0.0,
        "db_path": duckdb_path,
        "error": "That query isn't permitted by this tool. Only read-only lookups are supported.",
        "guard_rejection": f"[{rule}] {reason}",
        "raw_payload": None,
    }


def _execution_error_evidence(
    question: str,
    sql: str | None,
    duckdb_path: str,
    detail: str = "",
) -> dict[str, Any]:
    """Return a structured evidence dict for a query execution error.

    The raw database error is intentionally not included in the user-facing
    message to avoid leaking schema information.

    Args:
        question: The original user question.
        sql: The SQL that was attempted (or None if the error preceded execution).
        duckdb_path: Path to the DuckDB file.
        detail: Optional internal detail logged but not shown to the user.

    Returns:
        Evidence dict with a user-safe error message.
    """
    if detail:
        logger.debug("panda_jobs_query execution error detail: %s", detail)
    return {
        "question": question,
        "sql": sql,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "execution_time_ms": 0.0,
        "db_path": duckdb_path,
        "error": (
            "The query could not be executed. "
            "Try a more specific question or check that the database is available."
        ),
        "guard_rejection": None,
        "raw_payload": None,
    }


# ---------------------------------------------------------------------------
# MCP tool definition
# ---------------------------------------------------------------------------


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for ``panda_jobs_query``.

    Returns:
        Tool definition dict compatible with MCP discovery.
    """
    return {
        "name": "panda_jobs_query",
        "description": (
            "Answer natural-language questions about PanDA jobs by querying "
            "the local ingestion database. "
            "Use this tool when the user asks about job counts, statuses, "
            "errors, or resource usage at a specific computing site — for "
            "example: 'How many jobs failed at BNL?', 'What are the top "
            "errors at SWT2_CPB?', 'Which jobs are running at CERN?'. "
            "The database reflects jobs active at each site within "
            "approximately the last hour."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Natural-language question about PanDA jobs, e.g. "
                        "'How many jobs failed at BNL in the last hour?'"
                    ),
                },
                "queue": {
                    "type": "string",
                    "description": (
                        "Optional: computing site / queue to scope the query "
                        "(e.g. 'BNL', 'CERN_PROD'). If supplied, it is "
                        "appended to the question so the LLM uses it as a "
                        "filter hint."
                    ),
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class PandaJobsQueryTool:
    """MCP tool that answers NL questions about PanDA jobs via DuckDB SQL.

    Translates the user's natural-language question into a single SELECT
    statement, validates it through the AST guard, executes it against the
    read-only ingestion database, and returns a compact evidence dict
    structured for LLM synthesis.
    """

    def __init__(self) -> None:
        """Initialise with the cached tool definition."""
        self._def: dict[str, Any] = get_definition()

    def get_definition(self) -> dict[str, Any]:
        """Return the MCP tool definition.

        Returns:
            Tool definition dictionary.
        """
        return self._def

    async def call(self, arguments: dict[str, Any]) -> list[Any]:
        """Translate the question to SQL, execute it, and return evidence.

        The LLM call is awaited directly; only the blocking DuckDB execution
        is offloaded via ``asyncio.to_thread`` (inside ``fetch_and_analyse``).
        ``bamboo.tools.base`` is imported inside this method (deferred) so the
        rest of this module remains importable when bamboo core is not installed.

        Args:
            arguments: Dict with required ``"question"`` (str) and optional
                ``"queue"`` (str).

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence dict, or an error payload if anything goes wrong.
        """
        from bamboo.tools.base import text_content  # deferred — see module docstring

        question: str = arguments.get("question", "").strip()
        if not question:
            return text_content(json.dumps({
                "evidence": {"error": "question argument is required."},
            }))

        if len(question) > 2000:
            return text_content(json.dumps({
                "evidence": {
                    "error": "Question is too long (max 2000 characters). Please be more concise.",
                },
            }))

        queue: str | None = arguments.get("queue")
        if queue:
            question = f"{question} (filter _queue ILIKE '{queue}%')"

        duckdb_path: str = os.environ.get("PANDA_DUCKDB_PATH", "jobs.duckdb")

        try:
            evidence = await fetch_and_analyse(question, duckdb_path)
            return text_content(json.dumps({"evidence": evidence}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("panda_jobs_query tool call failed")
            return text_content(json.dumps({
                "evidence": {
                    "question": question,
                    "error": repr(exc),
                },
            }))


panda_jobs_query_tool = PandaJobsQueryTool()

__all__ = [
    "PandaJobsQueryTool",
    "fetch_and_analyse",
    "get_definition",
    "panda_jobs_query_tool",
]
