"""Tests for sim_query_schema, sim_query_impl, and the CgsimSimQueryTool.

Coverage:
- :func:`validate_and_guard` — valid SQL, every rejection rule, adversarial
  inputs, LIMIT injection, aggregation cap, CTE allowance.
- :func:`fetch_and_analyse` — happy path (SQL + summary), cannot-answer,
  guard rejection, execution error, truncation, DB not found.
- :class:`CgsimSimQueryTool`.call() — missing argument, too-long question,
  success, file-not-found.
- Schema-context and prompt builders — shape and content checks.
- Read-only enforcement — write attempts are blocked.

All database access uses an in-memory SQLite instance seeded with the CGSim
schema.  No network calls are made; both LLM calls are monkeypatched.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from askcgsim.sim_query_schema import (
    CANNOT_ANSWER_SENTINEL,
    build_schema_context,
    build_sql_prompt,
    build_summarise_prompt,
    invalidate_schema_cache,
    validate_and_guard,
)
from askcgsim.sim_query_impl import (
    _execute_query,
    _looks_like_cannot_answer,
    _strip_sql_fences,
    cgsim_sim_query_tool,
    fetch_and_analyse,
)


# ---------------------------------------------------------------------------
# In-memory SQLite fixture helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS EVENTS (
    _ID     INTEGER PRIMARY KEY AUTOINCREMENT,
    EVENT   TEXT    NOT NULL,
    STATE   TEXT    NOT NULL,
    STATUS  TEXT    NOT NULL,
    JOB_ID  TEXT    NOT NULL,
    TIME    REAL    NOT NULL,
    METADATA TEXT   NOT NULL
);
"""

_SEED_ROWS: list[tuple[str, str, str, str, float, str]] = [
    (
        "JobExecution", "Finished", "finished", "J-001", 100.0,
        json.dumps({
            "flops": 1e9, "cores": 4, "site": "CERN", "host": "host-01",
            "speed": 1e9, "site_cpu_util": 0.6, "grid_cpu_util": 0.5,
            "duration": 80.0, "retries": 0,
            "total_io_read_time": 5.0,
            "file_transfer_queue_time": 3.0,
            "resource_waiting_queue_time": 2.0,
            "total_queue_time": 5.0,
        }),
    ),
    (
        "JobExecution", "Finished", "finished", "J-002", 200.0,
        json.dumps({
            "flops": 2e9, "cores": 8, "site": "BNL", "host": "host-02",
            "speed": 1e9, "site_cpu_util": 0.9, "grid_cpu_util": 0.7,
            "duration": 160.0, "retries": 1,
            "total_io_read_time": 10.0,
            "file_transfer_queue_time": 20.0,
            "resource_waiting_queue_time": 5.0,
            "total_queue_time": 25.0,
        }),
    ),
    (
        "FileTransfer", "Finished", "running", "J-001", 50.0,
        json.dumps({
            "file": "input.dat", "size": 1000000,
            "source_site": "CERN", "destination_site": "BNL",
            "duration": 3.0, "bandwidth": 1e8, "latency": 0.01,
            "link_load": 0.95, "site_storage_util": 0.4,
            "grid_storage_util": 0.3,
        }),
    ),
    (
        "FileRead", "Finished", "running", "J-001", 60.0,
        json.dumps({
            "file": "input.dat", "size": 1000000,
            "site": "BNL", "host": "host-02",
            "disk": "sda", "disk_read_bw": 5e7, "duration": 5.0,
        }),
    ),
    (
        "JobAllocation", "Finished", "assigned", "J-001", 10.0,
        json.dumps({
            "site": "BNL", "host": "host-02",
            "site_storage_util": 0.3, "grid_storage_util": 0.2,
            "site_cpu_util": 0.5, "grid_cpu_util": 0.4,
        }),
    ),
]


def _make_mem_db() -> sqlite3.Connection:
    """Create and seed an in-memory CGSim EVENTS database.

    Returns:
        Open in-memory :class:`sqlite3.Connection` with test data.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    conn.executemany(
        "INSERT INTO EVENTS (EVENT, STATE, STATUS, JOB_ID, TIME, METADATA) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _SEED_ROWS,
    )
    conn.commit()
    return conn


def _make_llm_patch(sql_reply: str) -> AsyncMock:
    """Return an AsyncMock that yields *sql_reply* when awaited.

    Args:
        sql_reply: The raw text the mock LLM should return.

    Returns:
        ``AsyncMock`` compatible with ``_call_llm_for_sql``'s signature.
    """
    mock = AsyncMock(return_value=sql_reply)
    return mock


def _make_summary_patch(summary: str = "Test summary.") -> AsyncMock:
    """Return an AsyncMock that yields *summary* from the summarisation call.

    Args:
        summary: Natural-language summary the mock LLM should return.

    Returns:
        ``AsyncMock`` compatible with ``_call_llm_for_summary``'s signature.
    """
    return AsyncMock(return_value=summary)


# ---------------------------------------------------------------------------
# validate_and_guard — valid paths
# ---------------------------------------------------------------------------


class TestValidateAndGuardValid:
    """Happy-path checks for :func:`validate_and_guard`."""

    def test_simple_select_passes(self) -> None:
        """A minimal SELECT on EVENTS passes all rules."""
        result = validate_and_guard("SELECT * FROM EVENTS LIMIT 10")
        assert result.passed is True
        assert result.sanitised_sql is not None
        assert result.rejection_reason is None

    def test_json_extract_passes(self) -> None:
        """A query using json_extract is accepted."""
        sql = (
            "SELECT json_extract(METADATA, '$.duration') AS dur "
            "FROM EVENTS WHERE EVENT='JobExecution' AND STATE='Finished'"
        )
        result = validate_and_guard(sql)
        assert result.passed is True

    def test_group_by_passes(self) -> None:
        """An aggregation query with GROUP BY is accepted."""
        sql = (
            "SELECT json_extract(METADATA, '$.site') AS site, COUNT(*) AS n "
            "FROM EVENTS WHERE EVENT='JobAllocation' AND STATE='Finished' "
            "GROUP BY site ORDER BY n DESC"
        )
        result = validate_and_guard(sql)
        assert result.passed is True

    def test_case_insensitive_table_name(self) -> None:
        """Table name ``events`` (lowercase) is accepted; names are normalised."""
        result = validate_and_guard("SELECT COUNT(*) FROM events LIMIT 10")
        assert result.passed is True

    def test_cte_allowed(self) -> None:
        """A query with a CTE that references EVENTS is accepted."""
        sql = (
            "WITH finished AS (SELECT JOB_ID FROM EVENTS WHERE STATE='Finished') "
            "SELECT * FROM finished LIMIT 10"
        )
        result = validate_and_guard(sql)
        assert result.passed is True

    def test_sanitised_sql_is_string(self) -> None:
        """``sanitised_sql`` is always a non-empty string on success."""
        result = validate_and_guard("SELECT _ID FROM EVENTS LIMIT 5")
        assert isinstance(result.sanitised_sql, str)
        assert len(result.sanitised_sql) > 0


# ---------------------------------------------------------------------------
# validate_and_guard — LIMIT injection
# ---------------------------------------------------------------------------


class TestLimitInjection:
    """LIMIT clause is injected when absent; existing limits are preserved."""

    def test_limit_injected_when_absent(self) -> None:
        """A SELECT without LIMIT gets one injected."""
        sql = "SELECT * FROM EVENTS WHERE EVENT='JobExecution'"
        result = validate_and_guard(sql)
        assert result.passed is True
        assert "LIMIT" in (result.sanitised_sql or "").upper()

    def test_existing_limit_preserved(self) -> None:
        """A SELECT with LIMIT 3 keeps that limit (not overwritten to MAX_ROWS)."""
        sql = "SELECT * FROM EVENTS LIMIT 3"
        result = validate_and_guard(sql)
        assert result.passed is True
        assert "3" in (result.sanitised_sql or "")

    def test_aggregation_uses_higher_cap(self) -> None:
        """GROUP BY queries receive the larger MAX_ROWS_AGGREGATION cap."""
        from askcgsim.sim_query_schema import MAX_ROWS, MAX_ROWS_AGGREGATION

        sql = (
            "SELECT EVENT, COUNT(*) AS n FROM EVENTS GROUP BY EVENT ORDER BY n DESC"
        )
        result = validate_and_guard(sql)
        assert result.passed is True
        sanitised = result.sanitised_sql or ""
        assert str(MAX_ROWS_AGGREGATION) in sanitised
        assert str(MAX_ROWS) not in sanitised or str(MAX_ROWS_AGGREGATION) in sanitised


# ---------------------------------------------------------------------------
# validate_and_guard — rejection rules
# ---------------------------------------------------------------------------


class TestValidateAndGuardRejections:
    """Each guard rule fires correctly on adversarial / invalid input."""

    def test_malformed_sql_rejected(self) -> None:
        """Syntactically invalid SQL is rejected with parse_error rule."""
        result = validate_and_guard("SELECT !!! FROM ???")
        assert result.passed is False
        assert result.triggered_rule == "parse_error"

    def test_multiple_statements_rejected(self) -> None:
        """Stacked statements are rejected."""
        result = validate_and_guard("SELECT 1; DROP TABLE EVENTS")
        assert result.passed is False
        assert result.triggered_rule == "multiple_statements"

    def test_drop_table_rejected(self) -> None:
        """DROP TABLE is rejected as a non-SELECT root."""
        result = validate_and_guard("DROP TABLE EVENTS")
        assert result.passed is False
        assert result.triggered_rule in ("non_select_root", "forbidden_construct")

    def test_insert_rejected(self) -> None:
        """INSERT is rejected."""
        result = validate_and_guard("INSERT INTO EVENTS VALUES (1,'x','y','z','j',0.0,'{}')")
        assert result.passed is False
        assert result.triggered_rule in ("non_select_root", "forbidden_construct")

    def test_delete_rejected(self) -> None:
        """DELETE is rejected."""
        result = validate_and_guard("DELETE FROM EVENTS WHERE _ID=1")
        assert result.passed is False
        assert result.triggered_rule in ("non_select_root", "forbidden_construct")

    def test_create_table_rejected(self) -> None:
        """CREATE TABLE is rejected."""
        result = validate_and_guard("CREATE TABLE evil (x INTEGER)")
        assert result.passed is False
        assert result.triggered_rule in ("non_select_root", "forbidden_construct")

    def test_unknown_table_rejected(self) -> None:
        """References to unlisted tables are rejected."""
        result = validate_and_guard("SELECT * FROM secrets LIMIT 10")
        assert result.passed is False
        assert result.triggered_rule == "unknown_table"

    def test_sqlite_master_rejected(self) -> None:
        """References to sqlite_master are rejected as system tables."""
        result = validate_and_guard("SELECT * FROM sqlite_master LIMIT 10")
        assert result.passed is False
        assert result.triggered_rule == "system_table"

    def test_sqlite_sequence_rejected(self) -> None:
        """References to sqlite_sequence are rejected as system tables."""
        result = validate_and_guard("SELECT * FROM sqlite_sequence LIMIT 10")
        assert result.passed is False
        assert result.triggered_rule == "system_table"

    def test_empty_string_rejected(self) -> None:
        """An empty SQL string is rejected."""
        result = validate_and_guard("")
        assert result.passed is False


# ---------------------------------------------------------------------------
# _strip_sql_fences
# ---------------------------------------------------------------------------


class TestStripSqlFences:
    """SQL fence stripping covers all common LLM output formats."""

    def test_no_fence(self) -> None:
        """Plain SQL is returned unchanged (whitespace stripped)."""
        assert _strip_sql_fences("  SELECT 1  ") == "SELECT 1"

    def test_sql_fence(self) -> None:
        """```sql fences are removed."""
        assert _strip_sql_fences("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_generic_fence(self) -> None:
        """Plain ``` fences are removed."""
        assert _strip_sql_fences("```\nSELECT 1\n```") == "SELECT 1"

    def test_uppercase_sql_fence(self) -> None:
        """```SQL (uppercase) fences are removed."""
        assert _strip_sql_fences("```SQL\nSELECT 1\n```") == "SELECT 1"


# ---------------------------------------------------------------------------
# _looks_like_cannot_answer
# ---------------------------------------------------------------------------


class TestLooksLikeCannotAnswer:
    """Cannot-answer detection covers the sentinel and refusal phrases."""

    def test_sentinel_exact(self) -> None:
        """Exact CANNOT_ANSWER sentinel is detected."""
        assert _looks_like_cannot_answer(CANNOT_ANSWER_SENTINEL) is True

    def test_sentinel_lowercase(self) -> None:
        """Lowercase ``cannot_answer`` is caught because ``.upper()`` matches the sentinel."""
        assert _looks_like_cannot_answer("cannot_answer") is True

    def test_natural_refusal(self) -> None:
        """Natural-language refusals are caught."""
        assert _looks_like_cannot_answer("I cannot generate SQL for that.") is True
        assert _looks_like_cannot_answer("I'm sorry, I can't answer this.") is True

    def test_valid_sql_not_caught(self) -> None:
        """A real SQL statement is not falsely flagged as a refusal."""
        assert _looks_like_cannot_answer("SELECT * FROM EVENTS LIMIT 10") is False


# ---------------------------------------------------------------------------
# _execute_query against in-memory DB
# ---------------------------------------------------------------------------


class TestExecuteQuery:
    """Direct tests of :func:`_execute_query` using a temp-file SQLite DB."""

    def test_count_query(self, tmp_path: Any) -> None:
        """COUNT(*) returns the correct integer."""
        db_file = str(tmp_path / "test.db")
        # Create and seed via a normal connection.
        setup = sqlite3.connect(db_file)
        setup.execute(_DDL)
        setup.executemany(
            "INSERT INTO EVENTS (EVENT, STATE, STATUS, JOB_ID, TIME, METADATA) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            _SEED_ROWS,
        )
        setup.commit()
        setup.close()

        result = _execute_query(
            db_file,
            "SELECT COUNT(*) AS n FROM EVENTS",
            timeout_secs=5,
            max_rows=10,
        )
        assert result["error"] if "error" in result else True  # no error key expected
        assert result["row_count"] == 1
        assert result["rows"][0]["n"] == len(_SEED_ROWS)

    def test_truncation_flag(self, tmp_path: Any) -> None:
        """Rows beyond *max_rows* set ``truncated=True``."""
        db_file = str(tmp_path / "test.db")
        setup = sqlite3.connect(db_file)
        setup.execute(_DDL)
        setup.executemany(
            "INSERT INTO EVENTS (EVENT, STATE, STATUS, JOB_ID, TIME, METADATA) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            _SEED_ROWS,
        )
        setup.commit()
        setup.close()

        result = _execute_query(
            db_file,
            "SELECT * FROM EVENTS",
            timeout_secs=5,
            max_rows=2,
        )
        assert result["truncated"] is True
        assert result["row_count"] == 2

    def test_write_blocked(self, tmp_path: Any) -> None:
        """Write operations against the DB raise an error (read-only mode)."""
        db_file = str(tmp_path / "test.db")
        setup = sqlite3.connect(db_file)
        setup.execute(_DDL)
        setup.commit()
        setup.close()

        with pytest.raises(Exception):
            _execute_query(
                db_file,
                "INSERT INTO EVENTS VALUES (99,'X','Y','Z','J',0.0,'{}')",
                timeout_secs=5,
                max_rows=10,
            )


# ---------------------------------------------------------------------------
# fetch_and_analyse — full pipeline
# ---------------------------------------------------------------------------


class TestFetchAndAnalysePipeline:
    """End-to-end pipeline tests with a temp-file SQLite DB and mocked LLMs."""

    def _make_db(self, tmp_path: Any) -> str:
        """Create a seeded temp-file SQLite DB and return its path.

        Args:
            tmp_path: pytest tmp_path fixture.

        Returns:
            Path string for the created database file.
        """
        db_file = str(tmp_path / "cgsim.db")
        setup = sqlite3.connect(db_file)
        setup.execute(_DDL)
        setup.executemany(
            "INSERT INTO EVENTS (EVENT, STATE, STATUS, JOB_ID, TIME, METADATA) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            _SEED_ROWS,
        )
        setup.commit()
        setup.close()
        return db_file

    def test_happy_path_count(self, tmp_path: Any) -> None:
        """A count query returns the correct row count and a summary."""
        db_file = self._make_db(tmp_path)
        sql = "SELECT COUNT(*) AS n FROM EVENTS WHERE EVENT='JobExecution' AND STATE='Finished'"
        summary_text = "There are 2 finished JobExecution events."

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
            patch("askcgsim.sim_query_impl._call_llm_for_summary",
                  _make_summary_patch(summary_text)),
        ):
            result = asyncio.run(fetch_and_analyse("How many jobs finished?", db_file))

        assert result["error"] is None
        assert result["row_count"] == 1
        assert result["rows"][0]["n"] == 2
        assert result["summary"] == summary_text
        # The guard normalises whitespace and adds LIMIT, so check for key fragments.
        assert result["sql"] is not None
        assert "COUNT(*)" in result["sql"]
        assert "EVENTS" in result["sql"].upper()

    def test_happy_path_rows(self, tmp_path: Any) -> None:
        """A row-returning query populates columns and rows correctly."""
        db_file = self._make_db(tmp_path)
        sql = (
            "SELECT JOB_ID, json_extract(METADATA, '$.duration') AS dur "
            "FROM EVENTS WHERE EVENT='JobExecution' AND STATE='Finished' "
            "ORDER BY dur DESC LIMIT 200"
        )

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
            patch("askcgsim.sim_query_impl._call_llm_for_summary",
                  _make_summary_patch()),
        ):
            result = asyncio.run(fetch_and_analyse("List job durations", db_file))

        assert result["error"] is None
        assert result["row_count"] == 2
        assert "JOB_ID" in result["columns"]
        assert "dur" in result["columns"]

    def test_sql_appears_in_evidence(self, tmp_path: Any) -> None:
        """The sanitised SQL is always present in the evidence dict."""
        db_file = self._make_db(tmp_path)
        sql = "SELECT * FROM EVENTS LIMIT 5"

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
            patch("askcgsim.sim_query_impl._call_llm_for_summary",
                  _make_summary_patch()),
        ):
            result = asyncio.run(fetch_and_analyse("Show events", db_file))

        assert result["sql"] is not None
        assert "EVENTS" in result["sql"].upper()

    def test_zero_rows_returned(self, tmp_path: Any) -> None:
        """A query matching no rows returns row_count=0 and no error."""
        db_file = self._make_db(tmp_path)
        sql = "SELECT * FROM EVENTS WHERE JOB_ID='NONEXISTENT' LIMIT 200"

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
            patch("askcgsim.sim_query_impl._call_llm_for_summary",
                  _make_summary_patch("No events found.")),
        ):
            result = asyncio.run(fetch_and_analyse("Events for missing job?", db_file))

        assert result["error"] is None
        assert result["row_count"] == 0
        assert result["rows"] == []

    def test_cannot_answer_sentinel(self) -> None:
        """CANNOT_ANSWER sentinel produces a structured error, not a crash."""
        with patch(
            "askcgsim.sim_query_impl._call_llm_for_sql",
            _make_llm_patch(CANNOT_ANSWER_SENTINEL),
        ):
            result = asyncio.run(
                fetch_and_analyse("What is the answer to life?", "irrelevant.db")
            )

        assert result["error"] is not None
        assert "translate" in result["error"].lower() or "rephrase" in result["error"].lower()
        assert result["sql"] is None
        assert result["row_count"] == 0

    def test_natural_language_refusal(self) -> None:
        """Natural-language refusals from the LLM are caught correctly."""
        with patch(
            "askcgsim.sim_query_impl._call_llm_for_sql",
            _make_llm_patch("I cannot generate SQL for that."),
        ):
            result = asyncio.run(
                fetch_and_analyse("Philosophical question", "irrelevant.db")
            )

        assert result["error"] is not None
        assert result["sql"] is None

    def test_guard_rejection_produces_structured_evidence(self) -> None:
        """SQL rejected by the guard produces a guard_rejection key."""
        with patch(
            "askcgsim.sim_query_impl._call_llm_for_sql",
            _make_llm_patch("DROP TABLE EVENTS"),
        ):
            result = asyncio.run(
                fetch_and_analyse("Delete all events", "irrelevant.db")
            )

        assert result["error"] is not None
        assert result["guard_rejection"] is not None
        assert result["row_count"] == 0

    def test_db_not_found_returns_structured_evidence(self) -> None:
        """A missing database file returns a descriptive error, not an exception."""
        sql = "SELECT COUNT(*) FROM EVENTS LIMIT 200"

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
        ):
            result = asyncio.run(
                fetch_and_analyse("How many events?", "/nonexistent/path/cgsim.db")
            )

        assert result["error"] is not None
        assert "not found" in result["error"].lower() or "CGSIM_DB_PATH" in result["error"]

    def test_wrong_database_no_events_table(self, tmp_path: Any) -> None:
        """A file that exists but has no EVENTS table returns a clear error."""
        db_file = str(tmp_path / "wrong.db")
        # Create a SQLite file with a different schema.
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()

        sql = "SELECT COUNT(*) AS n FROM EVENTS LIMIT 200"

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
        ):
            result = asyncio.run(fetch_and_analyse("How many events?", db_file))

        assert result["error"] is not None
        assert "EVENTS" in result["error"] or "cgsim" in result["error"].lower()

    def test_llm_sql_fail_returns_structured_evidence(self) -> None:
        """An LLM call failure during SQL generation returns a structured error."""
        failing_llm = AsyncMock(side_effect=RuntimeError("LLM offline"))

        with patch("askcgsim.sim_query_impl._call_llm_for_sql", failing_llm):
            result = asyncio.run(
                fetch_and_analyse("How many jobs?", "irrelevant.db")
            )

        assert result["error"] is not None
        assert result["sql"] is None

    def test_summarisation_failure_is_non_fatal(self, tmp_path: Any) -> None:
        """A summarisation LLM failure does not abort the pipeline."""
        db_file = self._make_db(tmp_path)
        sql = "SELECT COUNT(*) AS n FROM EVENTS LIMIT 200"
        failing_summary = AsyncMock(side_effect=RuntimeError("Summary LLM offline"))

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
            patch("askcgsim.sim_query_impl._call_llm_for_summary", failing_summary),
        ):
            result = asyncio.run(fetch_and_analyse("Count events", db_file))

        # Row data should still be present even if summary is missing.
        assert result["row_count"] == 1
        assert result["summary"] is None
        assert result["error"] is None

    def test_sql_fence_stripping_in_pipeline(self, tmp_path: Any) -> None:
        """SQL wrapped in markdown fences is stripped correctly before execution."""
        db_file = self._make_db(tmp_path)
        sql_with_fences = "```sql\nSELECT COUNT(*) AS n FROM EVENTS\n```"

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql",
                  _make_llm_patch(sql_with_fences)),
            patch("askcgsim.sim_query_impl._call_llm_for_summary",
                  _make_summary_patch()),
        ):
            result = asyncio.run(fetch_and_analyse("Count all events", db_file))

        assert result["error"] is None
        assert result["row_count"] == 1


# ---------------------------------------------------------------------------
# CgsimSimQueryTool.call()
# ---------------------------------------------------------------------------


class TestCgsimSimQueryToolCall:
    """Tests for :class:`CgsimSimQueryTool` ``.call()``."""

    def _bamboo_text_content(self, text: str) -> list[dict[str, str]]:
        """Build a fake text_content return value matching bamboo's format.

        Args:
            text: JSON-serialised evidence string.

        Returns:
            One-element list matching the real ``text_content`` return shape.
        """
        return [{"type": "text", "text": text}]

    @pytest.mark.asyncio
    async def test_missing_question_returns_error(self) -> None:
        """An empty question argument returns a structured error."""
        import types
        fake_base = types.ModuleType("bamboo.tools.base")
        fake_base.text_content = self._bamboo_text_content  # type: ignore[attr-defined]

        import sys
        with patch.dict(sys.modules, {"bamboo": types.ModuleType("bamboo"),
                                      "bamboo.tools": types.ModuleType("bamboo.tools"),
                                      "bamboo.tools.base": fake_base}):
            result = await cgsim_sim_query_tool.call({})
        assert len(result) == 1
        payload = json.loads(result[0]["text"])
        assert "error" in payload["evidence"]
        assert "required" in payload["evidence"]["error"].lower()

    @pytest.mark.asyncio
    async def test_question_too_long_returns_error(self) -> None:
        """A question exceeding 2000 characters returns a structured error."""
        import types
        fake_base = types.ModuleType("bamboo.tools.base")
        fake_base.text_content = self._bamboo_text_content  # type: ignore[attr-defined]

        import sys
        with patch.dict(sys.modules, {"bamboo": types.ModuleType("bamboo"),
                                      "bamboo.tools": types.ModuleType("bamboo.tools"),
                                      "bamboo.tools.base": fake_base}):
            result = await cgsim_sim_query_tool.call({"question": "x" * 2001})
        assert len(result) == 1
        payload = json.loads(result[0]["text"])
        assert "error" in payload["evidence"]
        assert "long" in payload["evidence"]["error"].lower()

    @pytest.mark.asyncio
    async def test_db_not_found_via_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CGSIM_DB_PATH pointing to a missing file returns a descriptive error."""
        import sys
        import types

        monkeypatch.setenv("CGSIM_DB_PATH", "/nonexistent/cgsim.db")
        sql = "SELECT COUNT(*) AS n FROM EVENTS LIMIT 200"

        fake_base = types.ModuleType("bamboo.tools.base")
        fake_base.text_content = self._bamboo_text_content  # type: ignore[attr-defined]

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
            patch.dict(sys.modules, {"bamboo": types.ModuleType("bamboo"),
                                     "bamboo.tools": types.ModuleType("bamboo.tools"),
                                     "bamboo.tools.base": fake_base}),
        ):
            result = await cgsim_sim_query_tool.call(
                {"question": "How many events?"}
            )

        payload = json.loads(result[0]["text"])
        assert "error" in payload["evidence"]
        assert payload["evidence"]["error"] is not None

    @pytest.mark.asyncio
    async def test_successful_call(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful call returns evidence wrapped in the standard envelope."""
        import sys
        import types

        db_file = str(tmp_path / "cgsim.db")
        setup = sqlite3.connect(db_file)
        setup.execute(_DDL)
        setup.executemany(
            "INSERT INTO EVENTS (EVENT, STATE, STATUS, JOB_ID, TIME, METADATA) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            _SEED_ROWS,
        )
        setup.commit()
        setup.close()

        monkeypatch.setenv("CGSIM_DB_PATH", db_file)
        sql = "SELECT COUNT(*) AS n FROM EVENTS LIMIT 200"

        fake_base = types.ModuleType("bamboo.tools.base")
        fake_base.text_content = self._bamboo_text_content  # type: ignore[attr-defined]

        with (
            patch("askcgsim.sim_query_impl._call_llm_for_sql", _make_llm_patch(sql)),
            patch("askcgsim.sim_query_impl._call_llm_for_summary",
                  _make_summary_patch("There are 5 events.")),
            patch.dict(sys.modules, {"bamboo": types.ModuleType("bamboo"),
                                     "bamboo.tools": types.ModuleType("bamboo.tools"),
                                     "bamboo.tools.base": fake_base}),
        ):
            result = await cgsim_sim_query_tool.call(
                {"question": "How many events are there?"}
            )

        assert len(result) == 1
        payload = json.loads(result[0]["text"])
        evidence = payload["evidence"]
        assert evidence["error"] is None
        assert evidence["row_count"] == 1
        assert evidence["summary"] == "There are 5 events."

    def test_singleton_is_correct_type(self) -> None:
        """Module-level singleton ``cgsim_sim_query_tool`` has the right type."""
        from askcgsim.sim_query_impl import CgsimSimQueryTool

        assert isinstance(cgsim_sim_query_tool, CgsimSimQueryTool)

    def test_get_definition_shape(self) -> None:
        """``get_definition()`` returns a valid MCP tool definition."""
        defn = cgsim_sim_query_tool.get_definition()
        assert defn["name"] == "cgsim.sim_query"
        assert "question" in defn["inputSchema"]["properties"]
        assert defn["inputSchema"]["required"] == ["question"]
        assert defn["inputSchema"]["additionalProperties"] is False

    def test_description_mentions_simulation(self) -> None:
        """Tool description references simulation-specific concepts."""
        desc = cgsim_sim_query_tool.get_definition()["description"]
        assert "CGSim" in desc
        assert "simulation" in desc.lower()


# ---------------------------------------------------------------------------
# Schema context and prompt builders
# ---------------------------------------------------------------------------


class TestSchemaContext:
    """Schema context builder returns a useful, non-empty string."""

    def setup_method(self) -> None:
        """Reset the schema cache before each test."""
        invalidate_schema_cache()

    def test_build_schema_context_returns_string(self) -> None:
        """build_schema_context returns a non-empty string."""
        ctx = build_schema_context()
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_schema_mentions_events_table(self) -> None:
        """Schema context mentions the EVENTS table."""
        ctx = build_schema_context()
        assert "EVENTS" in ctx

    def test_schema_mentions_json_extract(self) -> None:
        """Schema context includes json_extract guidance."""
        ctx = build_schema_context()
        assert "json_extract" in ctx

    def test_schema_mentions_cost_exclusion(self) -> None:
        """Schema context explicitly warns that the cost field is excluded."""
        ctx = build_schema_context()
        assert "cost" in ctx.lower()

    def test_schema_cached(self) -> None:
        """Repeated calls return the identical string (cached)."""
        ctx1 = build_schema_context()
        ctx2 = build_schema_context()
        assert ctx1 is ctx2

    def test_invalidate_clears_cache(self) -> None:
        """invalidate_schema_cache causes a fresh build on next call."""
        ctx1 = build_schema_context()
        invalidate_schema_cache()
        ctx2 = build_schema_context()
        # Content should be equal but may or may not be the same object.
        assert ctx1 == ctx2


class TestPromptBuilders:
    """Prompt builder functions return correct message structures."""

    def test_build_sql_prompt_has_two_messages(self) -> None:
        """build_sql_prompt returns system + user messages."""
        msgs = build_sql_prompt("How many jobs?")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "How many jobs?" in msgs[1]["content"]

    def test_build_sql_prompt_system_mentions_cannot_answer(self) -> None:
        """SQL generation system prompt includes the CANNOT_ANSWER sentinel."""
        msgs = build_sql_prompt("test")
        assert CANNOT_ANSWER_SENTINEL in msgs[0]["content"]

    def test_build_sql_prompt_system_mentions_no_cost(self) -> None:
        """SQL generation system prompt warns against using the cost field."""
        msgs = build_sql_prompt("test")
        assert "cost" in msgs[0]["content"].lower()

    def test_build_summarise_prompt_has_two_messages(self) -> None:
        """build_summarise_prompt returns system + user messages."""
        msgs = build_summarise_prompt("How many?", "SELECT COUNT(*) FROM EVENTS", '{"rows":[]}')
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_build_summarise_prompt_embeds_question(self) -> None:
        """Summarisation system prompt embeds the original question."""
        question = "How long did job J-001 take?"
        msgs = build_summarise_prompt(question, "SELECT 1", '{}')
        assert question in msgs[0]["content"]

    def test_build_summarise_prompt_embeds_sql(self) -> None:
        """Summarisation system prompt embeds the executed SQL."""
        sql = "SELECT json_extract(METADATA, '$.duration') FROM EVENTS LIMIT 10"
        msgs = build_summarise_prompt("question", sql, '{}')
        assert sql in msgs[0]["content"]


# ---------------------------------------------------------------------------
# sim_query.py re-export wrapper
# ---------------------------------------------------------------------------


class TestSimQueryReExport:
    """The thin re-export wrapper exposes the expected symbols."""

    def test_imports_successfully(self) -> None:
        """sim_query can be imported without error."""
        import askcgsim.sim_query as sq  # noqa: F401

    def test_singleton_exported(self) -> None:
        """cgsim_sim_query_tool is exported from sim_query."""
        from askcgsim.sim_query import cgsim_sim_query_tool as tool
        from askcgsim.sim_query_impl import CgsimSimQueryTool

        assert isinstance(tool, CgsimSimQueryTool)

    def test_get_definition_exported(self) -> None:
        """get_definition is callable from sim_query."""
        from askcgsim.sim_query import get_definition

        defn = get_definition()
        assert defn["name"] == "cgsim.sim_query"
