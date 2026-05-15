"""Implementation of ``cgsim.sim_query`` — natural-language to SQL for the CGSim DB.

Translates a natural-language question into a SQLite SQL query, validates the
generated SQL through the AST guard in :mod:`askcgsim.sim_query_schema`,
executes it against the read-only CGSim SQLite file, then makes a second LLM
call to summarise the raw results in natural language.

Public surface:

- ``get_definition()``        — MCP tool definition dict
- ``CgsimSimQueryTool``       — MCP tool class with ``get_definition()`` and
  async ``call()``
- ``cgsim_sim_query_tool``    — singleton instance

Design rules (per Bamboo architecture):

* All ``bamboo.tools.base`` and ``bamboo.llm.*`` imports are **deferred inside
  ``call()`` and helpers** — never at module level.  This keeps every pure
  helper importable without bamboo installed.
* Both LLM calls are async (``await client.generate(...)``).  SQLite execution
  runs synchronously on the event loop thread — queries are fast (< 20 ms for
  typical CGSim databases) and ``asyncio.to_thread`` is deliberately avoided
  (consistent with the DuckDB precedent in ``jobs_query_impl``).
* ``call()`` never raises — errors are returned as ``text_content`` payloads.

Security model (four independent layers):
1. SQLite URI ``?mode=ro`` — OS-level write refusal.
2. ``PRAGMA query_only = ON`` — SQLite library-level write refusal.
3. sqlglot AST guard (``validate_and_guard``) — LLM SQL validated against
   allow-list before execution.
4. Local-only deployment — ``CGSIM_DB_PATH`` must be a local filesystem path.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQLite execution helpers
# ---------------------------------------------------------------------------


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Open a CGSim SQLite database in strict read-only mode.

    Uses the SQLite URI filename format with ``?mode=ro`` (layer 1) and
    immediately issues ``PRAGMA query_only = ON`` (layer 2) to prevent any
    write operations at both the driver and library levels.

    Args:
        db_path: Filesystem path to the CGSim SQLite file.

    Returns:
        Open :class:`sqlite3.Connection` in read-only mode.

    Raises:
        sqlite3.OperationalError: If the file does not exist or cannot be
            opened in read-only mode.
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _execute_query(
    db_path: str,
    sql: str,
    timeout_secs: int,
    max_rows: int,
) -> dict[str, Any]:
    """Open the database, execute *sql*, and return rows plus metadata.

    Opens a fresh read-only connection on the calling thread, executes the
    validated query, caps results at *max_rows* + 1 to detect truncation, and
    closes the connection before returning.

    Args:
        db_path: Filesystem path to the CGSim SQLite file.
        sql: Validated SQL string ready for execution.
        timeout_secs: Maximum execution time in seconds; applied via
            ``connection.set_progress_handler``.
        max_rows: Maximum rows to return (Python-side cap).

    Returns:
        Dict with keys ``columns``, ``rows``, ``row_count``, ``truncated``,
        and ``execution_time_ms``.

    Raises:
        Exception: Any SQLite error is re-raised so the caller can wrap it
            in a structured error response.
    """
    conn = _open_readonly(db_path)
    try:
        # Enforce a query timeout via SQLite's progress handler.
        deadline = time.monotonic() + timeout_secs
        check_count = 0

        def _timeout_handler() -> None:
            nonlocal check_count
            check_count += 1
            if check_count % 1000 == 0 and time.monotonic() > deadline:
                raise TimeoutError(
                    f"Query exceeded the {timeout_secs} s time limit."
                )

        conn.set_progress_handler(_timeout_handler, 100)

        t0 = time.monotonic()
        cursor = conn.execute(sql)
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        columns: list[str] = [d[0] for d in (cursor.description or [])]
        raw_rows = cursor.fetchmany(max_rows + 1)
        truncated = len(raw_rows) > max_rows
        rows = [dict(row) for row in raw_rows[:max_rows]]

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
    r"""Remove markdown code fences from *raw*, returning the inner SQL.

    Handles both ` ```sql ... ``` ` and plain ` ``` ... ``` ` wrappers.
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
    r"""Return ``True`` when the LLM signals it cannot produce SQL.

    Args:
        text: Stripped LLM reply text.

    Returns:
        ``True`` if the reply matches the cannot-answer sentinel or a
        plausible natural-language refusal.
    """
    from askcgsim.sim_query_schema import CANNOT_ANSWER_SENTINEL  # deferred

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
# Async LLM helpers
# ---------------------------------------------------------------------------


async def _get_llm_client() -> Any:
    """Resolve and return the configured Bamboo LLM client.

    Imports ``bamboo.llm.runtime`` lazily to keep this module importable
    without bamboo installed.

    Returns:
        A Bamboo LLM client instance with a ``generate`` coroutine.

    Raises:
        RuntimeError: If the LLM manager or selector is not initialised.
    """
    from bamboo.llm.runtime import get_llm_manager, get_llm_selector  # deferred

    selector = get_llm_selector()
    manager = get_llm_manager()

    registry = getattr(selector, "registry", None)
    if registry is None:
        raise RuntimeError("LLM selector does not expose a registry.")

    default_profile = getattr(selector, "default_profile", "default")
    model_spec = registry.get(default_profile)
    return await manager.get_client(model_spec)


async def _call_llm_for_sql(question: str, schema_context: str) -> str:
    """Call the configured Bamboo LLM and return its raw SQL reply.

    Uses temperature 0.0 and a tight token cap to minimise hallucination.

    Args:
        question: Natural-language question from the user.
        schema_context: Pre-built schema context string for the prompt.

    Returns:
        Raw reply string from the LLM (may contain SQL, fences, or a refusal).

    Raises:
        RuntimeError: If the LLM manager or selector is not initialised.
    """
    from bamboo.llm.types import GenerateParams, Message  # deferred
    from bamboo.tracing import EVENT_LLM_CALL, span  # deferred
    from askcgsim.sim_query_schema import build_sql_prompt  # deferred

    client = await _get_llm_client()

    messages_raw = build_sql_prompt(question, schema_context)
    messages: list[Message] = [
        {"role": m["role"], "content": m["content"]}
        for m in messages_raw
    ]

    async with span(
        EVENT_LLM_CALL,
        tool="cgsim.sim_query/sql_generation",
        provider=getattr(client, "provider", ""),
        model=getattr(client, "model", ""),
    ) as s:
        resp = await client.generate(
            messages=messages,
            params=GenerateParams(temperature=0.0, max_tokens=1024),
        )
        usage = resp.usage
        s.set(
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
        )

    return resp.text


async def _call_llm_for_summary(
    question: str,
    sql: str,
    results_json: str,
) -> str:
    """Call the configured Bamboo LLM and return a natural-language summary.

    Args:
        question: The original natural-language question from the user.
        sql: The SQL query that was executed.
        results_json: JSON string of the query results (rows).

    Returns:
        Natural-language summary string from the LLM.

    Raises:
        RuntimeError: If the LLM manager or selector is not initialised.
    """
    from bamboo.llm.types import GenerateParams, Message  # deferred
    from bamboo.tracing import EVENT_LLM_CALL, span  # deferred
    from askcgsim.sim_query_schema import build_summarise_prompt  # deferred

    client = await _get_llm_client()

    messages_raw = build_summarise_prompt(question, sql, results_json)
    messages: list[Message] = [
        {"role": m["role"], "content": m["content"]}
        for m in messages_raw
    ]

    async with span(
        EVENT_LLM_CALL,
        tool="cgsim.sim_query/summarisation",
        provider=getattr(client, "provider", ""),
        model=getattr(client, "model", ""),
    ) as s:
        resp = await client.generate(
            messages=messages,
            params=GenerateParams(temperature=0.2, max_tokens=1024),
        )
        usage = resp.usage
        s.set(
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
        )

    return resp.text


def _emit_sqlite_span(execution_time_ms: float, row_count: int, truncated: bool) -> None:
    """Emit a best-effort tracing span for the SQLite execution step.

    No-op when tracing is disabled or bamboo is not installed.  Never raises —
    tracing must never block the main pipeline.

    Args:
        execution_time_ms: Query execution time in milliseconds.
        row_count: Number of rows returned.
        truncated: Whether the result was capped at MAX_ROWS.
    """
    try:
        from bamboo.tracing import EVENT_TOOL_CALL, emit_sync  # deferred
        emit_sync(
            EVENT_TOOL_CALL,
            tool="cgsim.sim_query/sqlite_execute",
            duration_ms=execution_time_ms,
            row_count=row_count,
            truncated=truncated,
        )
    except Exception:  # noqa: BLE001
        pass


async def fetch_and_analyse(
    question: str,
    db_path: str,
) -> dict[str, Any]:
    """Translate *question* to SQL, guard it, execute it, summarise, and return evidence.

    This is the end-to-end pipeline:

    1. Build schema context (cached in-process).
    2. Call the LLM (async) to generate SQL (LLM call 1).
    3. Strip markdown fences.
    4. Detect "cannot answer" replies.
    5. Run :func:`~askcgsim.sim_query_schema.validate_and_guard` (AST guard).
    6. Execute the sanitised SQL synchronously against the read-only SQLite DB.
    7. Call the LLM (async) to summarise the results in natural language (LLM call 2).
    8. Build and return the evidence dict.

    Every failure mode produces a structured evidence dict (never an
    exception) so the Bamboo executor always receives a usable payload.

    Args:
        question: Natural-language question from the user.
        db_path: Filesystem path to the CGSim SQLite file.

    Returns:
        Evidence dictionary with keys: ``question``, ``sql``, ``columns``,
        ``rows``, ``row_count``, ``truncated``, ``execution_time_ms``,
        ``db_path``, ``summary``, ``error``, ``guard_rejection``.
    """
    from askcgsim.sim_query_schema import (  # deferred
        MAX_ROWS,
        MAX_ROWS_AGGREGATION,
        QUERY_TIMEOUT_SECS,
        build_schema_context,
        validate_and_guard,
        _has_group_by,
    )
    import sqlglot as _sqlglot  # deferred
    import sqlglot.expressions as _exp  # deferred

    schema_context = build_schema_context()

    # --- Stage 2-4: LLM SQL generation --------------------------------------
    try:
        raw_reply = await _call_llm_for_sql(question, schema_context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cgsim.sim_query: LLM SQL generation failed")
        return _execution_error_evidence(
            question=question,
            sql=None,
            db_path=db_path,
            detail=f"LLM SQL generation failed: {exc}",
        )

    raw_sql = _strip_sql_fences(raw_reply)
    if not raw_sql or _looks_like_cannot_answer(raw_sql):
        logger.debug(
            "cgsim.sim_query: LLM declined to generate SQL: %r", raw_reply[:120]
        )
        return _unable_to_answer_evidence(question, db_path)

    logger.debug("cgsim.sim_query: generated SQL: %s", raw_sql)

    # --- Stage 5: AST guard -------------------------------------------------
    guard = validate_and_guard(raw_sql)
    if not guard.passed:
        logger.warning(
            "cgsim.sim_query: guard rejected SQL (rule=%s): %s",
            guard.triggered_rule,
            raw_sql,
        )
        return _guard_rejected_evidence(
            question=question,
            raw_sql=raw_sql,
            reason=guard.rejection_reason or "Unknown guard violation.",
            rule=guard.triggered_rule or "unknown",
            db_path=db_path,
        )

    sanitised_sql: str = guard.sanitised_sql  # type: ignore[assignment]

    # Choose the Python-side fetch cap to match the SQL LIMIT.
    # Aggregation queries (GROUP BY) use MAX_ROWS_AGGREGATION so the
    # fetchmany cap never truncates results that the guard already allowed.
    try:
        _stmts = _sqlglot.parse(sanitised_sql, dialect="sqlite")
        _first = _stmts[0] if _stmts else None
        _is_agg = isinstance(_first, _exp.Select) and _has_group_by(_first)
    except Exception:  # noqa: BLE001
        _is_agg = False
    fetch_cap = MAX_ROWS_AGGREGATION if _is_agg else MAX_ROWS

    # --- Pre-flight: check file exists --------------------------------------
    if not os.path.exists(db_path):
        logger.warning("cgsim.sim_query: database file not found: %s", db_path)
        return _db_unavailable_evidence(question, db_path)

    # --- Stage 6: execute synchronously ------------------------------------
    # SQLite queries against a local simulation DB are fast (< 20 ms) so
    # running them on the event loop thread is safe and avoids the
    # asyncio.to_thread thread-pool conflicts seen with DuckDB on macOS.
    try:
        exec_result = _execute_query(
            db_path, sanitised_sql, QUERY_TIMEOUT_SECS, fetch_cap
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cgsim.sim_query: execution error: %s", exc)
        exc_str = str(exc).lower()
        if (
            "does not exist" in exc_str
            or "no such file" in exc_str
            or "unable to open" in exc_str
        ):
            return _db_unavailable_evidence(question, db_path)
        if "no such table" in exc_str:
            return _wrong_database_evidence(question, db_path)
        return _execution_error_evidence(
            question=question,
            sql=sanitised_sql,
            db_path=db_path,
            detail=str(exc),
        )

    logger.debug(
        "cgsim.sim_query: query returned %d rows (truncated=%s) in %.1f ms",
        exec_result["row_count"],
        exec_result["truncated"],
        exec_result["execution_time_ms"],
    )

    # Emit a tracing span for the SQLite execution step so it appears in
    # /tracing alongside the two LLM call spans.
    _emit_sqlite_span(
        exec_result["execution_time_ms"],
        exec_result["row_count"],
        exec_result["truncated"],
    )

    # --- Stage 7: LLM summarisation -----------------------------------------
    results_json = json.dumps(
        {
            "columns": exec_result["columns"],
            "rows": exec_result["rows"],
            "row_count": exec_result["row_count"],
            "truncated": exec_result["truncated"],
        },
        indent=2,
    )

    summary: str | None = None
    try:
        summary = await _call_llm_for_summary(question, sanitised_sql, results_json)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cgsim.sim_query: LLM summarisation failed: %s", exc)
        # Non-fatal — the raw evidence is still returned.

    return {
        "question": question,
        "sql": sanitised_sql,
        "columns": exec_result["columns"],
        "rows": exec_result["rows"],
        "row_count": exec_result["row_count"],
        "truncated": exec_result["truncated"],
        "execution_time_ms": exec_result["execution_time_ms"],
        "db_path": db_path,
        "summary": summary,
        "error": None,
        "guard_rejection": None,
    }


# ---------------------------------------------------------------------------
# Structured error constructors
# ---------------------------------------------------------------------------


def _unable_to_answer_evidence(question: str, db_path: str) -> dict[str, Any]:
    """Return a structured evidence dict for the 'LLM cannot answer' case.

    Args:
        question: The original user question.
        db_path: Path to the CGSim SQLite file.

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
        "db_path": db_path,
        "summary": None,
        "error": (
            "I wasn't able to translate that question into a CGSim database query. "
            "Try rephrasing with a specific job ID, site name, or event type."
        ),
        "guard_rejection": None,
    }


def _guard_rejected_evidence(
    question: str,
    raw_sql: str,
    reason: str,
    rule: str,
    db_path: str,
) -> dict[str, Any]:
    """Return a structured evidence dict for a guard rejection.

    Args:
        question: The original user question.
        raw_sql: The SQL string that triggered the rejection.
        reason: Human-readable rejection reason.
        rule: Short rule identifier that triggered rejection.
        db_path: Path to the CGSim SQLite file.

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
        "db_path": db_path,
        "summary": None,
        "error": "That query isn't permitted by this tool. Only read-only lookups are supported.",
        "guard_rejection": f"[{rule}] {reason}",
    }


def _execution_error_evidence(
    question: str,
    sql: str | None,
    db_path: str,
    detail: str = "",
) -> dict[str, Any]:
    """Return a structured evidence dict for a query execution error.

    The raw database error is intentionally not included in the user-facing
    message to avoid leaking schema or path information.

    Args:
        question: The original user question.
        sql: The SQL that was attempted, or ``None`` if the error preceded execution.
        db_path: Path to the CGSim SQLite file.
        detail: Optional internal detail logged but not shown to the user.

    Returns:
        Evidence dict with a user-safe error message.
    """
    if detail:
        logger.debug("cgsim.sim_query execution error detail: %s", detail)
    return {
        "question": question,
        "sql": sql,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "execution_time_ms": 0.0,
        "db_path": db_path,
        "summary": None,
        "error": (
            "The CGSim simulation database query could not be executed. "
            "Try a more specific question or check that the database file is available."
        ),
        "guard_rejection": None,
    }


def _db_unavailable_evidence(question: str, db_path: str) -> dict[str, Any]:
    """Return a structured evidence dict when the database file is not found.

    Args:
        question: The original user question.
        db_path: The expected path to the CGSim SQLite file.

    Returns:
        Evidence dict with a user-safe error message naming the missing file.
    """
    return {
        "question": question,
        "sql": None,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "execution_time_ms": 0.0,
        "db_path": db_path,
        "summary": None,
        "error": (
            f"The CGSim simulation database file was not found at '{db_path}'. "
            "Set the CGSIM_DB_PATH environment variable to the path of your "
            "simulation output database."
        ),
        "guard_rejection": None,
    }


def _wrong_database_evidence(question: str, db_path: str) -> dict[str, Any]:
    """Return a structured evidence dict when the file exists but lacks the EVENTS table.

    This indicates the file at *db_path* is not a CGSim simulation database —
    it may be an empty SQLite file, a different application's database, or a
    placeholder file created before a simulation run.

    Args:
        question: The original user question.
        db_path: The path to the SQLite file that was opened.

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
        "db_path": db_path,
        "summary": None,
        "error": (
            f"The file at '{db_path}' does not appear to be a CGSim simulation database "
            "(the EVENTS table was not found). "
            "Set CGSIM_DB_PATH to the path of a database produced by a CGSim run."
        ),
        "guard_rejection": None,
    }


# ---------------------------------------------------------------------------
# MCP tool definition
# ---------------------------------------------------------------------------


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for ``cgsim.sim_query``.

    Returns:
        Tool definition dict compatible with MCP discovery.
    """
    return {
        "name": "cgsim.sim_query",
        "description": (
            "Answer natural-language questions about a CGSim simulation run by "
            "querying the simulation output SQLite database. "
            "Use this tool when the user asks about simulation results such as "
            "job execution times, queue wait times, file transfer speeds, network "
            "congestion, site utilisation, retry rates, or disk I/O performance — "
            "for example: 'How long did job J-001 take?', "
            "'Which site had the most jobs?', "
            "'Were any jobs affected by network congestion?', "
            "'What was the average file transfer speed?'. "
            "Requires CGSIM_DB_PATH to be set to the simulation database file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Natural-language question about the CGSim simulation, e.g. "
                        "'How long did job J-001 take to execute?' or "
                        "'Which site had the highest CPU utilisation?'"
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


class CgsimSimQueryTool:
    """MCP tool that answers NL questions about a CGSim simulation via SQL.

    Translates the user's natural-language question into a single SELECT
    statement using the LLM, validates it through the AST guard, executes it
    against the read-only CGSim SQLite database, then uses the LLM again to
    summarise the raw results in natural language.
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
        """Translate the question to SQL, execute it, summarise, and return evidence.

        Both LLM calls are awaited directly.  Only the blocking SQLite
        execution runs synchronously on the event loop thread (it is fast
        enough not to require a thread pool).  ``bamboo.tools.base`` is
        imported inside this method (deferred) so the rest of this module
        remains importable when bamboo core is not installed.

        Args:
            arguments: Dict with required ``"question"`` (str).

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
                    "error": (
                        "Question is too long (max 2000 characters). "
                        "Please be more concise."
                    ),
                },
            }))

        db_path: str = os.environ.get("CGSIM_DB_PATH", "cgsim.db")

        try:
            evidence = await fetch_and_analyse(question, db_path)
            return text_content(json.dumps({"evidence": evidence}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("cgsim.sim_query tool call failed")
            return text_content(json.dumps({
                "evidence": {
                    "question": question,
                    "error": repr(exc),
                },
            }))


cgsim_sim_query_tool = CgsimSimQueryTool()

__all__ = [
    "CgsimSimQueryTool",
    "cgsim_sim_query_tool",
    "fetch_and_analyse",
    "get_definition",
]
