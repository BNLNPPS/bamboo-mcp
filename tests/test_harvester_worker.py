"""Tests for ``harvester_worker_impl``.

Covers the pure helper functions (URL building, aggregation), the
synchronous ``fetch_worker_stats`` path (HTTP mocked), and the async
``PandaHarvesterWorkersTool.call()`` entry point.

All HTTP mocks return the 4-tuple
``(status_code, content_type, body_text, parsed_json_or_none)``
expected by ``cached_fetch_jsonish``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from askpanda_atlas.harvester_worker_impl import (
    PandaHarvesterWorkersTool,
    _aggregate_evidence,
    _default_window,
    _error_evidence,
    _extract_records,
    _safe_int,
    build_harvester_url,
    fetch_worker_stats,
    panda_harvester_workers_tool,
)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _ok_list(records: list[dict[str, Any]]) -> tuple[int, str, str, dict[str, Any]]:
    """Return a successful 4-tuple wrapping a list payload as a dict.

    ``fetch_jsonish`` wraps a top-level JSON array as ``{"_data": [...]}``.
    This helper mimics that behaviour so tests do not need to patch the
    wrapping logic.

    Args:
        records: List of worker-stat record dicts.

    Returns:
        ``(200, "application/json", json_text, {"_data": records})``
    """
    body = json.dumps(records)
    return (200, "application/json", body, {"_data": records})


def _err_response(status: int = 500, body: str = "") -> tuple[int, str, str, None]:
    """Return an error 4-tuple with no parsed JSON.

    Args:
        status: HTTP status code.
        body: Response body text.

    Returns:
        ``(status, "text/html", body, None)``
    """
    return (status, "text/html", body, None)


def _html_response(body: str = "<html>maintenance</html>") -> tuple[int, str, str, None]:
    """Return a 200-OK 4-tuple with a non-JSON HTML body.

    Args:
        body: HTML body text.

    Returns:
        ``(200, "text/html", body, None)``
    """
    return (200, "text/html", body, None)


# ---------------------------------------------------------------------------
# Record factories
# ---------------------------------------------------------------------------


def _make_record(
    nworkers: int = 10,
    status: str = "running",
    jobtype: str = "managed",
    resourcetype: str = "SCORE",
    computingsite: str = "BNL",
    harvesterid: str = "harvester-bnl-01",
) -> dict[str, Any]:
    """Return a minimal Harvester worker-stat record.

    Args:
        nworkers: Worker (pilot) count.
        status: Worker status string.
        jobtype: Job type string.
        resourcetype: Resource type string.
        computingsite: Computing site name.
        harvesterid: Harvester instance identifier.

    Returns:
        Record dict matching the Harvester API shape.
    """
    return {
        "nworkers": nworkers,
        "status": status,
        "jobtype": jobtype,
        "resourcetype": resourcetype,
        "computingsite": computingsite,
        "harvesterid": harvesterid,
    }


def _unpack(result: list[Any]) -> dict[str, Any]:
    """Deserialise the JSON-wrapped MCP content returned by the tool.

    Args:
        result: Return value of ``PandaHarvesterWorkersTool.call()``.

    Returns:
        Deserialised dict with an ``"evidence"`` key.
    """
    return json.loads(result[0]["text"])


# ---------------------------------------------------------------------------
# _safe_int
# ---------------------------------------------------------------------------


class TestSafeInt:
    """Unit tests for :func:`_safe_int`."""

    def test_int_passthrough(self) -> None:
        """Integer input is returned unchanged."""
        assert _safe_int(42) == 42

    def test_string_coerced(self) -> None:
        """Numeric string is coerced to int."""
        assert _safe_int("7") == 7

    def test_none_returns_none(self) -> None:
        """None input yields None."""
        assert _safe_int(None) is None

    def test_bad_string_returns_none(self) -> None:
        """Non-numeric string yields None."""
        assert _safe_int("abc") is None

    def test_float_truncated(self) -> None:
        """Float is truncated to int."""
        assert _safe_int(3.9) == 3


# ---------------------------------------------------------------------------
# build_harvester_url
# ---------------------------------------------------------------------------


class TestBuildHarvesterUrl:
    """Unit tests for :func:`build_harvester_url`."""

    def test_basic_url_structure(self) -> None:
        """URL contains the expected path and query parameters."""
        url = build_harvester_url(
            "https://bigpanda.cern.ch",
            "2026-03-01T00:00:00",
            "2026-03-02T00:00:00",
        )
        assert "/harvester/getworkerstats/" in url
        assert "lastupdate_from=2026-03-01T00:00:00" in url
        assert "lastupdate_to=2026-03-02T00:00:00" in url

    def test_site_appended_when_provided(self) -> None:
        """Site filter is included when *site* is not None."""
        url = build_harvester_url(
            "https://bigpanda.cern.ch",
            "2026-03-01T00:00:00",
            "2026-03-02T00:00:00",
            site="BNL",
        )
        assert "computingsite=BNL" in url

    def test_no_site_param_when_none(self) -> None:
        """``computingsite`` is absent when *site* is None."""
        url = build_harvester_url(
            "https://bigpanda.cern.ch",
            "2026-03-01T00:00:00",
            "2026-03-02T00:00:00",
        )
        assert "computingsite" not in url

    def test_trailing_slash_stripped(self) -> None:
        """Trailing slash on base_url does not produce a double slash."""
        url = build_harvester_url(
            "https://bigpanda.cern.ch/",
            "2026-03-01T00:00:00",
            "2026-03-02T00:00:00",
        )
        assert "//" not in url.replace("https://", "")


# ---------------------------------------------------------------------------
# _default_window
# ---------------------------------------------------------------------------


class TestDefaultWindow:
    """Unit tests for :func:`_default_window`."""

    def test_returns_two_strings(self) -> None:
        """Return value is a 2-tuple of strings."""
        from_dt, to_dt = _default_window()
        assert isinstance(from_dt, str)
        assert isinstance(to_dt, str)

    def test_from_is_before_to(self) -> None:
        """*from_dt* is strictly earlier than *to_dt*."""
        from_dt, to_dt = _default_window()
        assert from_dt < to_dt

    def test_format_matches_iso(self) -> None:
        """Both timestamps match the expected ISO-8601 format."""
        from datetime import datetime

        from_dt, to_dt = _default_window()
        # Will raise ValueError if format is wrong.
        datetime.fromisoformat(from_dt)
        datetime.fromisoformat(to_dt)


# ---------------------------------------------------------------------------
# _extract_records
# ---------------------------------------------------------------------------


class TestExtractRecords:
    """Unit tests for :func:`_extract_records`."""

    def test_extracts_from_data_key(self) -> None:
        """Records are extracted from ``{"_data": [...]}`` payload."""
        records = [_make_record(), _make_record(status="idle")]
        result = _extract_records({"_data": records})
        assert len(result) == 2

    def test_non_dict_items_filtered(self) -> None:
        """Non-dict items inside ``_data`` are silently dropped."""
        result = _extract_records({"_data": [{"a": 1}, "bad", None]})
        assert result == [{"a": 1}]

    def test_empty_data_returns_empty(self) -> None:
        """Empty ``_data`` list returns an empty list."""
        assert _extract_records({"_data": []}) == []

    def test_fallback_to_first_list_value(self) -> None:
        """When ``_data`` is absent, the first list value is used."""
        records = [_make_record()]
        result = _extract_records({"workers": records})
        assert result == records

    def test_no_list_returns_empty(self) -> None:
        """A payload with no list values returns an empty list."""
        assert _extract_records({"count": 0, "status": "ok"}) == []


# ---------------------------------------------------------------------------
# _aggregate_evidence
# ---------------------------------------------------------------------------


class TestAggregateEvidence:
    """Unit tests for :func:`_aggregate_evidence`."""

    def test_total_nworkers_correct(self) -> None:
        """Total worker count equals the sum across all records."""
        records = [
            _make_record(nworkers=20, status="running"),
            _make_record(nworkers=5, status="idle"),
            _make_record(nworkers=3, status="failed"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", "BNL")
        assert ev["nworkers_total"] == 28

    def test_breakdown_by_status(self) -> None:
        """``nworkers_by_status`` groups counts correctly."""
        records = [
            _make_record(nworkers=10, status="running"),
            _make_record(nworkers=4, status="running"),
            _make_record(nworkers=2, status="idle"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert ev["nworkers_by_status"]["running"] == 14
        assert ev["nworkers_by_status"]["idle"] == 2

    def test_breakdown_by_jobtype(self) -> None:
        """``nworkers_by_jobtype`` groups counts correctly."""
        records = [
            _make_record(nworkers=6, jobtype="managed"),
            _make_record(nworkers=4, jobtype="user"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert ev["nworkers_by_jobtype"]["managed"] == 6
        assert ev["nworkers_by_jobtype"]["user"] == 4

    def test_breakdown_by_resourcetype(self) -> None:
        """``nworkers_by_resourcetype`` groups counts correctly."""
        records = [
            _make_record(nworkers=8, resourcetype="SCORE"),
            _make_record(nworkers=3, resourcetype="MCORE"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert ev["nworkers_by_resourcetype"]["SCORE"] == 8
        assert ev["nworkers_by_resourcetype"]["MCORE"] == 3

    def test_breakdown_by_site(self) -> None:
        """``nworkers_by_site`` groups counts correctly."""
        records = [
            _make_record(nworkers=15, computingsite="BNL"),
            _make_record(nworkers=5, computingsite="CERN"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert ev["nworkers_by_site"]["BNL"] == 15
        assert ev["nworkers_by_site"]["CERN"] == 5

    def test_empty_records(self) -> None:
        """Empty records list produces zeroed counters."""
        ev = _aggregate_evidence([], "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert ev["nworkers_total"] == 0
        assert ev["nworkers_by_status"] == {}

    def test_total_records_counted_correctly(self) -> None:
        """``total_records`` reflects the actual number of records received."""
        records = [_make_record(nworkers=1) for _ in range(7)]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert ev["total_records"] == 7

    def test_records_not_in_evidence_dict(self) -> None:
        """Raw records are NOT present in the evidence dict sent to the LLM."""
        records = [_make_record(nworkers=1) for _ in range(3)]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert "records" not in ev
        assert "records_truncated" not in ev

    def test_nworkers_none_skipped(self) -> None:
        """Records with None nworkers are silently skipped."""
        records = [
            {"nworkers": None, "status": "running", "jobtype": "managed",
             "resourcetype": "SCORE", "computingsite": "BNL"},
            _make_record(nworkers=5),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert ev["nworkers_total"] == 5

    def test_from_to_and_site_filter_preserved(self) -> None:
        """Time window and site filter are stored verbatim in the evidence."""
        ev = _aggregate_evidence(
            [], "2026-03-01T00:00:00", "2026-03-02T00:00:00", "BNL"
        )
        assert ev["from_dt"] == "2026-03-01T00:00:00"
        assert ev["to_dt"] == "2026-03-02T00:00:00"
        assert ev["site_filter"] == "BNL"

    def test_sorted_by_count_descending(self) -> None:
        """Breakdown dicts are sorted by count descending."""
        records = [
            _make_record(nworkers=1, status="idle"),
            _make_record(nworkers=100, status="running"),
            _make_record(nworkers=10, status="failed"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        statuses = list(ev["nworkers_by_status"].keys())
        assert statuses[0] == "running"
        assert statuses[-1] == "idle"

    def test_pivot_present_in_evidence(self) -> None:
        """``pivot`` is always present in the evidence dict."""
        ev = _aggregate_evidence([], "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert "pivot" in ev
        assert isinstance(ev["pivot"], list)

    def test_no_old_cross_tabs_in_evidence(self) -> None:
        """Old pairwise cross-tab keys are not present — replaced by pivot."""
        ev = _aggregate_evidence([], "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        assert "nworkers_by_status_and_resourcetype" not in ev
        assert "nworkers_by_status_and_site" not in ev

    def test_pivot_three_way_slice(self) -> None:
        """Pivot rows allow exact three-way filtering: status+jobtype+resourcetype."""
        records = [
            _make_record(nworkers=45, status="running", jobtype="managed", resourcetype="MCORE"),
            _make_record(nworkers=30, status="running", jobtype="managed", resourcetype="SCORE"),
            _make_record(nworkers=12, status="running", jobtype="user", resourcetype="MCORE"),
            _make_record(nworkers=10, status="idle", jobtype="managed", resourcetype="MCORE"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", "BNL")
        pivot = ev["pivot"]

        def _find(status: str, jobtype: str, resourcetype: str) -> int:
            """Return nworkers for an exact pivot row match."""
            for row in pivot:
                if (row["status"] == status and
                        row["jobtype"] == jobtype and
                        row["resourcetype"] == resourcetype):
                    return row["nworkers"]
            return 0

        # Two-way slices
        assert _find("running", "managed", "MCORE") == 45
        assert _find("running", "managed", "SCORE") == 30
        # Three-way question: running MCORE managed pilots
        assert _find("running", "managed", "MCORE") == 45
        # Jobtype=user variant
        assert _find("running", "user", "MCORE") == 12
        # Idle
        assert _find("idle", "managed", "MCORE") == 10

    def test_pivot_summed_across_harvesters_and_sites(self) -> None:
        """Pivot aggregates across different harvesterids and computingsites."""
        records = [
            _make_record(nworkers=20, status="running", jobtype="managed",
                         resourcetype="MCORE", computingsite="BNL", harvesterid="h1"),
            _make_record(nworkers=15, status="running", jobtype="managed",
                         resourcetype="MCORE", computingsite="BNL", harvesterid="h2"),
            _make_record(nworkers=10, status="running", jobtype="managed",
                         resourcetype="MCORE", computingsite="CERN", harvesterid="h3"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        pivot = ev["pivot"]
        # All three records collapse to one pivot row: running/managed/MCORE = 45
        assert len(pivot) == 1
        assert pivot[0]["nworkers"] == 45

    def test_pivot_sorted_by_nworkers_descending(self) -> None:
        """Pivot rows are sorted by nworkers descending."""
        records = [
            _make_record(nworkers=5, status="idle", jobtype="managed", resourcetype="SCORE"),
            _make_record(nworkers=50, status="running", jobtype="managed", resourcetype="MCORE"),
            _make_record(nworkers=20, status="running", jobtype="user", resourcetype="SCORE"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        counts = [row["nworkers"] for row in ev["pivot"]]
        assert counts == sorted(counts, reverse=True)

    def test_pivot_consistent_with_flat_status_breakdown(self) -> None:
        """Sum of nworkers in pivot rows for a given status matches nworkers_by_status."""
        records = [
            _make_record(nworkers=45, status="running", jobtype="managed", resourcetype="MCORE"),
            _make_record(nworkers=30, status="running", jobtype="managed", resourcetype="SCORE"),
            _make_record(nworkers=10, status="idle", jobtype="managed", resourcetype="MCORE"),
        ]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", None)
        pivot_running = sum(r["nworkers"] for r in ev["pivot"] if r["status"] == "running")
        assert pivot_running == ev["nworkers_by_status"]["running"]

    def test_evidence_is_json_serialisable(self) -> None:
        """The evidence dict round-trips through json.dumps without error."""
        records = [_make_record(nworkers=5)]
        ev = _aggregate_evidence(records, "2026-01-01T00:00:00", "2026-01-01T01:00:00", "BNL")
        assert json.loads(json.dumps(ev))["nworkers_total"] == 5


# ---------------------------------------------------------------------------
# _error_evidence
# ---------------------------------------------------------------------------


class TestErrorEvidence:
    """Unit tests for :func:`_error_evidence`."""

    def test_structure(self) -> None:
        """Error evidence contains all expected top-level keys."""
        ev = _error_evidence("2026-01-01T00:00:00", "2026-01-01T01:00:00", "BNL", "oops")
        for key in (
            "total_records", "nworkers_total",
            "nworkers_by_status", "from_dt", "to_dt", "site_filter", "error",
        ):
            assert key in ev, f"Missing key: {key}"
        assert "records" not in ev

    def test_error_field_populated(self) -> None:
        """``error`` field is a non-empty string."""
        ev = _error_evidence("a", "b", None, "boom")
        assert isinstance(ev["error"], str) and ev["error"]

    def test_counters_zeroed(self) -> None:
        """All numeric counters are zero."""
        ev = _error_evidence("a", "b", None, "boom")
        assert ev["nworkers_total"] == 0
        assert ev["total_records"] == 0

    def test_json_serialisable(self) -> None:
        """Error evidence dict is JSON-serialisable."""
        ev = _error_evidence("2026-01-01T00:00:00", "2026-01-01T01:00:00", "BNL", "e")
        assert isinstance(json.loads(json.dumps(ev)), dict)


# ---------------------------------------------------------------------------
# fetch_worker_stats (HTTP mocked)
# ---------------------------------------------------------------------------


class TestFetchWorkerStats:
    """Unit tests for :func:`fetch_worker_stats`."""

    def test_returns_evidence_with_correct_counts(self) -> None:
        """Successful fetch returns aggregated evidence."""
        records = [
            _make_record(nworkers=30, status="running"),
            _make_record(nworkers=5, status="idle"),
        ]
        with patch(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            MagicMock(return_value=_ok_list(records)),
        ):
            ev = fetch_worker_stats(
                "https://bigpanda.cern.ch",
                "2026-03-01T00:00:00",
                "2026-04-01T00:00:00",
                "BNL",
            )

        assert ev["nworkers_total"] == 35
        assert ev["nworkers_by_status"]["running"] == 30
        assert ev["site_filter"] == "BNL"
        assert ev["endpoint"] is not None

    def test_endpoint_url_contains_site_filter(self) -> None:
        """The URL passed to the cache includes the ``computingsite`` param."""
        captured: list[str] = []

        def _capture(url: str, ttl: float = 30.0) -> tuple[int, str, str, dict[str, Any]]:
            """Capture the URL and return an empty record list."""
            captured.append(url)
            return _ok_list([])

        with patch("askpanda_atlas._cache.cached_fetch_jsonish", _capture):
            fetch_worker_stats(
                "https://bigpanda.cern.ch",
                "2026-03-01T00:00:00",
                "2026-03-02T00:00:00",
                "BNL",
            )

        assert len(captured) == 1
        assert "computingsite=BNL" in captured[0]
        assert "lastupdate_from=" in captured[0]

    def test_raises_on_http_error(self) -> None:
        """RuntimeError is raised on a non-2xx HTTP response."""
        with patch(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            MagicMock(return_value=_err_response(503)),
        ):
            with pytest.raises(RuntimeError, match="HTTP 503"):
                fetch_worker_stats(
                    "https://bigpanda.cern.ch",
                    "2026-03-01T00:00:00",
                    "2026-03-02T00:00:00",
                )

    def test_raises_on_non_json_response(self) -> None:
        """RuntimeError is raised when the API returns HTML."""
        with patch(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            MagicMock(return_value=_html_response()),
        ):
            with pytest.raises(RuntimeError, match="Non-JSON response"):
                fetch_worker_stats(
                    "https://bigpanda.cern.ch",
                    "2026-03-01T00:00:00",
                    "2026-03-02T00:00:00",
                )

    def test_raw_payload_stored_in_evidence(self) -> None:
        """``raw_payload`` in the evidence dict holds the original records."""
        records = [_make_record(nworkers=7)]
        with patch(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            MagicMock(return_value=_ok_list(records)),
        ):
            ev = fetch_worker_stats(
                "https://bigpanda.cern.ch",
                "2026-03-01T00:00:00",
                "2026-03-02T00:00:00",
            )

        assert ev["raw_payload"] == records

    def test_empty_record_list_is_valid(self) -> None:
        """An empty record list returns a zeroed evidence dict without error."""
        with patch(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            MagicMock(return_value=_ok_list([])),
        ):
            ev = fetch_worker_stats(
                "https://bigpanda.cern.ch",
                "2026-03-01T00:00:00",
                "2026-03-02T00:00:00",
            )

        assert ev["nworkers_total"] == 0
        assert ev["error"] is None


# ---------------------------------------------------------------------------
# PandaHarvesterWorkersTool.call() — via singleton
# ---------------------------------------------------------------------------


class TestToolCall:
    """Integration tests for ``panda_harvester_workers_tool.call()``."""

    def test_call_success_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful call returns evidence with correct nworkers_total."""
        records = [
            _make_record(nworkers=50, status="running", computingsite="BNL"),
            _make_record(nworkers=10, status="idle", computingsite="BNL"),
        ]
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, ttl=30.0: _ok_list(records),
        )
        monkeypatch.setenv("PANDA_BASE_URL", "https://bigpanda.cern.ch")

        result = asyncio.run(panda_harvester_workers_tool.call({
            "question": "How many pilots are running at BNL?",
            "site": "BNL",
        }))
        ev = _unpack(result)["evidence"]

        assert ev["nworkers_total"] == 60
        assert ev["nworkers_by_status"]["running"] == 50
        assert ev["site_filter"] == "BNL"
        assert ev["error"] is None

    def test_call_defaults_to_last_hour_when_no_timestamps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no timestamps are supplied the URL contains a one-hour window."""
        captured: list[str] = []

        def _capture(url: str, ttl: float = 30.0) -> tuple[int, str, str, dict[str, Any]]:
            """Capture the request URL and return empty records."""
            captured.append(url)
            return _ok_list([])

        monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _capture)

        asyncio.run(panda_harvester_workers_tool.call({
            "question": "How many pilots are running?",
        }))

        assert len(captured) == 1
        assert "lastupdate_from=" in captured[0]
        assert "lastupdate_to=" in captured[0]

    def test_call_uses_explicit_timestamps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit from_dt / to_dt are forwarded to the URL verbatim."""
        captured: list[str] = []

        def _capture(url: str, ttl: float = 30.0) -> tuple[int, str, str, dict[str, Any]]:
            """Capture the request URL and return empty records."""
            captured.append(url)
            return _ok_list([])

        monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _capture)

        asyncio.run(panda_harvester_workers_tool.call({
            "question": "Pilots yesterday",
            "from_dt": "2026-03-24T00:00:00",
            "to_dt": "2026-03-25T00:00:00",
        }))

        assert "lastupdate_from=2026-03-24T00:00:00" in captured[0]
        assert "lastupdate_to=2026-03-25T00:00:00" in captured[0]

    def test_call_http_error_returns_error_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP failure returns error evidence instead of raising."""
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, ttl=30.0: _err_response(503),
        )

        result = asyncio.run(panda_harvester_workers_tool.call({
            "question": "How many pilots at BNL?",
            "site": "BNL",
        }))
        ev = _unpack(result)["evidence"]

        assert ev["error"] is not None
        assert ev["nworkers_total"] == 0

    def test_call_non_json_returns_error_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTML (non-JSON) response returns error evidence."""
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, ttl=30.0: _html_response(),
        )

        result = asyncio.run(panda_harvester_workers_tool.call({
            "question": "Pilot count?",
        }))
        ev = _unpack(result)["evidence"]
        assert ev["error"] is not None

    def test_call_result_is_json_serialisable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The full tool result round-trips through json.dumps."""
        records = [_make_record(nworkers=10)]
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, ttl=30.0: _ok_list(records),
        )

        result = asyncio.run(panda_harvester_workers_tool.call({
            "question": "Pilot counts?",
        }))
        assert isinstance(json.loads(result[0]["text"]), dict)

    def test_call_no_site_fetches_all_sites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting *site* does not append a computingsite parameter."""
        captured: list[str] = []

        def _capture(url: str, ttl: float = 30.0) -> tuple[int, str, str, dict[str, Any]]:
            """Capture the request URL and return empty records."""
            captured.append(url)
            return _ok_list([])

        monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _capture)

        asyncio.run(panda_harvester_workers_tool.call({"question": "All pilots?"}))

        assert "computingsite" not in captured[0]

    def test_call_empty_site_string_treated_as_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty string for *site* is treated the same as None."""
        captured: list[str] = []

        def _capture(url: str, ttl: float = 30.0) -> tuple[int, str, str, dict[str, Any]]:
            """Capture the request URL and return empty records."""
            captured.append(url)
            return _ok_list([])

        monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _capture)

        asyncio.run(panda_harvester_workers_tool.call({
            "question": "All pilots?",
            "site": "",
        }))

        assert "computingsite" not in captured[0]

    def test_call_exception_from_thread_returns_error_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any unexpected exception in the thread returns error evidence."""
        def _boom(url: str, ttl: float = 30.0) -> None:
            """Raise an unexpected error."""
            raise ConnectionError("unreachable")

        monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _boom)

        result = asyncio.run(panda_harvester_workers_tool.call({
            "question": "Pilots?",
        }))
        ev = _unpack(result)["evidence"]
        assert ev["error"] is not None

    def test_singleton_is_correct_type(self) -> None:
        """Module-level singleton is a PandaHarvesterWorkersTool instance."""
        assert isinstance(panda_harvester_workers_tool, PandaHarvesterWorkersTool)

    def test_get_definition_has_required_keys(self) -> None:
        """``get_definition()`` returns a dict with name, description, inputSchema."""
        defn = panda_harvester_workers_tool.get_definition()
        assert defn["name"] == "panda_harvester_workers"
        assert "description" in defn
        assert "inputSchema" in defn
        assert "question" in defn["inputSchema"]["properties"]


# ---------------------------------------------------------------------------
# _extract_time_window_from_question
# ---------------------------------------------------------------------------


class TestExtractTimeWindow:
    """Unit tests for :func:`bamboo_answer._extract_time_window_from_question`."""

    @staticmethod
    def _fn(q: str) -> "tuple[str, str] | None":
        """Import and call the function under test."""
        from bamboo.tools.bamboo_answer import _extract_time_window_from_question
        return _extract_time_window_from_question(q)

    def test_returns_none_for_right_now(self) -> None:
        """'right now' returns None so the tool uses its own default."""
        assert self._fn("How many pilots are running right now?") is None

    def test_returns_none_for_currently(self) -> None:
        """'currently' returns None."""
        assert self._fn("How many pilots are currently running at BNL?") is None

    def test_returns_none_for_empty_temporal(self) -> None:
        """No temporal expression returns None."""
        assert self._fn("How many pilots are running at BNL?") is None

    def test_last_n_hours(self) -> None:
        """'last N hours' returns a window of N hours ending now."""
        from datetime import datetime, timezone
        result = self._fn("How many pilots failed in the last 6 hours?")
        assert result is not None
        from_dt, to_dt = result
        t_from = datetime.fromisoformat(from_dt).replace(tzinfo=timezone.utc)
        t_to = datetime.fromisoformat(to_dt).replace(tzinfo=timezone.utc)
        delta = t_to - t_from
        assert abs(delta.total_seconds() - 6 * 3600) < 5

    def test_past_n_hours(self) -> None:
        """'past N hours' is equivalent to 'last N hours'."""
        result = self._fn("Pilots running in the past 3 hours at AGLT2?")
        assert result is not None
        from datetime import datetime, timezone
        t_from = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
        t_to = datetime.fromisoformat(result[1]).replace(tzinfo=timezone.utc)
        assert abs((t_to - t_from).total_seconds() - 3 * 3600) < 5

    def test_last_24_hours(self) -> None:
        """'last 24 hours' returns a 24-hour window."""
        result = self._fn("How many pilots ran in the last 24 hours?")
        assert result is not None
        from datetime import datetime, timezone
        t_from = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
        t_to = datetime.fromisoformat(result[1]).replace(tzinfo=timezone.utc)
        assert abs((t_to - t_from).total_seconds() - 24 * 3600) < 5

    def test_last_n_minutes(self) -> None:
        """'last N minutes' returns a window of N minutes."""
        result = self._fn("Pilots submitted in the last 30 minutes?")
        assert result is not None
        from datetime import datetime, timezone
        t_from = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
        t_to = datetime.fromisoformat(result[1]).replace(tzinfo=timezone.utc)
        assert abs((t_to - t_from).total_seconds() - 30 * 60) < 5

    def test_last_n_days(self) -> None:
        """'last N days' returns a window of N days."""
        result = self._fn("How many pilots failed in the last 3 days?")
        assert result is not None
        from datetime import datetime, timezone
        t_from = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
        t_to = datetime.fromisoformat(result[1]).replace(tzinfo=timezone.utc)
        assert abs((t_to - t_from).total_seconds() - 3 * 86400) < 5

    def test_yesterday(self) -> None:
        """'since yesterday' returns a window starting at midnight yesterday."""
        from datetime import datetime, timedelta, timezone
        result = self._fn("How many MCORE pilots ran at BNL since yesterday?")
        assert result is not None
        from_dt, to_dt = result
        t_from = datetime.fromisoformat(from_dt).replace(tzinfo=timezone.utc)
        # from_dt must be midnight yesterday (time = 00:00:00)
        assert t_from.hour == 0 and t_from.minute == 0 and t_from.second == 0
        # from_dt must be approximately yesterday
        now = datetime.now(tz=timezone.utc)
        yesterday_midnight = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        assert abs((t_from - yesterday_midnight).total_seconds()) < 5

    def test_bare_yesterday(self) -> None:
        """'yesterday' without 'since' prefix is also recognised."""
        result = self._fn("How many pilots failed yesterday at CERN?")
        assert result is not None
        from datetime import datetime, timezone
        t_from = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
        assert t_from.hour == 0 and t_from.minute == 0

    def test_today(self) -> None:
        """'today' returns a window starting at midnight today UTC."""
        from datetime import datetime, timezone
        result = self._fn("How many pilots ran today?")
        assert result is not None
        t_from = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
        assert t_from.hour == 0 and t_from.minute == 0 and t_from.second == 0
        now = datetime.now(tz=timezone.utc)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        assert abs((t_from - today_midnight).total_seconds()) < 5

    def test_explicit_iso_range(self) -> None:
        """Explicit ISO range is passed through verbatim preserving original case."""
        result = self._fn(
            "Pilots between 2026-03-24T00:00:00 and 2026-03-25T00:00:00 at BNL?"
        )
        assert result is not None
        assert "2026-03-24" in result[0]
        assert "2026-03-25" in result[1]

    def test_output_format_is_iso_without_timezone(self) -> None:
        """Returned strings match YYYY-MM-DDTHH:MM:SS format (no tz suffix)."""
        import re
        result = self._fn("pilots in the last 2 hours")
        assert result is not None
        pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        assert pat.match(result[0]), f"from_dt format wrong: {result[0]}"
        assert pat.match(result[1]), f"to_dt format wrong: {result[1]}"

    def test_case_insensitive(self) -> None:
        """Pattern matching is case-insensitive."""
        assert self._fn("PILOTS IN THE LAST 4 HOURS AT BNL?") is not None
        assert self._fn("Since Yesterday pilots at CERN?") is not None


# ---------------------------------------------------------------------------
# Routing integration: time window reaches the tool arguments
# ---------------------------------------------------------------------------


class TestPilotRoutingWithTimeWindow:
    """Verify that _build_deterministic_plan passes from_dt/to_dt to the tool."""

    @staticmethod
    def _plan(question: str) -> "dict":
        """Return the first tool call's arguments for a pilot question."""
        from bamboo.tools.bamboo_answer import _build_deterministic_plan
        plan = _build_deterministic_plan(question, None, None)
        assert plan is not None
        return plan.tool_calls[0].arguments

    def test_right_now_has_no_timestamps(self) -> None:
        """'right now' produces no from_dt/to_dt — tool uses its default."""
        args = self._plan("How many pilots are running right now at BNL?")
        assert "from_dt" not in args
        assert "to_dt" not in args

    def test_since_yesterday_injects_timestamps(self) -> None:
        """'since yesterday' injects from_dt and to_dt into plan arguments."""
        args = self._plan("How many MCORE pilots are running at BNL since yesterday?")
        assert "from_dt" in args
        assert "to_dt" in args
        from datetime import datetime, timezone
        t_from = datetime.fromisoformat(args["from_dt"]).replace(tzinfo=timezone.utc)
        assert t_from.hour == 0 and t_from.minute == 0

    def test_last_6_hours_injects_correct_window(self) -> None:
        """'last 6 hours' injects a ~6-hour window."""
        from datetime import datetime, timezone
        args = self._plan("How many pilots failed at AGLT2 in the last 6 hours?")
        assert "from_dt" in args and "to_dt" in args
        t_from = datetime.fromisoformat(args["from_dt"]).replace(tzinfo=timezone.utc)
        t_to = datetime.fromisoformat(args["to_dt"]).replace(tzinfo=timezone.utc)
        assert abs((t_to - t_from).total_seconds() - 6 * 3600) < 5

    def test_site_and_timestamp_both_present(self) -> None:
        """Site and time window are both extracted and passed together."""
        args = self._plan("How many pilots ran at CERN in the last 3 hours?")
        assert args.get("site") == "CERN"
        assert "from_dt" in args
        assert "to_dt" in args

    def test_site_extraction_unknown_site_pattern(self) -> None:
        """Pattern-based extractor picks up sites not in the fallback list."""
        from bamboo.tools.bamboo_answer import _extract_site_from_question
        assert _extract_site_from_question(
            "What are the pilot and job failure rates at MWT2?"
        ) == "MWT2"

    def test_site_extraction_with_separator(self) -> None:
        """Sites with hyphens and underscores are extracted correctly."""
        from bamboo.tools.bamboo_answer import _extract_site_from_question
        assert _extract_site_from_question("Status for site SLAC-SCS") == "SLAC-SCS"
        assert _extract_site_from_question("Pilots for queue CERN_PROD") == "CERN_PROD"
        assert _extract_site_from_question("Pilots at SWT2_CPB") == "SWT2_CPB"

    def test_site_extraction_no_false_positives(self) -> None:
        """Generic words after prepositions are not extracted as sites."""
        from bamboo.tools.bamboo_answer import _extract_site_from_question
        assert _extract_site_from_question(
            "How many pilots are running at the site?"
        ) is None
        assert _extract_site_from_question("Status for site here") is None


# ---------------------------------------------------------------------------
# _is_site_health_question
# ---------------------------------------------------------------------------


class TestIsSiteHealthQuestion:
    """Unit tests for :func:`bamboo_answer._is_site_health_question`."""

    @staticmethod
    def _fn(q: str) -> bool:
        """Import and call the function under test."""
        from bamboo.tools.bamboo_answer import _is_site_health_question
        return _is_site_health_question(q)

    # --- should be True ---

    def test_pilots_and_failed_jobs(self) -> None:
        """Explicit mention of both pilots and failed jobs triggers site-health."""
        assert self._fn(
            "How many pilots and how many failed jobs are there at BNL right now?"
        ) is True

    def test_job_failure_rates_with_pilot(self) -> None:
        """'job failure rates' alongside pilot signal triggers site-health."""
        assert self._fn(
            "What are the pilot and job failure rates at BNL?"
        ) is True

    def test_bare_jobs_word_with_pilot(self) -> None:
        """Bare 'jobs' word alongside pilot signal is sufficient for site-health."""
        assert self._fn("How is BNL doing — pilots and jobs?") is True

    def test_running_pilots_and_job_failures(self) -> None:
        """'running pilots' + 'job failures' triggers site-health."""
        assert self._fn("How many running pilots and job failures at CERN?") is True

    def test_pilot_count_and_job_error_rate(self) -> None:
        """'pilot count' + 'job error' triggers site-health."""
        assert self._fn("What is the pilot count and job error rate at AGLT2?") is True

    def test_summary_with_pilots_and_failed_jobs(self) -> None:
        """Site summary phrasing with pilots and failed jobs triggers site-health."""
        assert self._fn("Give me a BNL summary — pilots and failed jobs") is True

    def test_pilots_and_which_jobs_failing(self) -> None:
        """'pilots' + 'which jobs are failing' triggers site-health."""
        assert self._fn("How many pilots and which jobs are failing at BNL?") is True

    # --- should be False (pure pilot) ---

    def test_pure_pilot_running_at(self) -> None:
        """Pure pilot question with 'running at' must not trigger site-health."""
        assert self._fn("How many pilots are running at BNL?") is False

    def test_pure_pilot_failed_at(self) -> None:
        """'pilots failed at' must not trigger site-health."""
        assert self._fn("How many pilots failed at BNL in the last hour?") is False

    def test_pure_pilot_ran_at(self) -> None:
        """'pilots ran at' must not trigger site-health."""
        assert self._fn("How many pilots ran at AGLT2 yesterday?") is False

    def test_pure_pilot_idle(self) -> None:
        """'pilots idle' must not trigger site-health."""
        assert self._fn("How many pilots are idle at CERN?") is False

    # --- should be False (pure jobs) ---

    def test_pure_jobs_failed(self) -> None:
        """Pure jobs question must not trigger site-health."""
        assert self._fn("How many jobs failed at BNL?") is False

    def test_pure_jobs_failed_at(self) -> None:
        """'failed at' without pilot signal must not trigger site-health."""
        assert self._fn("How many failed at AGLT2?") is False

    # --- should be False (task keyword) ---

    def test_task_keyword_excluded(self) -> None:
        """Questions with 'task' are excluded from site-health routing."""
        assert self._fn(
            "How many pilots and failed jobs are there for task 12345?"
        ) is False


# ---------------------------------------------------------------------------
# Site-health two-tool routing integration
# ---------------------------------------------------------------------------


class TestSiteHealthRouting:
    """Verify that site-health questions produce a two-tool plan."""

    @staticmethod
    def _tools(question: str) -> list[str]:
        """Return the tool names from the deterministic plan for *question*.

        For site-health questions the plan is built inside
        ``_run_fast_path_intercepts`` rather than ``_build_deterministic_plan``,
        so we import and call the intercept function directly with a null
        history.
        """
        import asyncio
        from bamboo.tools.bamboo_answer import _run_fast_path_intercepts
        from unittest.mock import patch

        captured: list[list[str]] = []

        async def _fake_execute(plan, q, history, **kw):  # type: ignore[misc]
            """Capture tool names instead of executing the plan."""
            captured.append([tc.tool for tc in plan.tool_calls])
            return [{"type": "text", "text": "stub"}]

        with patch(
            "bamboo.tools.bamboo_answer.execute_plan",
            side_effect=_fake_execute,
        ):
            asyncio.run(_run_fast_path_intercepts(question, []))

        return captured[0] if captured else []

    def test_site_health_calls_both_tools(self) -> None:
        """Site-health question routes to both harvester and jobs query."""
        tools = self._tools(
            "How many pilots and how many failed jobs are there at BNL right now?"
        )
        assert "panda_harvester_workers" in tools
        assert "panda_jobs_query" in tools

    def test_site_health_harvester_before_jobs(self) -> None:
        """Harvester workers is called before jobs query in the plan."""
        tools = self._tools(
            "How many running pilots and job failures at CERN?"
        )
        assert tools.index("panda_harvester_workers") < tools.index("panda_jobs_query")

    def test_pure_pilot_calls_only_harvester(self) -> None:
        """Pure pilot question routes to harvester only, not jobs query."""
        tools = self._tools("How many pilots are running at BNL?")
        assert "panda_harvester_workers" in tools
        assert "panda_jobs_query" not in tools

    def test_pure_jobs_calls_only_jobs_query(self) -> None:
        """Pure jobs question routes to jobs query only, not harvester."""
        tools = self._tools("How many jobs failed at BNL?")
        assert "panda_jobs_query" in tools
        assert "panda_harvester_workers" not in tools

    def test_site_health_passes_queue_to_jobs_query(self) -> None:
        """Site extracted from question is passed as 'queue' to panda_jobs_query."""
        import asyncio
        from bamboo.tools.bamboo_answer import _run_fast_path_intercepts
        from unittest.mock import patch

        captured_args: list[dict] = []

        async def _fake_execute(plan, q, history, **kw):  # type: ignore[misc]
            """Capture tool call arguments from the plan."""
            for tc in plan.tool_calls:
                captured_args.append({"tool": tc.tool, "args": dict(tc.arguments)})
            return [{"type": "text", "text": "stub"}]

        with patch("bamboo.tools.bamboo_answer.execute_plan", side_effect=_fake_execute):
            asyncio.run(_run_fast_path_intercepts(
                "What are the pilot and job failure rates at BNL?", []
            ))

        jobs_call = next(
            (c for c in captured_args if c["tool"] == "panda_jobs_query"), None
        )
        assert jobs_call is not None, "panda_jobs_query was not called"
        assert jobs_call["args"].get("queue") == "BNL", (
            f"Expected queue=BNL, got: {jobs_call['args']}"
        )

        harvester_call = next(
            (c for c in captured_args if c["tool"] == "panda_harvester_workers"), None
        )
        assert harvester_call is not None, "panda_harvester_workers was not called"
        assert harvester_call["args"].get("site") == "BNL"


# ---------------------------------------------------------------------------
# _pick_synthesis_prompt: site-health case
# ---------------------------------------------------------------------------


class TestPickSynthesisPromptSiteHealth:
    """Verify _pick_synthesis_prompt selects the site-health prompt correctly."""

    def test_both_tools_selects_site_health(self) -> None:
        """Both harvester and jobs query in tool_names → site-health prompt."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_SITE_HEALTH,
        )
        result = _pick_synthesis_prompt(
            ["panda_harvester_workers", "panda_jobs_query"]
        )
        assert result is _SYSTEM_SITE_HEALTH

    def test_harvester_only_selects_harvester_prompt(self) -> None:
        """Harvester alone does not select site-health prompt."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_HARVESTER_WORKERS,
            _SYSTEM_SITE_HEALTH,
        )
        result = _pick_synthesis_prompt(["panda_harvester_workers"])
        assert result is _SYSTEM_HARVESTER_WORKERS
        assert result is not _SYSTEM_SITE_HEALTH

    def test_jobs_only_selects_jobs_prompt(self) -> None:
        """Jobs query alone does not select site-health prompt."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_JOBS_QUERY,
            _SYSTEM_SITE_HEALTH,
        )
        result = _pick_synthesis_prompt(["panda_jobs_query"])
        assert result is _SYSTEM_JOBS_QUERY
        assert result is not _SYSTEM_SITE_HEALTH

    def test_site_health_beats_individual_tools(self) -> None:
        """Site-health prompt takes priority when both tools present."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_SITE_HEALTH,
        )
        # Reverse order — should still pick site-health
        result = _pick_synthesis_prompt(
            ["panda_jobs_query", "panda_harvester_workers"]
        )
        assert result is _SYSTEM_SITE_HEALTH
