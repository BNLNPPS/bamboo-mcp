"""Tests for jobs_query_schema, jobs_query_impl, and bamboo_answer routing.

Coverage:
- :func:`validate_and_guard` — valid SQL, every rejection rule, adversarial
  inputs, LIMIT injection.
- :func:`fetch_and_analyse` — happy path, cannot-answer, guard rejection,
  execution error, truncation.
- :class:`PandaJobsQueryTool`.call() — missing argument, success, file-not-
  found.
- :func:`_is_jobs_db_question` — routing heuristic in bamboo_answer.

All database access uses an in-memory DuckDB instance seeded with the schema
from :mod:`askpanda_atlas_agents.common.storage.schema` (or a local minimal
DDL if that package is not installed).  No network calls are made.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import duckdb
import pytest

from askpanda_atlas.jobs_query_impl import (
    _strip_sql_fences,
    fetch_and_analyse,
    panda_jobs_query_tool,
)
from askpanda_atlas.jobs_query_schema import (
    build_schema_context,
    invalidate_schema_cache,
    validate_and_guard,
)


# ---------------------------------------------------------------------------
# Minimal DDL for in-memory test DB
# ---------------------------------------------------------------------------

_MINIMAL_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    pandaid        BIGINT PRIMARY KEY,
    jobstatus      VARCHAR,
    computingsite  VARCHAR,
    jeditaskid     BIGINT,
    durationsec    DOUBLE,
    cpuefficiency  DOUBLE,
    piloterrorcode INTEGER,
    piloterrordiag VARCHAR,
    _queue         VARCHAR NOT NULL,
    _fetched_utc   TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS selectionsummary (
    id           INTEGER NOT NULL,
    field        VARCHAR NOT NULL,
    list_json    JSON NOT NULL,
    stats_json   JSON,
    _queue       VARCHAR NOT NULL,
    _fetched_utc TIMESTAMP NOT NULL,
    PRIMARY KEY (id, _queue)
);
CREATE TABLE IF NOT EXISTS errors_by_count (
    id              INTEGER NOT NULL,
    error           VARCHAR,
    codename        VARCHAR,
    codeval         INTEGER,
    count           INTEGER,
    example_pandaid BIGINT,
    _queue          VARCHAR NOT NULL,
    _fetched_utc    TIMESTAMP NOT NULL,
    PRIMARY KEY (id, _queue)
);
"""

_SEED_SQL = """
INSERT INTO jobs VALUES
    (1001, 'failed',   'BNL', 99, 120.0, 0.82, 1008, 'pilot timeout', 'BNL', '2026-03-24 10:00:00'),
    (1002, 'finished', 'BNL', 99,  90.0, 0.95, 0,    NULL,            'BNL', '2026-03-24 10:00:00'),
    (1003, 'running',  'BNL', 99, NULL,  NULL, 0,    NULL,            'BNL', '2026-03-24 10:00:00'),
    (2001, 'failed',   'CERN_PROD', 88, 60.0, 0.70, 1099, 'memory exceeded', 'CERN_PROD', '2026-03-24 10:00:00');
INSERT INTO errors_by_count VALUES
    (1, 'pilot', 'PilotTimeout', 1008, 5, 1001, 'BNL', '2026-03-24 10:00:00');
"""


@pytest.fixture()
def mem_db() -> duckdb.DuckDBPyConnection:
    """Return a seeded in-memory DuckDB connection.

    Yields:
        Open DuckDB connection with the minimal schema and seed data.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(_MINIMAL_DDL)
    conn.execute(_SEED_SQL)
    return conn


def _make_llm_patch(sql: str) -> Any:
    """Return an AsyncMock that makes ``_call_llm_for_sql`` return *sql*.

    Args:
        sql: The SQL string the fake LLM should return.

    Returns:
        ``unittest.mock.AsyncMock`` suitable for use with ``patch``.
    """
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=sql)


def _unpack(result: list[Any]) -> dict[str, Any]:
    """Deserialise the JSON evidence from a tool call result.

    Args:
        result: Return value of ``PandaJobsQueryTool.call()``.

    Returns:
        Parsed evidence dict.
    """
    assert result, "Expected non-empty result list"
    payload = json.loads(result[0]["text"])
    return payload.get("evidence", payload)


# ===========================================================================
# Guard unit tests
# ===========================================================================


class TestGuardAllowsValidSQL:
    """Ensure well-formed SELECT statements pass the guard."""

    def test_simple_select(self) -> None:
        """A plain SELECT with a LIMIT should pass unchanged."""
        r = validate_and_guard("SELECT pandaid FROM jobs WHERE _queue='BNL' LIMIT 10")
        assert r.passed
        assert r.sanitised_sql is not None
        assert r.rejection_reason is None

    def test_select_with_aggregation(self) -> None:
        """A GROUP BY aggregate should pass."""
        sql = "SELECT jobstatus, COUNT(*) AS n FROM jobs WHERE _queue='BNL' GROUP BY jobstatus LIMIT 100"
        r = validate_and_guard(sql)
        assert r.passed

    def test_select_with_join(self) -> None:
        """A join between allowed tables should pass."""
        sql = (
            "SELECT j.pandaid, e.codename "
            "FROM jobs j JOIN errors_by_count e ON j._queue = e._queue "
            "WHERE j._queue = 'BNL' LIMIT 50"
        )
        r = validate_and_guard(sql)
        assert r.passed

    def test_select_case_insensitive(self) -> None:
        """Mixed-case SELECT should still parse and pass."""
        r = validate_and_guard("SeLeCt pandaid FrOm jobs LiMiT 5")
        assert r.passed

    def test_cte_select(self) -> None:
        """A CTE (WITH ... SELECT) is a valid SELECT variant."""
        sql = (
            "WITH failed AS (SELECT pandaid FROM jobs WHERE jobstatus='failed' AND _queue='BNL') "
            "SELECT COUNT(*) FROM failed LIMIT 1"
        )
        r = validate_and_guard(sql)
        assert r.passed


class TestGuardRejectsDML:
    """Guard must reject all DML statements."""

    def test_insert(self) -> None:
        """INSERT should be rejected at the root."""
        r = validate_and_guard("INSERT INTO jobs (pandaid) VALUES (9)")
        assert not r.passed
        assert r.triggered_rule == "non_select_root"

    def test_update(self) -> None:
        """UPDATE should be rejected at the root."""
        r = validate_and_guard("UPDATE jobs SET jobstatus='failed' WHERE pandaid=1")
        assert not r.passed
        assert r.triggered_rule == "non_select_root"

    def test_delete(self) -> None:
        """DELETE should be rejected at the root."""
        r = validate_and_guard("DELETE FROM jobs WHERE pandaid=1")
        assert not r.passed
        assert r.triggered_rule == "non_select_root"


class TestGuardRejectsDDL:
    """Guard must reject DDL statements."""

    def test_drop(self) -> None:
        """DROP TABLE should be rejected."""
        r = validate_and_guard("DROP TABLE jobs")
        assert not r.passed
        assert r.triggered_rule == "non_select_root"

    def test_create(self) -> None:
        """CREATE TABLE should be rejected."""
        r = validate_and_guard("CREATE TABLE foo (id INT)")
        assert not r.passed
        assert r.triggered_rule == "non_select_root"

    def test_alter(self) -> None:
        """ALTER TABLE should be rejected."""
        r = validate_and_guard("ALTER TABLE jobs ADD COLUMN foo VARCHAR")
        assert not r.passed
        assert r.triggered_rule == "non_select_root"

    def test_truncate(self) -> None:
        """TRUNCATE should be rejected."""
        r = validate_and_guard("TRUNCATE TABLE jobs")
        assert not r.passed


class TestGuardRejectsStackedStatements:
    """Stacked (multi-statement) inputs must be rejected."""

    def test_select_then_drop(self) -> None:
        """SELECT followed by DROP should be rejected as multiple statements."""
        r = validate_and_guard("SELECT pandaid FROM jobs LIMIT 1; DROP TABLE jobs")
        assert not r.passed
        assert r.triggered_rule == "multiple_statements"

    def test_two_selects(self) -> None:
        """Two SELECT statements separated by semicolon should be rejected."""
        r = validate_and_guard("SELECT 1; SELECT 2")
        assert not r.passed
        assert r.triggered_rule == "multiple_statements"


class TestGuardRejectsSystemTables:
    """References to system/internal tables must be rejected."""

    def test_information_schema(self) -> None:
        """information_schema reference should be rejected."""
        r = validate_and_guard("SELECT * FROM information_schema.tables LIMIT 10")
        assert not r.passed
        assert r.triggered_rule == "system_table"

    def test_duckdb_prefix(self) -> None:
        """duckdb_* table reference should be rejected."""
        r = validate_and_guard("SELECT * FROM duckdb_tables() LIMIT 10")
        assert not r.passed
        assert r.triggered_rule in ("system_table", "forbidden_construct", "parse_error", "non_select_root")


class TestGuardRejectsUnknownTables:
    """References to tables not in ALLOWED_TABLES must be rejected."""

    def test_unknown_table(self) -> None:
        """A table name not in the allow-list should be rejected."""
        r = validate_and_guard("SELECT * FROM secret_table LIMIT 10")
        assert not r.passed
        assert r.triggered_rule == "unknown_table"

    def test_subquery_unknown_table(self) -> None:
        """An unknown table inside a subquery should still be rejected."""
        r = validate_and_guard(
            "SELECT * FROM (SELECT * FROM secret_table LIMIT 5) t LIMIT 10"
        )
        assert not r.passed
        assert r.triggered_rule == "unknown_table"


class TestGuardAdversarialInputs:
    """Known SQL injection patterns must be caught."""

    def test_union_injection(self) -> None:
        """UNION-based injection should fail at the unknown-table or parse stage."""
        r = validate_and_guard(
            "SELECT pandaid FROM jobs LIMIT 1 "
            "UNION SELECT table_name FROM information_schema.tables LIMIT 1"
        )
        assert not r.passed

    def test_stacked_insert(self) -> None:
        """Stacked statement with INSERT should be rejected."""
        r = validate_and_guard(
            "SELECT pandaid FROM jobs LIMIT 1; INSERT INTO jobs (pandaid) VALUES (99)"
        )
        assert not r.passed

    def test_empty_sql(self) -> None:
        """Empty string should be rejected gracefully."""
        r = validate_and_guard("")
        assert not r.passed

    def test_whitespace_only(self) -> None:
        """Whitespace-only string should be rejected gracefully."""
        r = validate_and_guard("   ")
        assert not r.passed


class TestGuardLimitInjection:
    """LIMIT injection and preservation."""

    def test_injects_limit_when_absent(self) -> None:
        """SELECT without LIMIT should have a LIMIT injected."""
        r = validate_and_guard("SELECT pandaid FROM jobs WHERE _queue='BNL'")
        assert r.passed
        assert "LIMIT" in (r.sanitised_sql or "").upper()

    def test_preserves_existing_limit(self) -> None:
        """SELECT with an explicit LIMIT should keep the original value."""
        r = validate_and_guard("SELECT pandaid FROM jobs WHERE _queue='BNL' LIMIT 5")
        assert r.passed
        sql = r.sanitised_sql or ""
        # The existing LIMIT 5 should be preserved (not doubled or replaced).
        assert "5" in sql


# ===========================================================================
# _strip_sql_fences
# ===========================================================================


class TestStripSqlFences:
    """SQL fence stripping helper."""

    def test_strips_sql_fence(self) -> None:
        """```sql ... ``` wrapper should be removed."""
        assert _strip_sql_fences("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_strips_plain_fence(self) -> None:
        """``` ... ``` wrapper should be removed."""
        assert _strip_sql_fences("```\nSELECT 1\n```") == "SELECT 1"

    def test_no_fence(self) -> None:
        """Plain SQL without fences should pass through unchanged."""
        assert _strip_sql_fences("SELECT 1") == "SELECT 1"

    def test_strips_whitespace(self) -> None:
        """Leading/trailing whitespace should be stripped."""
        assert _strip_sql_fences("  SELECT 1  ") == "SELECT 1"


# ===========================================================================
# Schema context cache
# ===========================================================================


class TestBuildSchemaContext:
    """Schema context builder and cache."""

    def setup_method(self) -> None:
        """Clear the schema context cache before each test."""
        invalidate_schema_cache()

    def test_returns_non_empty_string(self) -> None:
        """build_schema_context should return a non-empty string."""
        ctx = build_schema_context()
        assert isinstance(ctx, str)
        assert len(ctx) > 50

    def test_contains_jobs_table(self) -> None:
        """Context for the default tables should mention 'jobs'."""
        ctx = build_schema_context()
        assert "jobs" in ctx

    def test_subset_of_tables(self) -> None:
        """Requesting only errors_by_count should omit jobs-only columns."""
        ctx = build_schema_context(["errors_by_count"])
        assert "errors_by_count" in ctx
        # 'jobstatus' is defined only in the jobs table, not in errors_by_count.
        assert "jobstatus" not in ctx

    def test_caches_result(self) -> None:
        """Successive calls with the same tables should return identical strings."""
        ctx1 = build_schema_context(["jobs"])
        ctx2 = build_schema_context(["jobs"])
        assert ctx1 is ctx2 or ctx1 == ctx2


# ===========================================================================
# fetch_and_analyse pipeline tests
# ===========================================================================


class TestFetchAndAnalysePipeline:
    """End-to-end pipeline with an in-memory DuckDB and a mock LLM."""

    def test_successful_count_query(self, mem_db: duckdb.DuckDBPyConnection) -> None:
        """A count query against the test DB should return the correct count."""
        sql = "SELECT COUNT(*) AS n FROM jobs WHERE _queue='BNL' AND jobstatus='failed'"
        with patch("askpanda_atlas.jobs_query_impl.duckdb") as mock_duckdb:
            mock_duckdb.connect.return_value = mem_db
            mock_duckdb.Error = duckdb.Error
            with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch(sql)):
                result = asyncio.run(fetch_and_analyse("How many failed at BNL?", ":memory:"))
        assert result["error"] is None
        assert result["row_count"] == 1
        assert result["rows"][0]["n"] == 1

    def test_returns_sql_in_evidence(self, mem_db: duckdb.DuckDBPyConnection) -> None:
        """The executed SQL should always appear in the evidence dict."""
        sql = "SELECT pandaid FROM jobs WHERE _queue='BNL' LIMIT 5"
        with patch("askpanda_atlas.jobs_query_impl.duckdb") as mock_duckdb:
            mock_duckdb.connect.return_value = mem_db
            mock_duckdb.Error = duckdb.Error
            with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch(sql)):
                result = asyncio.run(fetch_and_analyse("List BNL jobs", ":memory:"))
        assert result["sql"] is not None
        assert "jobs" in result["sql"]

    def test_zero_rows_returns_explicit_count(self, mem_db: duckdb.DuckDBPyConnection) -> None:
        """A query that matches no rows should return row_count=0 explicitly."""
        sql = "SELECT pandaid FROM jobs WHERE _queue='NOWHERE' LIMIT 10"
        with patch("askpanda_atlas.jobs_query_impl.duckdb") as mock_duckdb:
            mock_duckdb.connect.return_value = mem_db
            mock_duckdb.Error = duckdb.Error
            with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch(sql)):
                result = asyncio.run(fetch_and_analyse("Jobs at NOWHERE?", ":memory:"))
        assert result["error"] is None
        assert result["row_count"] == 0
        assert result["rows"] == []

    def test_truncation_flag(self, mem_db: duckdb.DuckDBPyConnection) -> None:
        """Result sets at the cap should have truncated=True."""
        sql = "SELECT pandaid FROM jobs"
        with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch(sql)):
            with patch("askpanda_atlas.jobs_query_impl._execute_query") as mock_exec:
                mock_exec.return_value = {
                    "columns": ["pandaid"],
                    "rows": [{"pandaid": 1}, {"pandaid": 2}],
                    "row_count": 2,
                    "truncated": True,
                    "execution_time_ms": 1.0,
                }
                result = asyncio.run(fetch_and_analyse("All jobs", ":memory:"))
        assert result["truncated"] is True

    def test_llm_cannot_answer(self) -> None:
        """When the LLM returns CANNOT_ANSWER the evidence should have an error."""
        with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch("CANNOT_ANSWER")):
            result = asyncio.run(fetch_and_analyse("What is the meaning of life?", ":memory:"))
        assert result["error"] is not None
        assert "translate" in result["error"].lower() or "rephrase" in result["error"].lower()
        assert result["row_count"] == 0

    def test_llm_natural_language_refusal(self) -> None:
        """Natural-language refusals from the LLM should also be caught."""
        with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql",
                   _make_llm_patch("I cannot answer that question.")):
            result = asyncio.run(fetch_and_analyse("Who wrote Hamlet?", ":memory:"))
        assert result["error"] is not None
        assert result["sql"] is None

    def test_guard_rejected_sql(self) -> None:
        """SQL rejected by the guard should produce a structured guard_rejection."""
        with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql",
                   _make_llm_patch("DELETE FROM jobs WHERE _queue='BNL'")):
            result = asyncio.run(fetch_and_analyse("Delete BNL jobs", ":memory:"))
        assert result["error"] is not None
        assert result["guard_rejection"] is not None
        assert result["row_count"] == 0

    def test_execution_error_returns_structured_evidence(self) -> None:
        """A DuckDB execution error should produce a structured error, not raise."""
        sql = "SELECT pandaid FROM jobs WHERE _queue='BNL' LIMIT 10"
        with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch(sql)):
            with patch("askpanda_atlas.jobs_query_impl._execute_query",
                       side_effect=Exception("simulated DB error")):
                result = asyncio.run(fetch_and_analyse("Jobs at BNL?", ":memory:"))
        assert result["error"] is not None
        assert "simulated" not in result["error"]
        assert result["row_count"] == 0

    def test_markdown_fences_stripped(self, mem_db: duckdb.DuckDBPyConnection) -> None:
        """SQL wrapped in markdown fences should still execute correctly."""
        sql = "```sql\nSELECT COUNT(*) AS n FROM jobs WHERE _queue='BNL'\n```"
        with patch("askpanda_atlas.jobs_query_impl.duckdb") as mock_duckdb:
            mock_duckdb.connect.return_value = mem_db
            mock_duckdb.Error = duckdb.Error
            with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch(sql)):
                result = asyncio.run(fetch_and_analyse("BNL count?", ":memory:"))
        assert result["error"] is None
        assert result["row_count"] == 1

    def test_evidence_always_has_question(self) -> None:
        """The evidence dict must always contain the original question."""
        with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch("CANNOT_ANSWER")):
            result = asyncio.run(fetch_and_analyse("CANNOT_ANSWER", ":memory:"))
        assert result["question"] == "CANNOT_ANSWER"


# ===========================================================================
# PandaJobsQueryTool.call() tests
# ===========================================================================


class TestPandaJobsQueryToolCall:
    """Tests for the async MCP tool entry point."""

    def _run(self, coro: Any) -> Any:
        """Run a coroutine synchronously.

        Args:
            coro: Coroutine to run.

        Returns:
            Return value of the coroutine.
        """
        return asyncio.run(coro)

    def test_missing_question_returns_error(self) -> None:
        """call() with no question should return an error payload, not raise."""
        result = self._run(panda_jobs_query_tool.call({}))
        evidence = _unpack(result)
        assert "error" in evidence
        assert evidence["error"] is not None

    def test_empty_question_returns_error(self) -> None:
        """call() with an empty question string should return an error."""
        result = self._run(panda_jobs_query_tool.call({"question": "  "}))
        evidence = _unpack(result)
        assert "error" in evidence

    def test_question_too_long_returns_error(self) -> None:
        """call() with a question exceeding 2000 chars should return an error."""
        result = self._run(panda_jobs_query_tool.call({"question": "x" * 2001}))
        evidence = _unpack(result)
        assert "error" in evidence
        assert "long" in evidence["error"].lower() or "2000" in evidence["error"]

    def test_successful_call_returns_evidence(self, mem_db: duckdb.DuckDBPyConnection) -> None:
        """A successful call should return a JSON evidence dict."""
        sql = "SELECT COUNT(*) AS n FROM jobs WHERE _queue='BNL' AND jobstatus='failed'"
        with patch("askpanda_atlas.jobs_query_impl.duckdb") as mock_duckdb:
            mock_duckdb.connect.return_value = mem_db
            mock_duckdb.Error = duckdb.Error
            with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", _make_llm_patch(sql)):
                result = self._run(panda_jobs_query_tool.call({
                    "question": "How many jobs failed at BNL?",
                }))
        evidence = _unpack(result)
        assert evidence.get("error") is None
        assert evidence["row_count"] == 1

    def test_queue_appended_to_question(self, mem_db: duckdb.DuckDBPyConnection) -> None:
        """When queue is provided it should be appended to the question string."""
        captured: list[str] = []

        async def _capture_llm(question: str, schema_context: str) -> str:
            """Capture the question and return safe SQL.

            Args:
                question: The question passed to the LLM.
                schema_context: Ignored.

            Returns:
                Safe SELECT SQL.
            """
            captured.append(question)
            return "SELECT COUNT(*) AS n FROM jobs LIMIT 1"

        with patch("askpanda_atlas.jobs_query_impl.duckdb") as mock_duckdb:
            mock_duckdb.connect.return_value = mem_db
            mock_duckdb.Error = duckdb.Error
            with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql", side_effect=_capture_llm):
                self._run(panda_jobs_query_tool.call({
                    "question": "How many failed jobs?",
                    "queue": "BNL",
                }))
        assert captured, "LLM callable was never called"
        assert "BNL" in captured[0]

    def test_duckdb_file_not_found_returns_error(self) -> None:
        """When the DuckDB file is absent the tool should return a structured error."""
        with patch.dict("os.environ", {"PANDA_DUCKDB_PATH": "/nonexistent/jobs.duckdb"}):
            with patch("askpanda_atlas.jobs_query_impl._execute_query",
                       side_effect=FileNotFoundError("no such file")):
                with patch("askpanda_atlas.jobs_query_impl._call_llm_for_sql",
                           _make_llm_patch("SELECT pandaid FROM jobs LIMIT 1")):
                    result = self._run(panda_jobs_query_tool.call({
                        "question": "How many failed jobs at BNL?",
                    }))
        evidence = _unpack(result)
        assert "error" in evidence


# ===========================================================================
# bamboo_answer routing heuristic
# ===========================================================================


class TestIsJobsDbQuestion:
    """Tests for the _is_jobs_db_question routing helper."""

    def setup_method(self) -> None:
        """Import the helper fresh for each test."""
        from bamboo.tools.bamboo_answer import _is_jobs_db_question  # noqa: PLC0415
        self._fn = _is_jobs_db_question

    def test_failed_at_matches(self) -> None:
        """'failed at BNL' should be detected as a jobs DB question."""
        assert self._fn("How many jobs failed at BNL?")

    def test_jobs_running_matches(self) -> None:
        """'jobs running' should be detected as a jobs DB question."""
        assert self._fn("How many jobs are running at CERN?")

    def test_top_errors_matches(self) -> None:
        """'top errors' should be detected as a jobs DB question."""
        assert self._fn("What are the top errors at SWT2_CPB?")

    def test_last_updated_matches(self) -> None:
        """'last updated' should be detected as a jobs DB question."""
        assert self._fn("When was the database last updated?")

    def test_each_status_matches(self) -> None:
        """'each status' should be detected as a jobs DB question."""
        assert self._fn("How many jobs are in each status at BNL?")

    def test_which_queues_matches(self) -> None:
        """'which queues' should be detected as a jobs DB question."""
        assert self._fn("Which queues have the most failed jobs?")

    def test_task_question_excluded(self) -> None:
        """Questions containing 'task' should not route to jobs DB."""
        assert not self._fn("How many jobs failed in task 12345?")

    def test_general_question_excluded(self) -> None:
        """General PanDA documentation questions should not route to jobs DB."""
        assert not self._fn("What is a pilot job?")

    def test_pandaid_question_excluded(self) -> None:
        """Questions about a specific job ID should not route to jobs DB."""
        assert not self._fn("What happened to job 987654321?")


class TestDatabaseDisambiguation:
    """Tests for the multi-database disambiguation helpers."""

    def setup_method(self) -> None:
        """Import helpers fresh for each test."""
        from bamboo.tools.bamboo_answer import (  # noqa: PLC0415
            QUERYABLE_DATABASES,
            _build_clarification_response,
            _resolve_target_database,
        )
        self._resolve = _resolve_target_database
        self._clarify = _build_clarification_response
        self._dbs = QUERYABLE_DATABASES

    def test_single_db_always_resolves(self) -> None:
        """With only one database registered, any question resolves to it."""
        assert len(self._dbs) == 1
        result = self._resolve("When was the database last updated?")
        assert result == "jobs"

    def test_jobs_keywords_resolve_to_jobs(self) -> None:
        """Questions with job-specific keywords resolve to the jobs DB."""
        assert self._resolve("How many jobs failed at BNL?") == "jobs"
        assert self._resolve("What are the top errors at SWT2_CPB?") == "jobs"

    def test_clarification_message_lists_databases(self) -> None:
        """Clarification response should mention all registered databases."""
        msg = self._clarify("Some ambiguous question")
        for db_name in self._dbs:
            assert db_name in msg
        assert "rephrase" in msg.lower()

    def test_clarification_message_is_string(self) -> None:
        """Clarification response should always be a non-empty string."""
        msg = self._clarify("ambiguous")
        assert isinstance(msg, str)
        assert len(msg) > 20
