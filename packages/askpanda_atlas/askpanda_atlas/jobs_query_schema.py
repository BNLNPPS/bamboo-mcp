"""Schema allow-list, SQL guard, and LLM context builder for the jobs query tool.

This module is the single security gate between LLM-generated SQL and the
DuckDB execution engine.  It has **zero bamboo-core dependency** so it can
be imported freely by tests, stubs, and the tool implementation alike.

Guard pipeline
--------------
Every SQL string produced by the LLM passes through :func:`validate_and_guard`
before it is executed.  The guard:

1. Parses the SQL into an AST via ``sqlglot`` (DuckDB dialect).
2. Rejects any input that is not a single SELECT statement.
3. Rejects DDL, DML, DCL, and TCL at any depth of the AST.
4. Rejects references to system/internal tables.
5. Rejects table references not in :data:`ALLOWED_TABLES`.
6. Injects a ``LIMIT`` clause if the query omits one, capped at
   :data:`MAX_ROWS`.

Schema context
--------------
:func:`build_schema_context` returns a compact multi-line string describing
the database schema.  The string is intended for inclusion in LLM system
prompts.  Results are cached in-process with a configurable TTL
(:data:`SCHEMA_CONTEXT_TTL`) because the schema never changes between
ingestion cycles.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import sqlglot
import sqlglot.expressions as exp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Tables exposed to the LLM and permitted in generated queries.
ALLOWED_TABLES: frozenset[str] = frozenset(
    {"jobs", "selectionsummary", "errors_by_count"}
)

#: Maximum rows returned per query.  The guard injects ``LIMIT {MAX_ROWS}``
#: when the LLM omits a LIMIT clause, and execution caps the result set at
#: this value regardless.
MAX_ROWS: int = 500

#: Hard timeout applied to every query execution (seconds).
QUERY_TIMEOUT_SECS: int = 10

#: TTL for the cached schema context string (seconds).  One hour is
#: sufficient — the schema does not change between ingestion cycles.
SCHEMA_CONTEXT_TTL: float = 3600.0

# System / internal table name prefixes and exact names to reject.
_SYSTEM_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "sys",
        "sqlite_master",
        "sqlite_temp_master",
    }
)
_SYSTEM_TABLE_PREFIXES: tuple[str, ...] = ("duckdb_", "pg_", "sqlite_")

# AST node types that are forbidden anywhere in the statement tree.
_FORBIDDEN_NODE_TYPES: tuple[type[Any], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Revoke,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Command,   # covers EXECUTE, CALL, and other commands
)

# ---------------------------------------------------------------------------
# GuardResult
# ---------------------------------------------------------------------------


@dataclass
class GuardResult:
    """Outcome of :func:`validate_and_guard`.

    Attributes:
        passed: ``True`` if the SQL passed all checks and is safe to execute.
        sanitised_sql: The SQL string with a ``LIMIT`` injected (if absent),
            ready for execution.  ``None`` when ``passed`` is ``False``.
        rejection_reason: Human-readable explanation of why the SQL was
            rejected.  ``None`` when ``passed`` is ``True``.
        triggered_rule: Short identifier for the rule that triggered
            rejection.  ``None`` when ``passed`` is ``True``.
    """

    passed: bool
    sanitised_sql: str | None = field(default=None)
    rejection_reason: str | None = field(default=None)
    triggered_rule: str | None = field(default=None)


# ---------------------------------------------------------------------------
# Guard implementation
# ---------------------------------------------------------------------------


def _is_system_name(name: str) -> bool:
    """Return ``True`` if *name* matches a known system table prefix or name.

    Args:
        name: Lowercase identifier to check (table name, db qualifier, etc.).

    Returns:
        ``True`` if the name refers to an internal or system object.
    """
    if not name:
        return False
    if name in _SYSTEM_TABLE_NAMES:
        return True
    return any(name.startswith(pfx) for pfx in _SYSTEM_TABLE_PREFIXES)


def _check_system_tables(ast: exp.Select, cte_aliases: frozenset[str]) -> GuardResult | None:
    """Check AST for references to system tables or internal functions.

    Inspects all ``Table`` nodes and ``Anonymous`` function calls in *ast*.
    Returns a :class:`GuardResult` rejection if a system reference is found,
    or ``None`` if all references are clean.

    Args:
        ast: Parsed SELECT expression to inspect.
        cte_aliases: Set of CTE-defined alias names to skip (they are not
            external table references).

    Returns:
        A failed :class:`GuardResult` if a system reference is detected,
        otherwise ``None``.
    """
    for table_node in ast.find_all(exp.Table):
        name = (table_node.name or "").lower()
        if name in cte_aliases:
            continue
        db_node = table_node.args.get("db")
        db = (db_node.name if db_node is not None else "").lower()
        catalog_node = table_node.args.get("catalog")
        catalog = (catalog_node.name if catalog_node is not None else "").lower()
        for part in (name, db, catalog):
            if _is_system_name(part):
                return GuardResult(
                    passed=False,
                    rejection_reason=f"Reference to system table '{part}' is not permitted.",
                    triggered_rule="system_table",
                )

    for func_node in ast.find_all(exp.Anonymous):
        func_name = (func_node.name or "").lower()
        if any(func_name.startswith(pfx) for pfx in _SYSTEM_TABLE_PREFIXES):
            return GuardResult(
                passed=False,
                rejection_reason=f"Reference to system function '{func_name}' is not permitted.",
                triggered_rule="system_table",
            )
    return None


def _check_table_allowlist(
    ast: exp.Select,
    cte_aliases: frozenset[str],
) -> GuardResult | None:
    """Check that every table reference in *ast* is in :data:`ALLOWED_TABLES`.

    Args:
        ast: Parsed SELECT expression to inspect.
        cte_aliases: Set of CTE-defined alias names to skip.

    Returns:
        A failed :class:`GuardResult` if an unknown table is found,
        otherwise ``None``.
    """
    for table_node in ast.find_all(exp.Table):
        name = (table_node.name or "").lower()
        if name in cte_aliases:
            continue
        if name and name not in ALLOWED_TABLES:
            return GuardResult(
                passed=False,
                rejection_reason=(
                    f"Table '{name}' is not in the list of permitted tables: "
                    f"{sorted(ALLOWED_TABLES)}."
                ),
                triggered_rule="unknown_table",
            )
    return None


def validate_and_guard(sql: str) -> GuardResult:
    """Validate SQL syntax and enforce the read-only allow-list guard.

    Parses *sql* into an AST using the DuckDB dialect and evaluates it
    against the configured guard rules.  Returns a structured result
    indicating whether the SQL is safe to execute and, if not, which rule
    triggered rejection.

    The guard never attempts to fix or rewrite malformed SQL — it either
    passes the statement (optionally injecting a ``LIMIT``) or rejects it.

    Args:
        sql: Raw SQL string as generated by the LLM.  May be malformed or
            contain adversarial constructs.

    Returns:
        :class:`GuardResult` with ``passed=True`` and ``sanitised_sql`` set
        on success, or ``passed=False`` with ``rejection_reason`` and
        ``triggered_rule`` set on failure.
    """
    # --- Rule 1: parse must succeed -----------------------------------------
    try:
        statements = sqlglot.parse(sql, dialect="duckdb", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.ParseError as exc:
        return GuardResult(
            passed=False,
            rejection_reason=f"SQL could not be parsed: {exc}",
            triggered_rule="parse_error",
        )

    # --- Rule 2: exactly one statement --------------------------------------
    if len(statements) != 1:
        return GuardResult(
            passed=False,
            rejection_reason=(
                f"Expected exactly one SQL statement, got {len(statements)}. "
                "Stacked statements are not permitted."
            ),
            triggered_rule="multiple_statements",
        )

    ast = statements[0]
    if ast is None:
        return GuardResult(
            passed=False,
            rejection_reason="Empty SQL statement.",
            triggered_rule="empty_statement",
        )

    # --- Rule 3: top-level must be SELECT -----------------------------------
    if not isinstance(ast, exp.Select):
        node_type = type(ast).__name__
        return GuardResult(
            passed=False,
            rejection_reason=(
                f"Only SELECT statements are permitted; got {node_type}."
            ),
            triggered_rule="non_select_root",
        )

    # --- Rule 4: no forbidden node types anywhere in the tree ---------------
    for node in ast.walk():
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            node_type = type(node).__name__
            return GuardResult(
                passed=False,
                rejection_reason=(
                    f"Forbidden SQL construct '{node_type}' found in query."
                ),
                triggered_rule="forbidden_construct",
            )

    # --- Rules 5 & 5b: no system / internal tables or functions ------------
    cte_aliases: frozenset[str] = frozenset(
        cte.alias.lower() for cte in ast.find_all(exp.CTE)
    )
    system_rejection = _check_system_tables(ast, cte_aliases)
    if system_rejection is not None:
        return system_rejection

    # --- Rule 6: table allow-list -------------------------------------------
    allowlist_rejection = _check_table_allowlist(ast, cte_aliases)
    if allowlist_rejection is not None:
        return allowlist_rejection

    # --- Rule 7: LIMIT injection --------------------------------------------
    sanitised_sql = _inject_limit_if_absent(ast, MAX_ROWS)

    return GuardResult(passed=True, sanitised_sql=sanitised_sql)


def _inject_limit_if_absent(ast: exp.Select, max_rows: int) -> str:
    """Return the SQL string, injecting LIMIT *max_rows* if not already present.

    Modifies the AST in-place (sqlglot nodes are mutable) and renders it
    back to a SQL string using the DuckDB dialect.

    Args:
        ast: A parsed SELECT expression.
        max_rows: The LIMIT value to inject if none is present.

    Returns:
        SQL string with a LIMIT clause guaranteed to be present.
    """
    if ast.find(exp.Limit) is None:
        ast = ast.limit(max_rows)
    return ast.sql(dialect="duckdb")


# ---------------------------------------------------------------------------
# Schema context cache
# ---------------------------------------------------------------------------

_ctx_lock: threading.Lock = threading.Lock()
# key: tuple of sorted table names (as a string); value: (expiry, context_str)
_ctx_cache: dict[str, tuple[float, str]] = {}


def build_schema_context(tables: list[str] | None = None) -> str:
    """Return a compact schema summary suitable for inclusion in an LLM prompt.

    Results are cached in-process for :data:`SCHEMA_CONTEXT_TTL` seconds.
    The schema is stable between ingestion cycles so a long TTL is safe.

    Args:
        tables: Optional list of table names to include.  Defaults to all
            three data tables: ``jobs``, ``selectionsummary``,
            ``errors_by_count``.

    Returns:
        A multi-line string describing the schema, ready to paste into an
        LLM system prompt.
    """
    if tables is None:
        tables = ["jobs", "selectionsummary", "errors_by_count"]

    cache_key = ",".join(sorted(tables))
    now = time.monotonic()

    with _ctx_lock:
        entry = _ctx_cache.get(cache_key)

    if entry is not None:
        expiry, ctx = entry
        if expiry == math.inf or now < expiry:
            return ctx

    # Cache miss — build the context string.
    ctx = _build_context_uncached(tables)
    expiry = now + SCHEMA_CONTEXT_TTL

    with _ctx_lock:
        _ctx_cache[cache_key] = (expiry, ctx)

    return ctx


def _build_context_uncached(tables: list[str]) -> str:
    """Build the schema context string without consulting the cache.

    Delegates to ``get_schema_context()`` from
    ``askpanda_atlas_agents.common.storage.schema_annotations`` when that
    package is importable.  Falls back to a minimal hand-built summary
    (sufficient for tests that run without the full agents package).

    Args:
        tables: List of table names to include in the context.

    Returns:
        Multi-line schema context string.
    """
    try:
        # The agents package contains the authoritative annotated schema.
        from askpanda_atlas_agents.common.storage.schema_annotations import (  # type: ignore[import]
            get_schema_context,
        )
        return get_schema_context(tables)
    except ImportError:
        pass

    # Minimal fallback — used in tests and CI where the agents package is
    # not installed.  Covers only the columns most relevant to NL queries.
    lines: list[str] = []
    _FALLBACK: dict[str, list[tuple[str, str, str]]] = {
        "jobs": [
            ("pandaid", "BIGINT", "Unique PanDA job identifier (primary key)."),
            ("jobstatus", "VARCHAR", "Job status: defined/waiting/sent/starting/running/holding/merging/finished/failed/cancelled/closed."),
            ("computingsite", "VARCHAR", "Computing site / queue name (same as _queue)."),
            ("jeditaskid", "BIGINT", "JEDI task ID the job belongs to."),
            ("taskid", "BIGINT", "PanDA task ID."),
            ("produserid", "VARCHAR", "User DN or production role that submitted the job."),
            ("piloterrorcode", "INTEGER", "Pilot error code (0 = no error)."),
            ("piloterrordiag", "VARCHAR", "Pilot error diagnostic message."),
            ("exeerrorcode", "INTEGER", "Payload execution error code (0 = no error)."),
            ("exeerrordiag", "VARCHAR", "Payload execution diagnostic message."),
            ("cpuefficiency", "DOUBLE", "CPU efficiency ratio (0.0–1.0+)."),
            ("durationsec", "DOUBLE", "Wall-clock run time in seconds."),
            ("statechangetime", "TIMESTAMP", "UTC timestamp of the last job status transition."),
            ("creationtime", "TIMESTAMP", "UTC timestamp when the job was created."),
            ("_queue", "VARCHAR", "Ingestion bookkeeping: full queue name polled (e.g. 'BNL_ATLAS_TIER1'). Use ILIKE 'SITE%' for site-level filtering."),
            ("_fetched_utc", "TIMESTAMP", "UTC timestamp when the ingestion agent last fetched this row."),
        ],
        "selectionsummary": [
            ("id", "INTEGER", "Position within this queue's response."),
            ("field", "VARCHAR", "Facet name (e.g. jobstatus, cloud, gshare)."),
            ("list_json", "JSON", "JSON array of {kname, kvalue} — value and count pairs."),
            ("stats_json", "JSON", "JSON aggregate stats, e.g. {\"sum\": 9928}."),
            ("_queue", "VARCHAR", "Source queue (full queue name, e.g. 'BNL_ATLAS_TIER1'). Use ILIKE 'SITE%' for site-level filtering."),
            ("_fetched_utc", "TIMESTAMP", "When this snapshot was fetched."),
        ],
        "errors_by_count": [
            ("id", "INTEGER", "Rank within this queue's response."),
            ("error", "VARCHAR", "Error category: pilot, exe, ddm, brokerage, etc."),
            ("codename", "VARCHAR", "Symbolic error name."),
            ("codeval", "INTEGER", "Numeric error code."),
            ("diag", "VARCHAR", "Diagnostic string."),
            ("count", "INTEGER", "Jobs affected IN THIS QUEUE from a separate BigPanDA summary endpoint. Do NOT use for site-scoped frequency questions — use COUNT(*) on the jobs table instead."),
            ("example_pandaid", "BIGINT", "A representative job with this error."),
            ("_queue", "VARCHAR", "Source queue (full queue name, e.g. 'BNL_ATLAS_TIER1'). Use ILIKE 'SITE%' for site-level filtering."),
            ("_fetched_utc", "TIMESTAMP", "When this snapshot was fetched."),
        ],
    }
    for table in tables:
        cols = _FALLBACK.get(table, [])
        lines.append(f"Table: {table}")
        for col_name, col_type, desc in cols:
            lines.append(f"  {col_name:<30} {col_type:<10}  {desc}")
        lines.append("")
    return "\n".join(lines)


def invalidate_schema_cache() -> None:
    """Evict all cached schema context strings.

    Intended for tests and situations where the schema has changed and a
    fresh context string is required immediately.
    """
    with _ctx_lock:
        _ctx_cache.clear()


# ---------------------------------------------------------------------------
# LLM prompt builder
# ---------------------------------------------------------------------------

#: Sentinel returned by the LLM when it cannot generate SQL.
CANNOT_ANSWER_SENTINEL: str = "CANNOT_ANSWER"

#: Instructions injected into the LLM system prompt for SQL generation.
SQL_GENERATION_SYSTEM_TEMPLATE: str = """\
You are a read-only SQL assistant for a PanDA jobs database (DuckDB dialect).

{schema_context}

Rules:
- Return ONLY a single SELECT statement. No explanation, no markdown, no fences.
- Do not use INSERT, UPDATE, DELETE, DROP, CREATE, or any DDL or DML.
- Do not reference information_schema or any system tables.
- Do not use semicolons.
- Always filter by _queue to scope queries to a specific computing site.
- IMPORTANT: _queue values are full queue names like 'BNL_ATLAS_TIER1' or
  'BNL_ATLAS_TIER1-condor', not bare site abbreviations like 'BNL'.
  When the question says "(filter _queue ILIKE 'SITE%')", use that pattern exactly.
  Otherwise use ILIKE with a '%' wildcard suffix: _queue ILIKE 'BNL%'.
  Never use exact equality (= 'BNL') for site-level filtering.
- IMPORTANT: for "most common failures / top errors" questions scoped to a
  site, ALWAYS query the jobs table directly using COUNT(*) GROUP BY on the
  error code columns (piloterrorcode, exeerrorcode, transexitcode). Never use
  errors_by_count for site-scoped frequency questions — its counts come from a
  different data source and will not match the jobs table. Only use
  errors_by_count for global (no site filter) cross-queue rankings.
- IMPORTANT: errors_by_count.count is a per-queue value from a separate
  BigPanDA summary endpoint, not derived from the jobs rows. It may differ
  from COUNT(*) on the jobs table.
- The database contains jobs from approximately the last hour per queue.
- Use _fetched_utc for freshness checks; use statechangetime for job state history.
- jobstatus values: defined, waiting, sent, starting, running, holding, merging,
  finished, failed, cancelled, closed.
- If the question cannot be answered by a SQL query against the available tables,
  reply with exactly: CANNOT_ANSWER

Example queries:
- "When was the database last updated?" →
  SELECT _queue, MAX(_fetched_utc) AS last_fetched, COUNT(*) AS job_count FROM jobs GROUP BY _queue ORDER BY last_fetched DESC LIMIT 500
- "How many jobs failed at BNL?" →
  SELECT COUNT(*) AS n FROM jobs WHERE _queue ILIKE 'BNL%' AND jobstatus = 'failed' LIMIT 500
- "How many jobs are in each status at BNL?" →
  SELECT jobstatus, COUNT(*) AS n FROM jobs WHERE _queue ILIKE 'BNL%' GROUP BY jobstatus ORDER BY n DESC LIMIT 500
- "How many jobs failed at BNL? (filter _queue ILIKE 'BNL%')" →
  SELECT COUNT(*) AS n FROM jobs WHERE _queue ILIKE 'BNL%' AND jobstatus = 'failed' LIMIT 500
- "What are the most common job failures at BNL?" →
  SELECT piloterrorcode, MIN(piloterrordiag) AS diag, COUNT(*) AS n
  FROM jobs WHERE _queue ILIKE 'BNL%' AND jobstatus = 'failed'
  AND piloterrorcode != 0
  GROUP BY piloterrorcode ORDER BY n DESC LIMIT 10
- "What are the top errors at SWT2_CPB?" →
  SELECT piloterrorcode, MIN(piloterrordiag) AS diag, COUNT(*) AS n
  FROM jobs WHERE _queue ILIKE 'SWT2_CPB%' AND jobstatus = 'failed'
  AND piloterrorcode != 0
  GROUP BY piloterrorcode ORDER BY n DESC LIMIT 10
- "Which error codes affect the most jobs across all sites?" →
  SELECT error, codename, codeval, SUM(count) AS total_count, MIN(diag) AS diag FROM errors_by_count GROUP BY error, codename, codeval ORDER BY total_count DESC LIMIT 10
- "Which queues have the most failed jobs?" →
  SELECT _queue, COUNT(*) AS failed_jobs FROM jobs WHERE jobstatus = 'failed' GROUP BY _queue ORDER BY failed_jobs DESC LIMIT 500
- "Which jobs are running at BNL?" →
  SELECT pandaid, produserid, durationsec, cpuefficiency FROM jobs WHERE _queue ILIKE 'BNL%' AND jobstatus = 'running' ORDER BY durationsec DESC LIMIT 500
"""


def build_sql_prompt(
    question: str,
    schema_context: str | None = None,
) -> list[dict[str, Any]]:
    """Build the LLM message list for SQL generation.

    Args:
        question: Natural-language question from the user.
        schema_context: Pre-built schema context string.  If ``None``,
            :func:`build_schema_context` is called to generate it.

    Returns:
        A list of ``{"role": str, "content": str}`` dicts suitable for
        passing to any Bamboo LLM provider.
    """
    if schema_context is None:
        schema_context = build_schema_context()

    system_content = SQL_GENERATION_SYSTEM_TEMPLATE.format(
        schema_context=schema_context,
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": question},
    ]
