"""Schema allow-list, SQL guard, and LLM context builder for the CGSim sim_query tool.

This module is the single security gate between LLM-generated SQL and the
SQLite execution engine for the CGSim simulation database.  It has **zero
bamboo-core dependency** so it can be imported freely by tests, stubs, and
the tool implementation alike.

Guard pipeline
--------------
Every SQL string produced by the LLM passes through :func:`validate_and_guard`
before it is executed.  The guard enforces four independent layers of defence:

1. **Parse check** — ``sqlglot`` must parse the SQL successfully (SQLite
   dialect).  Malformed or adversarial SQL that cannot be parsed at all is
   rejected immediately.
2. **Single-statement rule** — exactly one statement is permitted.  Stacked
   statements (e.g. ``SELECT 1; DROP TABLE EVENTS``) are rejected.
3. **SELECT-only root** — the top-level statement must be a ``SELECT``.  All
   DDL, DML, DCL, and TCL variants are rejected if they appear as the root
   node *or* anywhere inside the AST tree (rule 4).
4. **Forbidden-construct scan** — the full AST is walked; any node of a
   forbidden type causes rejection regardless of nesting depth.
5. **System-table filter** — references to ``sqlite_master``,
   ``sqlite_temp_master``, ``information_schema``, etc. are rejected.
6. **Table allow-list** — only :data:`ALLOWED_TABLES` (currently just
   ``events``) may appear in ``FROM`` or ``JOIN`` clauses.  CTE alias names
   are collected and excluded from allow-list checks.
7. **LIMIT injection** — if the validated query contains no ``LIMIT`` clause,
   one is injected at :data:`MAX_ROWS`.

SQL execution safety (enforced by the caller, not this module)
--------------------------------------------------------------
* The connection is opened with ``sqlite3.connect("file:{path}?mode=ro",
  uri=True)`` so the driver refuses any write at the OS level.
* ``PRAGMA query_only = ON`` is issued immediately after connection, adding a
  second enforcement layer inside the SQLite library itself.

Schema context
--------------
:func:`build_schema_context` returns a static multi-line string describing the
CGSim EVENTS table and all METADATA fields.  The string is intended for
inclusion in LLM system prompts.  The schema never changes between simulation
runs so the value is module-level constant (no TTL needed).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import sqlglot
import sqlglot.expressions as exp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Tables exposed to the LLM and permitted in generated queries.
#: CGSim databases expose a single table named ``EVENTS``.
ALLOWED_TABLES: frozenset[str] = frozenset({"events"})

#: Maximum rows returned per query for raw (non-aggregated) results.
#: CGSim METADATA blobs are large; 200 rows keeps synthesis prompts manageable.
MAX_ROWS: int = 200

#: Higher cap applied when the query contains a GROUP BY clause.
#: Aggregations collapse many rows into few groups — the full result is always
#: small and must not be truncated.
MAX_ROWS_AGGREGATION: int = 1000

#: Hard timeout applied to every query execution (seconds).
QUERY_TIMEOUT_SECS: int = 10

# System / internal SQLite table names and prefixes to reject.
_SYSTEM_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "sys",
        "sqlite_master",
        "sqlite_temp_master",
        "sqlite_sequence",
    }
)
_SYSTEM_TABLE_PREFIXES: tuple[str, ...] = ("sqlite_", "pg_", "duckdb_")

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
    exp.Command,   # covers EXECUTE, CALL, ATTACH, DETACH, and other commands
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
            rendered back to a string for execution.  ``None`` when ``passed``
            is ``False``.
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
# Guard helpers
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


def _check_system_tables(
    ast: exp.Select,
    cte_aliases: frozenset[str],
) -> GuardResult | None:
    r"""Check AST for references to system tables or internal functions.

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
    r"""Check that every table reference in *ast* is in :data:`ALLOWED_TABLES`.

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


def _has_group_by(ast: exp.Select) -> bool:
    """Return ``True`` if *ast* contains a GROUP BY clause.

    Args:
        ast: Parsed SELECT expression to inspect.

    Returns:
        ``True`` if a ``GROUP BY`` clause is present.
    """
    return ast.find(exp.Group) is not None


def _inject_limit_if_absent(ast: exp.Select, max_rows: int) -> str:
    r"""Return the SQL string, injecting ``LIMIT`` *max_rows* if absent.

    Parses with the SQLite dialect and renders back without a dialect
    specifier.  This round-trip converts ``json_extract`` calls to
    ``JSON_EXTRACT`` (uppercase) which SQLite accepts, while leaving all
    other constructs unchanged.

    Args:
        ast: A parsed SELECT expression (already validated).
        max_rows: The LIMIT value to inject if none is present.

    Returns:
        SQL string with a LIMIT clause guaranteed to be present.
    """
    if ast.find(exp.Limit) is None:
        ast = ast.limit(max_rows)
    # Render without a dialect so JSON_EXTRACT is preserved in its canonical
    # form (not transformed to the SQLite -> operator which some Python builds
    # do not support).
    return ast.sql()


# ---------------------------------------------------------------------------
# Main guard entry point
# ---------------------------------------------------------------------------


def validate_and_guard(sql: str) -> GuardResult:
    r"""Validate SQL syntax and enforce the read-only allow-list guard.

    Parses *sql* into an AST using the SQLite dialect and evaluates it
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
        statements = sqlglot.parse(
            sql, dialect="sqlite", error_level=sqlglot.ErrorLevel.RAISE
        )
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

    # --- Rules 5 & 5b: collect CTE aliases; check system tables -------------
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
    is_agg = _has_group_by(ast)
    cap = MAX_ROWS_AGGREGATION if is_agg else MAX_ROWS
    sanitised_sql = _inject_limit_if_absent(ast, cap)

    return GuardResult(passed=True, sanitised_sql=sanitised_sql)


# ---------------------------------------------------------------------------
# Schema context (static — CGSim schema does not change at runtime)
# ---------------------------------------------------------------------------

#: Thread lock protecting lazy initialisation of the schema context string.
_ctx_lock: threading.Lock = threading.Lock()
_ctx_cache: str | None = None


def build_schema_context() -> str:
    """Return a compact schema summary suitable for inclusion in an LLM prompt.

    The CGSim schema never changes at runtime so the result is computed once
    and cached for the lifetime of the process.

    Returns:
        A multi-line string describing the EVENTS table and all METADATA
        fields, ready to paste into an LLM system prompt.
    """
    global _ctx_cache  # noqa: PLW0603
    with _ctx_lock:
        if _ctx_cache is None:
            _ctx_cache = _build_schema_context_uncached()
        return _ctx_cache


def _build_schema_context_uncached() -> str:
    """Build the schema context string.

    Returns:
        Multi-line schema context string.
    """
    return _SCHEMA_CONTEXT


# ---------------------------------------------------------------------------
# Schema context string
# ---------------------------------------------------------------------------

_SCHEMA_CONTEXT: str = """\
CGSim EVENTS table — SQLite database
=====================================
Table: EVENTS

  _ID     INTEGER   Auto-assigned row ID. Not meaningful for analysis.
  EVENT   TEXT      Activity type: JobAllocation | JobExecution | FileTransfer | FileRead | FileWrite
  STATE   TEXT      Lifecycle stage: Started | Finished
  STATUS  TEXT      Job status: pending | assigned | running | finished
  JOB_ID  TEXT      Unique job identifier. Groups all rows for a single job.
  TIME    REAL      Simulation clock timestamp (seconds). Not authoritative for duration.
  METADATA TEXT     JSON object whose shape depends on (EVENT, STATE). Use json_extract().

KEY RULES:
- Every activity produces exactly one Started row and one Finished row (same JOB_ID).
- The authoritative elapsed time is the `duration` field in the Finished row's METADATA.
  Do NOT compute Finished.TIME − Started.TIME; always use json_extract(METADATA,'$.duration').
- All TIME and duration values are in SECONDS (simulation clock).
- Utilisation fields (site_cpu_util, grid_cpu_util, site_storage_util, grid_storage_util)
  are snapshots in [0.0, 1.0]. They are NOT averages; they reflect grid state at row write time.
- NEVER reference json_extract(METADATA,'$.cost') — this field is uncalibrated; exclude it.
- Use json_extract(METADATA,'$.field') to access any METADATA field.

METADATA FIELDS BY (EVENT, STATE):

JobAllocation/Started:   site, host
JobAllocation/Finished:  site, host, site_storage_util, grid_storage_util,
                         site_cpu_util, grid_cpu_util

JobExecution/Started:    flops(FLOP), site, host, cores(CPU), speed(FLOP/s),
                         site_cpu_util, grid_cpu_util
JobExecution/Finished:   flops(FLOP), cores(CPU), site, host, speed(FLOP/s),
                         site_cpu_util, grid_cpu_util,
                         duration(s) — compute time only,
                         retries(int, 0=first-attempt success),
                         total_io_read_time(s), file_transfer_queue_time(s),
                         resource_waiting_queue_time(s), total_queue_time(s)
                         [total_queue_time = file_transfer_queue_time + resource_waiting_queue_time]

FileTransfer/Started:    file, size(bytes), source_site, destination_site,
                         bandwidth(bytes/s), latency(s), link_load([0,1]),
                         site_storage_util, grid_storage_util
FileTransfer/Finished:   file, size(bytes), source_site, destination_site,
                         duration(s), bandwidth(bytes/s), latency(s),
                         link_load([0,1]), site_storage_util, grid_storage_util

FileRead/Started:        file, size(bytes), site, host, disk, disk_read_bw(bytes/s)
FileRead/Finished:       file, size(bytes), site, host, disk, disk_read_bw(bytes/s),
                         duration(s)

FileWrite/Started:       file, size(bytes), site, host, disk, disk_write_bw(bytes/s),
                         site_storage_util, grid_storage_util
FileWrite/Finished:      file, size(bytes), site, host, disk, disk_write_bw(bytes/s),
                         site_storage_util, grid_storage_util, duration(s)

TOTAL WALL-CLOCK TIME for a job:
  resource_waiting_queue_time + file_transfer_queue_time + total_io_read_time + duration
  (all from JobExecution/Finished METADATA)
"""

# ---------------------------------------------------------------------------
# LLM prompt builders
# ---------------------------------------------------------------------------

#: Sentinel returned by the LLM when it cannot generate SQL.
CANNOT_ANSWER_SENTINEL: str = "CANNOT_ANSWER"

#: System prompt template for SQL generation (LLM call 1).
_SQL_GENERATION_SYSTEM: str = """\
You are a read-only SQL assistant for a CGSim simulation database (SQLite dialect).
The database has ONE table: EVENTS. Every query must use FROM EVENTS.

{schema_context}

RULES:
- Return ONLY a single SELECT statement. No explanation, no markdown, no code fences.
- The ONLY permitted table is EVENTS. Never reference any other table name.
- Use json_extract(METADATA, '$.field') to access any METADATA field.
- Do not use INSERT, UPDATE, DELETE, DROP, CREATE, or any DDL or DML.
- Do not reference sqlite_master, sqlite_sequence, or any system tables.
- Do not use semicolons. Do not stack multiple statements.
- Always filter on EVENT and STATE to avoid mixing row types, e.g.:
    WHERE EVENT='JobExecution' AND STATE='Finished'
- Use the `duration` field from Finished rows for elapsed time. Never compute
  Finished.TIME - Started.TIME.
- Never reference json_extract(METADATA, '$.cost') — this field is uncalibrated.
- If the question cannot be answered from the EVENTS table, reply with exactly: CANNOT_ANSWER

EXAMPLE QUERIES:

-- "Show me all jobs" / "List all jobs" / "What jobs are in the simulation?"
SELECT DISTINCT JOB_ID FROM EVENTS ORDER BY JOB_ID LIMIT 200

-- "Show me all job IDs" / "What are the job IDs?"
SELECT DISTINCT JOB_ID FROM EVENTS ORDER BY JOB_ID LIMIT 200

-- "How long did job J-001 take to execute?"
SELECT json_extract(METADATA, '$.duration') AS execution_duration_s
FROM EVENTS
WHERE JOB_ID = 'J-001' AND EVENT = 'JobExecution' AND STATE = 'Finished'
LIMIT 1

-- "What was the total wall-clock time for job J-001?"
SELECT
    json_extract(METADATA, '$.duration') AS compute_s,
    json_extract(METADATA, '$.total_queue_time') AS queue_s,
    json_extract(METADATA, '$.total_io_read_time') AS io_read_s,
    json_extract(METADATA, '$.duration')
    + json_extract(METADATA, '$.total_queue_time')
    + json_extract(METADATA, '$.total_io_read_time') AS total_wall_clock_s
FROM EVENTS
WHERE JOB_ID = 'J-001' AND EVENT = 'JobExecution' AND STATE = 'Finished'
LIMIT 1

-- "Why did job J-001 spend so long queuing?"
SELECT
    json_extract(METADATA, '$.file_transfer_queue_time') AS file_transfer_wait_s,
    json_extract(METADATA, '$.resource_waiting_queue_time') AS resource_wait_s,
    json_extract(METADATA, '$.total_queue_time') AS total_queue_s
FROM EVENTS
WHERE JOB_ID = 'J-001' AND EVENT = 'JobExecution' AND STATE = 'Finished'
LIMIT 1

-- "Which site had the most jobs allocated to it?"
-- Return all sites with counts so ties are visible — do NOT use LIMIT 1.
SELECT json_extract(METADATA, '$.site') AS site, COUNT(*) AS job_count
FROM EVENTS
WHERE EVENT = 'JobAllocation' AND STATE = 'Finished'
GROUP BY site
ORDER BY job_count DESC
LIMIT 200

-- "Which jobs were affected by network congestion?"
SELECT JOB_ID, json_extract(METADATA, '$.link_load') AS link_load,
       json_extract(METADATA, '$.source_site') AS source,
       json_extract(METADATA, '$.destination_site') AS dest
FROM EVENTS
WHERE EVENT = 'FileTransfer' AND STATE = 'Started'
  AND json_extract(METADATA, '$.link_load') > 0.8
ORDER BY link_load DESC
LIMIT 200

-- "Average execution time per site?"
SELECT json_extract(METADATA, '$.site') AS site,
       AVG(json_extract(METADATA, '$.duration')) AS avg_duration_s,
       COUNT(*) AS job_count
FROM EVENTS
WHERE EVENT = 'JobExecution' AND STATE = 'Finished'
GROUP BY site
ORDER BY avg_duration_s DESC
LIMIT 200

-- "Did jobs retry frequently?"
SELECT retries, COUNT(*) AS n
FROM (
    SELECT json_extract(METADATA, '$.retries') AS retries
    FROM EVENTS
    WHERE EVENT = 'JobExecution' AND STATE = 'Finished'
)
GROUP BY retries
ORDER BY n DESC
LIMIT 200

-- "Which disk was the bottleneck for reads?"
SELECT json_extract(METADATA, '$.disk') AS disk,
       AVG(CAST(json_extract(METADATA, '$.size') AS REAL)
           / json_extract(METADATA, '$.duration')) AS avg_throughput_bytes_per_s,
       json_extract(METADATA, '$.disk_read_bw') AS max_bw_bytes_per_s,
       COUNT(*) AS n
FROM EVENTS
WHERE EVENT = 'FileRead' AND STATE = 'Finished'
GROUP BY disk, max_bw_bytes_per_s
ORDER BY avg_throughput_bytes_per_s ASC
LIMIT 200
"""

#: System prompt template for natural-language summarisation (LLM call 2).
#:
#: LIST RULE: when the user explicitly asks to enumerate specific values
#: (e.g. "show me all job IDs"), the model must reproduce every value from
#: the relevant column(s) rather than summarising or giving only a range.
#: For all other questions the model summarises concisely as before.
_SUMMARISE_SYSTEM: str = """\
You are a scientific computing assistant summarising results from a CGSim \
simulation database query.

The user asked: {question}
The SQL executed was:
{sql}

The raw results (JSON) are:
{results_json}

Field units: TIME and duration values are in SECONDS. size values are in BYTES.
speed and bandwidth values are in FLOP/s or bytes/s respectively.
Utilisation fractions are in [0.0, 1.0] (multiply by 100 for percent).
retries=0 means the job succeeded on its first attempt.

LIST RULE: If the user's question explicitly asks to list, show, or enumerate \
specific identifiers or records (e.g. "show me all job IDs", "list all sites", \
"what are the job IDs"), AND the result set was not truncated, reproduce every \
value from the relevant column(s) in the rows — do not summarise or give a range. \
If the result was truncated, enumerate what is available and note that more exist.

For all other questions: summarise the results clearly and concisely in natural \
language. Mention the key numbers.
If the result set was truncated (more rows exist than shown), say so.
If no rows were returned, say the query matched no events.
If all rows have the same value for an ordered/ranked column (e.g. all sites have \
the same job count), explicitly say so — do not report only the top row as if it \
were uniquely the winner.\
"""


def build_sql_prompt(question: str, schema_context: str | None = None) -> list[dict[str, Any]]:
    """Build the LLM message list for SQL generation (LLM call 1).

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
    system_content = _SQL_GENERATION_SYSTEM.format(schema_context=schema_context)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": question},
    ]


def build_summarise_prompt(
    question: str,
    sql: str,
    results_json: str,
) -> list[dict[str, Any]]:
    """Build the LLM message list for natural-language summarisation (LLM call 2).

    Args:
        question: The original natural-language question from the user.
        sql: The SQL query that was executed.
        results_json: JSON string of the query results.

    Returns:
        A list of ``{"role": str, "content": str}`` dicts suitable for
        passing to any Bamboo LLM provider.
    """
    system_content = _SUMMARISE_SYSTEM.format(
        question=question,
        sql=sql,
        results_json=results_json,
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Please summarise these results."},
    ]


def invalidate_schema_cache() -> None:
    """Evict the cached schema context string.

    Intended for tests that need a fresh context.  Not normally needed since
    the CGSim schema is static.
    """
    global _ctx_cache  # noqa: PLW0603
    with _ctx_lock:
        _ctx_cache = None


__all__ = [
    "ALLOWED_TABLES",
    "CANNOT_ANSWER_SENTINEL",
    "MAX_ROWS",
    "MAX_ROWS_AGGREGATION",
    "QUERY_TIMEOUT_SECS",
    "GuardResult",
    "build_schema_context",
    "build_sql_prompt",
    "build_summarise_prompt",
    "invalidate_schema_cache",
    "validate_and_guard",
]
