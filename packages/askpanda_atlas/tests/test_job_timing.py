"""Tests for ``job_timing_schema`` and ``job_timing_impl``.

Covers:

* Schema constants and field registry sanity checks.
* :func:`parse_llm_params` — valid JSON, sentinel, refusals, bad fields.
* :func:`_default_window` — window length and ordering.
* :func:`_error_evidence` and :func:`_cannot_answer_evidence` structure.
* :func:`fetch_job_timing` with OpenSearch mocked.
* :class:`PandaJobTimingTool.call` end-to-end with LLM and OpenSearch mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from askpanda_atlas.job_timing_schema import (
    ALL_FIELD_NAMES,
    CACHE_PREFIX,
    CANNOT_ANSWER_SENTINEL,
    DEFAULT_FIELD,
    DEFAULT_METRIC,
    DEFAULT_WINDOW_HOURS,
    INDEX_PATTERN,
    NUMERIC_FIELDS,
    TIMING_FIELDS,
    VALID_METRICS,
    build_query_prompt,
)
from askpanda_atlas.job_timing_impl import (
    PandaJobTimingTool,
    _cannot_answer_evidence,
    _default_window,
    _error_evidence,
    fetch_job_timing,
    panda_job_timing_tool,
    parse_llm_params,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unpack(result: list[Any]) -> dict[str, Any]:
    """Deserialise the JSON-wrapped MCP content returned by the tool.

    Args:
        result: Return value of ``PandaJobTimingTool.call()``.

    Returns:
        Deserialised dict with an ``"evidence"`` key.
    """
    return json.loads(result[0]["text"])


def _mock_os_response(value: float | None, doc_count: int = 100) -> MagicMock:
    """Build a mock OpenSearch aggregation response.

    Args:
        value: Aggregation result value (or ``None`` for empty result).
        doc_count: Number of matching documents.

    Returns:
        MagicMock with ``hits.total.value`` and ``aggregations`` attributes.
    """
    resp = MagicMock()
    resp.hits.total.value = doc_count
    resp.aggregations.__getitem__.return_value.value = value
    return resp


def _patch_os(response: MagicMock):
    """Return a context-manager stack that mocks OpenSearch for fetch_job_timing.

    Args:
        response: Mock response object to return from ``Search.execute()``.
    """
    import unittest.mock as _mock

    mock_search = MagicMock()
    mock_search.return_value.extra.return_value = mock_search.return_value
    mock_search.return_value.filter.return_value = mock_search.return_value
    mock_search.return_value.aggs.metric.return_value = MagicMock()
    mock_search.return_value.execute.return_value = response
    mock_os_dsl = MagicMock()
    mock_os_dsl.Search = mock_search

    return _mock.patch.dict(sys.modules, {"opensearch_dsl": mock_os_dsl})


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------


class TestSchemaConstants:
    """Sanity checks on the schema module constants."""

    def test_index_pattern_has_wildcard(self) -> None:
        """INDEX_PATTERN ends with '*' for multi-index coverage."""
        assert INDEX_PATTERN.endswith("-*")

    def test_timing_fields_non_empty(self) -> None:
        """TIMING_FIELDS contains at least the confirmed batch-1 fields."""
        assert len(TIMING_FIELDS) >= 18  # 8 core + 10 timing

    def test_all_field_names_match_timing_fields(self) -> None:
        """ALL_FIELD_NAMES equals the set of names in TIMING_FIELDS."""
        expected = {name for name, *_ in TIMING_FIELDS}
        assert ALL_FIELD_NAMES == expected

    def test_numeric_fields_subset_of_all(self) -> None:
        """NUMERIC_FIELDS is a proper subset of ALL_FIELD_NAMES."""
        assert NUMERIC_FIELDS < ALL_FIELD_NAMES

    def test_default_field_is_numeric(self) -> None:
        """DEFAULT_FIELD is in NUMERIC_FIELDS."""
        assert DEFAULT_FIELD in NUMERIC_FIELDS

    def test_default_metric_is_valid(self) -> None:
        """DEFAULT_METRIC is in VALID_METRICS."""
        assert DEFAULT_METRIC in VALID_METRICS

    def test_valid_metrics_contains_expected(self) -> None:
        """VALID_METRICS contains all five expected aggregation types."""
        assert VALID_METRICS == {"avg", "sum", "min", "max", "value_count"}

    def test_pilottiming_subfields_present(self) -> None:
        """All six parsed pilottiming sub-fields are in TIMING_FIELDS."""
        subfields = {
            "pilottiming_getjob",
            "pilottiming_stagein",
            "pilottiming_payload",
            "pilottiming_stageout",
            "pilottiming_initial_setup",
            "pilottiming_payload_setup",
        }
        assert subfields <= ALL_FIELD_NAMES

    def test_pilottiming_subfields_are_numeric(self) -> None:
        """All six pilottiming sub-fields are in NUMERIC_FIELDS."""
        subfields = {
            "pilottiming_getjob",
            "pilottiming_stagein",
            "pilottiming_payload",
            "pilottiming_stageout",
            "pilottiming_initial_setup",
            "pilottiming_payload_setup",
        }
        assert subfields <= NUMERIC_FIELDS

    def test_cache_prefix_is_unique(self) -> None:
        """CACHE_PREFIX does not collide with known other prefixes."""
        assert CACHE_PREFIX == "job_timing:"
        assert CACHE_PREFIX != "harvester_timeseries:"

    def test_build_query_prompt_returns_two_messages(self) -> None:
        """build_query_prompt returns a system + user message list."""
        msgs = build_query_prompt("What is the average stage-in time at BNL?")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_build_query_prompt_embeds_question(self) -> None:
        """User message contains the exact question string."""
        q = "How long does stage-out take at CERN?"
        msgs = build_query_prompt(q)
        assert msgs[1]["content"] == q

    def test_build_query_prompt_embeds_field_names(self) -> None:
        """System message references key field names."""
        msgs = build_query_prompt("test")
        system = msgs[0]["content"]
        assert "pilottiming_stagein" in system
        assert "job_walltime" in system


# ---------------------------------------------------------------------------
# _default_window
# ---------------------------------------------------------------------------


class TestDefaultWindow:
    """Unit tests for :func:`_default_window`."""

    def test_returns_two_strings(self) -> None:
        """Returns a 2-tuple of ISO-8601 strings."""
        from_dt, to_dt = _default_window()
        assert isinstance(from_dt, str)
        assert isinstance(to_dt, str)

    def test_from_before_to(self) -> None:
        """from_dt is strictly before to_dt."""
        from_dt, to_dt = _default_window()
        assert from_dt < to_dt

    def test_window_matches_default_hours(self) -> None:
        """Window spans approximately DEFAULT_WINDOW_HOURS hours."""
        fmt = "%Y-%m-%dT%H:%M:%S"
        from_dt, to_dt = _default_window()
        t0 = datetime.strptime(from_dt, fmt).replace(tzinfo=timezone.utc)
        t1 = datetime.strptime(to_dt, fmt).replace(tzinfo=timezone.utc)
        expected_secs = DEFAULT_WINDOW_HOURS * 3600
        diff = (t1 - t0).total_seconds()
        assert abs(diff - expected_secs) < 5


# ---------------------------------------------------------------------------
# parse_llm_params
# ---------------------------------------------------------------------------


class TestParseLlmParams:
    """Unit tests for :func:`parse_llm_params`."""

    def test_valid_json_avg(self) -> None:
        """Valid JSON with avg metric and known field parses correctly."""
        raw = json.dumps({
            "metric": "avg",
            "field": "pilottiming_stagein",
            "site": "BNL",
        })
        result = parse_llm_params(raw)
        assert result is not None
        assert result["metric"] == "avg"
        assert result["field"] == "pilottiming_stagein"
        assert result["site"] == "BNL"

    def test_valid_json_all_fields(self) -> None:
        """All optional fields are parsed when present."""
        raw = json.dumps({
            "metric": "sum",
            "field": "job_walltime",
            "site": "CERN",
            "jobstatus": "finished",
            "jeditaskid": 12345,
            "from_dt": "2026-06-01T00:00:00",
            "to_dt": "2026-06-07T00:00:00",
        })
        result = parse_llm_params(raw)
        assert result is not None
        assert result["metric"] == "sum"
        assert result["jobstatus"] == "finished"
        assert result["jeditaskid"] == 12345
        assert result["from_dt"] == "2026-06-01T00:00:00"

    def test_cannot_answer_sentinel_returns_none(self) -> None:
        """CANNOT_ANSWER sentinel returns None."""
        assert parse_llm_params(CANNOT_ANSWER_SENTINEL) is None

    def test_cannot_answer_lowercase_returns_none(self) -> None:
        """Lowercase 'cannot_answer' also returns None."""
        assert parse_llm_params("cannot_answer") is None

    def test_refusal_phrase_returns_none(self) -> None:
        """Natural-language refusal returns None."""
        assert parse_llm_params("I cannot answer this question.") is None

    def test_invalid_json_returns_none(self) -> None:
        """Malformed JSON returns None."""
        assert parse_llm_params("{not valid json}") is None

    def test_non_dict_json_returns_none(self) -> None:
        """JSON array (not object) returns None."""
        assert parse_llm_params("[1, 2, 3]") is None

    def test_unknown_metric_falls_back_to_default(self) -> None:
        """Unknown metric is replaced with DEFAULT_METRIC."""
        raw = json.dumps({"metric": "median", "field": "job_walltime"})
        result = parse_llm_params(raw)
        assert result is not None
        assert result["metric"] == DEFAULT_METRIC

    def test_non_numeric_field_falls_back_to_default(self) -> None:
        """Non-numeric field (e.g. computingsite) is replaced with DEFAULT_FIELD."""
        raw = json.dumps({"metric": "avg", "field": "computingsite"})
        result = parse_llm_params(raw)
        assert result is not None
        assert result["field"] == DEFAULT_FIELD

    def test_markdown_fences_stripped(self) -> None:
        """JSON wrapped in ```json fences is parsed correctly."""
        inner = json.dumps({"metric": "min", "field": "job_queuetime"})
        raw = f"```json\n{inner}\n```"
        result = parse_llm_params(raw)
        assert result is not None
        assert result["metric"] == "min"

    def test_missing_optional_keys_are_none(self) -> None:
        """Missing optional keys are set to None, not missing."""
        raw = json.dumps({"metric": "avg", "field": "job_walltime"})
        result = parse_llm_params(raw)
        assert result is not None
        assert result["site"] is None
        assert result["jobstatus"] is None
        assert result["jeditaskid"] is None
        assert result["from_dt"] is None
        assert result["to_dt"] is None

    def test_jeditaskid_coerced_to_int(self) -> None:
        """jeditaskid given as string is coerced to int."""
        raw = json.dumps({"metric": "avg", "field": "job_walltime", "jeditaskid": "99"})
        result = parse_llm_params(raw)
        assert result is not None
        assert result["jeditaskid"] == 99

    def test_invalid_jeditaskid_becomes_none(self) -> None:
        """Non-numeric jeditaskid becomes None."""
        raw = json.dumps({"metric": "avg", "field": "job_walltime", "jeditaskid": "not-an-int"})
        result = parse_llm_params(raw)
        assert result is not None
        assert result["jeditaskid"] is None


# ---------------------------------------------------------------------------
# _error_evidence and _cannot_answer_evidence
# ---------------------------------------------------------------------------


class TestErrorEvidence:
    """Unit tests for structured error constructors."""

    def test_error_evidence_structure(self) -> None:
        """_error_evidence returns expected keys with error populated."""
        ev = _error_evidence(
            "avg", "job_walltime", "BNL", "finished", None,
            "2026-06-01T00:00:00", "2026-06-08T00:00:00",
            detail="test error",
        )
        assert ev["error"] is not None
        assert ev["value"] is None
        assert ev["doc_count"] == 0
        assert ev["metric"] == "avg"
        assert ev["field"] == "job_walltime"
        assert ev["site_filter"] == "BNL"
        assert ev["jobstatus_filter"] == "finished"
        assert "ASKPANDA_OPENSEARCH" in ev["error"]

    def test_error_evidence_does_not_expose_detail(self) -> None:
        """Internal detail is not included in the user-facing error message."""
        ev = _error_evidence(
            "avg", "job_walltime", None, None, None, None, None,
            detail="secret internal error xyz",
        )
        assert "secret internal error xyz" not in ev["error"]

    def test_cannot_answer_evidence_structure(self) -> None:
        """_cannot_answer_evidence returns expected keys with error populated."""
        ev = _cannot_answer_evidence("What is the CPU count per site?")
        assert ev["error"] is not None
        assert ev["value"] is None
        assert ev["question"] == "What is the CPU count per site?"
        assert "timing" in ev["error"].lower()


# ---------------------------------------------------------------------------
# fetch_job_timing (OpenSearch mocked)
# ---------------------------------------------------------------------------


class TestFetchJobTiming:
    """Unit tests for :func:`fetch_job_timing` with OpenSearch mocked."""

    def _run(
        self,
        value: float | None,
        doc_count: int = 100,
        metric: str = "avg",
        field: str = "job_walltime",
        site: str | None = None,
        jobstatus: str | None = None,
        jeditaskid: int | None = None,
        from_dt: str | None = "2026-06-01T00:00:00",
        to_dt: str | None = "2026-06-08T00:00:00",
    ) -> dict[str, Any]:
        """Execute fetch_job_timing with patched OpenSearch.

        Args:
            value: Aggregation value to return from the mock.
            doc_count: Document count to return.
            metric: Aggregation metric.
            field: Field to aggregate.
            site: Optional site filter.
            jobstatus: Optional job status filter.
            jeditaskid: Optional task ID filter.
            from_dt: Lower time bound.
            to_dt: Upper time bound.

        Returns:
            Evidence dict from fetch_job_timing.
        """
        mock_response = _mock_os_response(value, doc_count)

        with (
            patch(
                "askpanda_atlas.job_timing_impl._create_os_client",
                return_value=MagicMock(),
            ),
            patch.dict(os.environ, {"ASKPANDA_OPENSEARCH": "test-password"}),
            _patch_os(mock_response),
        ):
            from askpanda_atlas._cache import invalidate
            cache_key = (
                f"job_timing:{metric}|{field}|{site or ''}|"
                f"{jobstatus or ''}|{jeditaskid or ''}|"
                f"{from_dt or ''}|{to_dt or ''}"
            )
            invalidate(cache_key)
            return fetch_job_timing(
                metric, field, site, jobstatus, jeditaskid, from_dt, to_dt
            )

    def test_avg_returns_value(self) -> None:
        """avg aggregation returns the mocked value."""
        result = self._run(value=42.5)
        assert result["value"] == 42.5
        assert result["error"] is None

    def test_sum_metric(self) -> None:
        """sum aggregation stores metric name correctly."""
        result = self._run(value=1000.0, metric="sum")
        assert result["metric"] == "sum"

    def test_value_count_metric(self) -> None:
        """value_count metric is stored and value returned."""
        result = self._run(value=500, metric="value_count", field="pandaid")
        assert result["metric"] == "value_count"
        assert result["field"] == "pandaid"

    def test_none_value_when_no_docs(self) -> None:
        """None aggregation value is propagated correctly."""
        result = self._run(value=None, doc_count=0)
        assert result["value"] is None
        assert result["doc_count"] == 0

    def test_site_filter_stored(self) -> None:
        """Site filter is reflected in evidence."""
        result = self._run(value=10.0, site="BNL")
        assert result["site_filter"] == "BNL"

    def test_jobstatus_filter_stored(self) -> None:
        """Job status filter is reflected in evidence."""
        result = self._run(value=10.0, jobstatus="finished")
        assert result["jobstatus_filter"] == "finished"

    def test_jeditaskid_filter_stored(self) -> None:
        """JEDI task ID filter is reflected in evidence."""
        result = self._run(value=10.0, jeditaskid=99999)
        assert result["jeditaskid_filter"] == 99999

    def test_endpoint_is_index_pattern(self) -> None:
        """Evidence endpoint matches INDEX_PATTERN."""
        result = self._run(value=1.0)
        assert result["endpoint"] == INDEX_PATTERN

    def test_missing_password_raises(self) -> None:
        """RuntimeError is raised when ASKPANDA_OPENSEARCH is not set."""
        from askpanda_atlas._cache import clear as _clear
        _clear()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.dict(sys.modules, {"opensearch_dsl": MagicMock()}),
        ):
            os.environ.pop("ASKPANDA_OPENSEARCH", None)
            with pytest.raises(RuntimeError, match="ASKPANDA_OPENSEARCH"):
                fetch_job_timing("avg", "job_walltime")


# ---------------------------------------------------------------------------
# PandaJobTimingTool.call()
# ---------------------------------------------------------------------------


class TestPandaJobTimingTool:
    """Integration tests for :class:`PandaJobTimingTool`."""

    def _call(
        self,
        arguments: dict[str, Any],
        llm_reply: str,
        os_value: float | None = 42.0,
        os_doc_count: int = 500,
    ) -> dict[str, Any]:
        """Run tool.call() with patched LLM and OpenSearch.

        Args:
            arguments: Tool input arguments.
            llm_reply: Raw string the LLM returns.
            os_value: Aggregation value from OpenSearch mock.
            os_doc_count: Document count from OpenSearch mock.

        Returns:
            Deserialised evidence wrapper dict.
        """
        mock_text_content = lambda s: [{"type": "text", "text": s}]  # noqa: E731
        mock_response = _mock_os_response(os_value, os_doc_count)

        with (
            patch(
                "askpanda_atlas.job_timing_impl._call_llm_for_params",
                new=AsyncMock(return_value=llm_reply),
            ),
            patch(
                "askpanda_atlas.job_timing_impl._create_os_client",
                return_value=MagicMock(),
            ),
            patch(
                "bamboo.tools.base.text_content",
                side_effect=mock_text_content,
            ),
            patch.dict(os.environ, {"ASKPANDA_OPENSEARCH": "test-password"}),
            _patch_os(mock_response),
        ):
            from askpanda_atlas._cache import clear as _clear
            _clear()
            tool = PandaJobTimingTool()
            return _unpack(asyncio.run(tool.call(arguments)))

    def test_successful_call_returns_value(self) -> None:
        """Successful call includes aggregation value in evidence."""
        llm_reply = json.dumps({"metric": "avg", "field": "pilottiming_stagein"})
        result = self._call({"question": "What is the avg stagein time?"}, llm_reply)
        ev = result["evidence"]
        assert ev["error"] is None
        assert ev["value"] == 42.0
        assert ev["field"] == "pilottiming_stagein"

    def test_cannot_answer_returns_error_evidence(self) -> None:
        """CANNOT_ANSWER from LLM returns error evidence, does not raise."""
        result = self._call(
            {"question": "What is the CPU count per site?"},
            llm_reply=CANNOT_ANSWER_SENTINEL,
        )
        ev = result["evidence"]
        assert ev["error"] is not None
        assert ev["value"] is None

    def test_argument_site_override(self) -> None:
        """site argument overrides LLM-extracted site."""
        llm_reply = json.dumps({"metric": "avg", "field": "job_walltime", "site": "CERN"})
        result = self._call(
            {"question": "avg wall time?", "site": "BNL"},
            llm_reply,
        )
        # site should have been overridden to BNL
        ev = result["evidence"]
        assert ev["site_filter"] == "BNL"

    def test_default_time_window_applied(self) -> None:
        """Default time window is applied when LLM does not specify one."""
        llm_reply = json.dumps({"metric": "avg", "field": "job_walltime"})
        result = self._call({"question": "avg wall time?"}, llm_reply)
        ev = result["evidence"]
        assert ev["from_dt"] is not None
        assert ev["to_dt"] is not None

    def test_empty_question_returns_error(self) -> None:
        """Empty question returns an error without calling the LLM."""
        mock_tc = lambda s: [{"type": "text", "text": s}]  # noqa: E731
        with patch("bamboo.tools.base.text_content", side_effect=mock_tc):
            tool = PandaJobTimingTool()
            result = _unpack(asyncio.run(tool.call({"question": "  "})))
        assert result["evidence"]["error"] is not None

    def test_llm_failure_returns_error_evidence(self) -> None:
        """LLM call failure returns error evidence, does not raise."""
        mock_tc = lambda s: [{"type": "text", "text": s}]  # noqa: E731
        with (
            patch(
                "askpanda_atlas.job_timing_impl._call_llm_for_params",
                new=AsyncMock(side_effect=RuntimeError("LLM down")),
            ),
            patch("bamboo.tools.base.text_content", side_effect=mock_tc),
        ):
            tool = PandaJobTimingTool()
            result = _unpack(asyncio.run(tool.call({"question": "avg stagein?"})))
        assert result["evidence"]["error"] is not None

    def test_singleton_instance(self) -> None:
        """Module-level singleton is a PandaJobTimingTool."""
        assert isinstance(panda_job_timing_tool, PandaJobTimingTool)

    def test_get_definition_name(self) -> None:
        """Tool definition name is 'panda_job_timing'."""
        tool = PandaJobTimingTool()
        assert tool.get_definition()["name"] == "panda_job_timing"

    def test_get_definition_has_required_question(self) -> None:
        """Tool definition marks question as required."""
        tool = PandaJobTimingTool()
        schema = tool.get_definition()["inputSchema"]
        assert "question" in schema["required"]
