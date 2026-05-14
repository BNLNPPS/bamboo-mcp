"""Tests for panda_task_schema and task_status_impl.

Covers the schema module (pure functions, no I/O) and the tool's
``call()`` path via the public ``panda_task_status_tool`` singleton
(HTTP mocked via monkeypatch / unittest.mock).

All mocks of ``fetch_jsonish`` return the 4-tuple
``(status_code, content_type, body_text, parsed_json_or_none)``
that matches the ``cached_fetch_jsonish`` signature.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bamboo.tools.task_status import panda_task_status_tool


# ---------------------------------------------------------------------------
# fetch_jsonish response helpers
# ---------------------------------------------------------------------------


def _ok_response(raw: dict[str, Any]) -> tuple[int, str, str, dict[str, Any]]:
    """Return a successful 4-tuple from fetch_jsonish for a JSON response.

    Args:
        raw: The parsed JSON dict to include as the fourth element.

    Returns:
        ``(200, "application/json", json_text, raw)``
    """
    return (200, "application/json", json.dumps(raw), raw)


def _err_response(
    status: int = 404,
    body: str = "",
) -> tuple[int, str, str, None]:
    """Return an error 4-tuple from fetch_jsonish (no parsed JSON).

    Args:
        status: HTTP status code.
        body: Response body text.

    Returns:
        ``(status, "text/html", body, None)``
    """
    return (status, "text/html", body, None)


def _html_response(body: str = "<html>Maintenance</html>") -> tuple[int, str, str, None]:
    """Return a 200-OK 4-tuple with non-JSON HTML body.

    Args:
        body: HTML body text.

    Returns:
        ``(200, "text/html", body, None)``
    """
    return (200, "text/html", body, None)


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _unpack(result: list[Any]) -> dict[str, Any]:
    """Deserialise the JSON-wrapped MCPContent returned by the tool.

    Args:
        result: Return value of ``panda_task_status_tool.call()``.

    Returns:
        Deserialised dict with ``"evidence"`` key (and optionally ``"text"``).
    """
    return json.loads(result[0]["text"])


def _make_raw_job(
    pandaid: int = 1001,
    jobstatus: str = "failed",
    computingsite: str = "AGLT2",
    piloterrorcode: int = 1008,
    piloterrordiag: str = "Pilot could not stage in input files",
    attemptnr: int = 1,
    jeditaskid: int = 48432100,
) -> dict[str, Any]:
    """Return a minimal raw job dict for testing.

    Args:
        pandaid: PanDA job identifier.
        jobstatus: Job status string.
        computingsite: Computing site name.
        piloterrorcode: Pilot error code.
        piloterrordiag: Pilot error diagnostics string.
        attemptnr: Attempt number.
        jeditaskid: JEDI task identifier.

    Returns:
        Minimal raw job dictionary.
    """
    return {
        "pandaid": pandaid,
        "jobstatus": jobstatus,
        "computingsite": computingsite,
        "piloterrorcode": piloterrorcode,
        "piloterrordiag": piloterrordiag,
        "attemptnr": attemptnr,
        "jeditaskid": jeditaskid,
        "reqid": 999,
        "processingtype": "managed",
        "exeerrorcode": 0,
        "exeerrordiag": None,
        "jobname": f"job_{pandaid}",
        "transformation": "Reco_tf.py",
        "cloud": "US",
        "starttime": "2024-01-01T00:00:00",
        "endtime": "2024-01-01T01:00:00",
        "durationsec": 3600,
    }


def _make_raw_task(
    n_failed: int = 3,
    n_finished: int = 2,
    task_id: int = 48432100,
) -> dict[str, Any]:
    """Return a minimal raw task dict for testing.

    Args:
        n_failed: Number of failed jobs to generate.
        n_finished: Number of finished jobs to generate.
        task_id: JEDI task identifier.

    Returns:
        Minimal raw task dictionary matching the BigPanDA jobs endpoint shape.
    """
    jobs: list[dict[str, Any]] = []
    for i in range(n_failed):
        jobs.append(_make_raw_job(
            pandaid=1000 + i,
            jobstatus="failed",
            computingsite="AGLT2",
            piloterrorcode=1008,
            jeditaskid=task_id,
        ))
    for i in range(n_finished):
        jobs.append(_make_raw_job(
            pandaid=2000 + i,
            jobstatus="finished",
            computingsite="BNL",
            piloterrorcode=0,
            jeditaskid=task_id,
        ))
    return {
        "jobs": jobs,
        "selectionsummary": [
            {
                "field": "taskname",
                "list": [{"value": "mc21_task_test", "count": 1}],
            },
            {
                "field": "computingsite",
                "list": [
                    {"value": "AGLT2", "count": n_failed},
                    {"value": "BNL", "count": n_finished},
                ],
            },
        ],
        "errsByCount": {"1008": n_failed},
    }


# ---------------------------------------------------------------------------
# panda_task_schema — PandaJob
# ---------------------------------------------------------------------------


class TestPandaJob:
    """Unit tests for :class:`panda_task_schema.PandaJob`."""

    def test_promoted_fields_parsed(self) -> None:
        """Promoted attributes are correctly typed."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        job = PandaJob(_make_raw_job(pandaid=42, jobstatus="finished"))
        assert job.pandaid == 42
        assert job.jobstatus == "finished"
        assert isinstance(job.jeditaskid, int)

    def test_pandaid_coerced_from_string(self) -> None:
        """pandaid as a string is coerced to int."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        raw = _make_raw_job()
        raw["pandaid"] = "7061545370"
        job = PandaJob(raw)
        assert job.pandaid == 7061545370

    def test_pandaid_none_on_garbage(self) -> None:
        """Unparseable pandaid yields None without raising."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        raw = _make_raw_job()
        raw["pandaid"] = "not-an-int"
        job = PandaJob(raw)
        assert job.pandaid is None

    def test_extra_captures_unknown_fields(self) -> None:
        """Unknown fields land in .extra."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        raw = _make_raw_job()
        raw["future_field_xyz"] = "hello"
        job = PandaJob(raw)
        assert "future_field_xyz" in job.extra

    def test_get_returns_raw_value(self) -> None:
        """get() retrieves any raw field by name."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        job = PandaJob(_make_raw_job())
        assert job.get("transformation") == "Reco_tf.py"
        assert job.get("nonexistent", "default") == "default"

    def test_to_dict_equals_input(self) -> None:
        """to_dict() returns a copy of the original raw dict."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        raw = _make_raw_job()
        job = PandaJob(raw)
        assert job.to_dict() == raw
        assert job.to_dict() is not raw

    def test_to_slim_dict_omits_none(self) -> None:
        """to_slim_dict() excludes None-valued fields."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        raw = _make_raw_job()
        raw["exeerrordiag"] = None
        job = PandaJob(raw)
        slim = job.to_slim_dict()
        assert "exeerrordiag" not in slim

    def test_to_slim_dict_includes_key_fields(self) -> None:
        """to_slim_dict() includes pandaid, jobstatus, and site."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        job = PandaJob(_make_raw_job(pandaid=999, jobstatus="failed"))
        slim = job.to_slim_dict()
        assert slim["pandaid"] == 999
        assert slim["jobstatus"] == "failed"
        assert slim["computingsite"] == "AGLT2"

    def test_schema_returns_dict(self) -> None:
        """schema() is a non-empty dict."""
        from askpanda_atlas.panda_task_schema import PandaJob  # type: ignore[import]

        s = PandaJob.schema()
        assert isinstance(s, dict)
        assert "pandaid" in s


# ---------------------------------------------------------------------------
# panda_task_schema — PandaTaskData
# ---------------------------------------------------------------------------


class TestPandaTaskData:
    """Unit tests for :class:`panda_task_schema.PandaTaskData`."""

    def test_jobs_parsed(self) -> None:
        """All jobs in the raw dict are parsed into PandaJob instances."""
        from askpanda_atlas.panda_task_schema import PandaJob, PandaTaskData  # type: ignore[import]

        raw = _make_raw_task(n_failed=3, n_finished=2)
        task = PandaTaskData(raw)
        assert len(task.jobs) == 5
        assert all(isinstance(j, PandaJob) for j in task.jobs)

    def test_empty_jobs_list(self) -> None:
        """A task with no jobs is handled gracefully."""
        from askpanda_atlas.panda_task_schema import PandaTaskData  # type: ignore[import]

        task = PandaTaskData({"jobs": [], "selectionsummary": [], "errsByCount": {}})
        assert task.jobs == []

    def test_get_job_found(self) -> None:
        """get_job() returns the correct PandaJob."""
        from askpanda_atlas.panda_task_schema import PandaTaskData  # type: ignore[import]

        task = PandaTaskData(_make_raw_task())
        job = task.get_job(1000)
        assert job is not None
        assert job.pandaid == 1000

    def test_get_job_missing(self) -> None:
        """get_job() returns None for an unknown pandaid."""
        from askpanda_atlas.panda_task_schema import PandaTaskData  # type: ignore[import]

        task = PandaTaskData(_make_raw_task())
        assert task.get_job(9999999) is None

    def test_extra_captures_unknown_top_level(self) -> None:
        """Unknown top-level keys are captured in .extra."""
        from askpanda_atlas.panda_task_schema import PandaTaskData  # type: ignore[import]

        raw = _make_raw_task()
        raw["future_top_level_key"] = [1, 2, 3]
        task = PandaTaskData(raw)
        assert "future_top_level_key" in task.extra

    def test_to_dict_is_copy(self) -> None:
        """to_dict() returns a copy, not the same object."""
        from askpanda_atlas.panda_task_schema import PandaTaskData  # type: ignore[import]

        raw = _make_raw_task()
        task = PandaTaskData(raw)
        assert task.to_dict() is not raw

    def test_schema_has_three_sections(self) -> None:
        """schema() contains top_level, selectionsummary_item, job_fields."""
        from askpanda_atlas.panda_task_schema import PandaTaskData  # type: ignore[import]

        s = PandaTaskData.schema()
        assert set(s.keys()) == {"top_level", "selectionsummary_item", "job_fields"}


# ---------------------------------------------------------------------------
# panda_task_schema — extraction helpers
# ---------------------------------------------------------------------------


class TestGetPandaidList:
    """Unit tests for :func:`panda_task_schema.get_pandaid_list`."""

    def test_returns_all_ids(self) -> None:
        """All pandaids are returned in order."""
        from askpanda_atlas.panda_task_schema import PandaTaskData, get_pandaid_list  # type: ignore[import]

        task = PandaTaskData(_make_raw_task(n_failed=3, n_finished=2))
        ids = get_pandaid_list(task)
        assert len(ids) == 5
        assert ids[0] == 1000
        assert ids[3] == 2000

    def test_skips_unparseable_ids(self) -> None:
        """Jobs with non-integer pandaids are silently omitted."""
        from askpanda_atlas.panda_task_schema import PandaTaskData, get_pandaid_list  # type: ignore[import]

        raw = _make_raw_task(n_failed=1, n_finished=0)
        raw["jobs"][0]["pandaid"] = "bad"
        task = PandaTaskData(raw)
        assert get_pandaid_list(task) == []

    def test_empty_task(self) -> None:
        """Empty job list yields empty ID list."""
        from askpanda_atlas.panda_task_schema import PandaTaskData, get_pandaid_list  # type: ignore[import]

        task = PandaTaskData({"jobs": [], "selectionsummary": [], "errsByCount": {}})
        assert get_pandaid_list(task) == []


class TestGetPandaidListByStatus:
    """Unit tests for :func:`panda_task_schema.get_pandaid_list_by_status`."""

    def test_filters_by_status(self) -> None:
        """Only IDs matching the requested status are returned."""
        from askpanda_atlas.panda_task_schema import (  # type: ignore[import]
            PandaTaskData,
            get_pandaid_list_by_status,
        )

        task = PandaTaskData(_make_raw_task(n_failed=3, n_finished=2))
        failed_ids = get_pandaid_list_by_status(task, "failed")
        assert len(failed_ids) == 3
        assert all(i < 2000 for i in failed_ids)

    def test_no_match_returns_empty(self) -> None:
        """Returns empty list when no jobs match the status."""
        from askpanda_atlas.panda_task_schema import (  # type: ignore[import]
            PandaTaskData,
            get_pandaid_list_by_status,
        )

        task = PandaTaskData(_make_raw_task(n_failed=2, n_finished=0))
        assert get_pandaid_list_by_status(task, "finished") == []


class TestSummariseSelectionsummary:
    """Unit tests for :func:`panda_task_schema.summarise_selectionsummary`."""

    def test_single_value_unwrapped(self) -> None:
        """A field with one unique value is stored directly, not as a list."""
        from askpanda_atlas.panda_task_schema import summarise_selectionsummary  # type: ignore[import]

        ss = [{"field": "taskname", "list": [{"value": "mc21_test", "count": 5}]}]
        assert summarise_selectionsummary(ss)["taskname"] == "mc21_test"

    def test_multi_value_preserved_as_list(self) -> None:
        """A field with multiple values is stored as a list of values."""
        from askpanda_atlas.panda_task_schema import summarise_selectionsummary  # type: ignore[import]

        ss = [{"field": "computingsite", "list": [
            {"value": "AGLT2", "count": 10},
            {"value": "BNL", "count": 5},
        ]}]
        assert summarise_selectionsummary(ss)["computingsite"] == ["AGLT2", "BNL"]

    def test_empty_list(self) -> None:
        """Empty selectionsummary yields empty dict."""
        from askpanda_atlas.panda_task_schema import summarise_selectionsummary  # type: ignore[import]

        assert summarise_selectionsummary([]) == {}

    def test_missing_field_key_skipped(self) -> None:
        """Entries without a 'field' key are skipped."""
        from askpanda_atlas.panda_task_schema import summarise_selectionsummary  # type: ignore[import]

        assert summarise_selectionsummary([{"list": [{"value": "x", "count": 1}]}]) == {}


class TestBuildEvidence:
    """Unit tests for :func:`panda_task_schema.build_evidence`."""

    def _task(self, **kwargs: Any) -> Any:
        from askpanda_atlas.panda_task_schema import PandaTaskData  # type: ignore[import]

        return PandaTaskData(_make_raw_task(**kwargs))

    def test_structure(self) -> None:
        """Evidence dict contains all expected top-level keys."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        ev = build_evidence(self._task())
        for key in (
            "task_summary", "total_jobs", "jobs_by_status", "jobs_by_site",
            "jobs_by_piloterrorcode", "errs_by_count", "failed_jobs_sample",
            "finished_jobs_sample", "pandaid_list_note",
        ):
            assert key in ev, f"Missing key: {key}"

    def test_total_jobs_correct(self) -> None:
        """total_jobs reflects the actual job count."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        assert build_evidence(self._task(n_failed=7, n_finished=3))["total_jobs"] == 10

    def test_jobs_by_status_counts(self) -> None:
        """jobs_by_status counts match the task composition."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        ev = build_evidence(self._task(n_failed=4, n_finished=1))
        assert ev["jobs_by_status"]["failed"] == 4
        assert ev["jobs_by_status"]["finished"] == 1

    def test_failed_sample_capped(self) -> None:
        """failed_jobs_sample is capped at _MAX_FAILED_JOBS (20)."""
        from askpanda_atlas.panda_task_schema import build_evidence, _MAX_FAILED_JOBS  # type: ignore[import]

        ev = build_evidence(self._task(n_failed=50, n_finished=0))
        assert len(ev["failed_jobs_sample"]) == _MAX_FAILED_JOBS

    def test_finished_sample_capped(self) -> None:
        """finished_jobs_sample is capped at _MAX_FINISHED_JOBS (5)."""
        from askpanda_atlas.panda_task_schema import build_evidence, _MAX_FINISHED_JOBS  # type: ignore[import]

        ev = build_evidence(self._task(n_failed=0, n_finished=30))
        assert len(ev["finished_jobs_sample"]) == _MAX_FINISHED_JOBS

    def test_pandaid_list_inlined_for_small_task(self) -> None:
        """pandaid_list is inlined when total_jobs <= _MAX_PANDAID_LIST_INLINE."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        ev = build_evidence(self._task(n_failed=2, n_finished=2))
        assert ev["pandaid_list"] is not None
        assert isinstance(ev["pandaid_list"], list)

    def test_pandaid_list_not_inlined_for_large_task(self) -> None:
        """pandaid_list is None and a note is set when total_jobs > 500."""
        from askpanda_atlas.panda_task_schema import (  # type: ignore[import]
            PandaTaskData, build_evidence, _MAX_PANDAID_LIST_INLINE,
        )

        n = _MAX_PANDAID_LIST_INLINE + 1
        raw = {
            "jobs": [_make_raw_job(pandaid=i, jobstatus="finished") for i in range(n)],
            "selectionsummary": [],
            "errsByCount": {},
        }
        ev = build_evidence(PandaTaskData(raw))
        assert ev["pandaid_list"] is None
        assert "not inlined" in ev["pandaid_list_note"]

    def test_errs_by_count_passed_through(self) -> None:
        """errs_by_count from the API is passed through unmodified."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        assert build_evidence(self._task(n_failed=3))["errs_by_count"] == {"1008": 3}

    def test_jobs_by_piloterrorcode_excludes_zero(self) -> None:
        """Pilot error code 0 (no error) is not counted."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        ev = build_evidence(self._task(n_failed=3, n_finished=2))
        assert "0" not in ev["jobs_by_piloterrorcode"]

    def test_task_summary_single_value(self) -> None:
        """task_summary unwraps single-value selectionsummary entries."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        assert build_evidence(self._task())["task_summary"]["taskname"] == "mc21_task_test"

    def test_evidence_is_json_serialisable(self) -> None:
        """build_evidence output can be round-tripped through json.dumps."""
        from askpanda_atlas.panda_task_schema import build_evidence  # type: ignore[import]

        ev = build_evidence(self._task(n_failed=5, n_finished=3))
        assert json.loads(json.dumps(ev))["total_jobs"] == 8


# ---------------------------------------------------------------------------
# fetch_and_analyse — pure HTTP layer (HTTP mocked)
# ---------------------------------------------------------------------------


class TestFetchAndAnalyse:
    """Unit tests for :func:`task_status_impl.fetch_and_analyse`."""

    def test_returns_evidence_dict(self) -> None:
        """fetch_and_analyse returns a dict with expected evidence keys."""
        from askpanda_atlas.task_status_impl import fetch_and_analyse  # type: ignore[import]

        raw = _make_raw_task(n_failed=2, n_finished=1, task_id=48432100)
        with patch("askpanda_atlas._cache.cached_fetch_jsonish",
                   MagicMock(return_value=_ok_response(raw))):
            result = fetch_and_analyse("https://bigpanda.cern.ch", 48432100)

        assert result["task_id"] == 48432100
        assert result["total_jobs"] == 3
        assert "endpoint" in result

    def test_endpoint_url_correct(self) -> None:
        """The URL passed to cached_fetch_jsonish uses the /jobs/ path."""
        from askpanda_atlas.task_status_impl import fetch_and_analyse  # type: ignore[import]

        raw = _make_raw_task()
        captured: list[str] = []

        def _capture(url: str, timeout: int = 30) -> tuple[int, str, str, dict[str, Any]]:
            captured.append(url)
            return _ok_response(raw)

        with patch("askpanda_atlas._cache.cached_fetch_jsonish", _capture):
            fetch_and_analyse("https://bigpanda.cern.ch", 12345)

        assert len(captured) == 1
        assert "/jobs/" in captured[0]
        assert "jeditaskid=12345" in captured[0]

    def test_raises_on_non_json_response(self) -> None:
        """RuntimeError is raised when the API returns non-JSON (payload=None)."""
        from askpanda_atlas.task_status_impl import fetch_and_analyse  # type: ignore[import]

        with patch("askpanda_atlas._cache.cached_fetch_jsonish",
                   MagicMock(return_value=_html_response())):
            with pytest.raises(RuntimeError, match="Non-JSON response"):
                fetch_and_analyse("https://bigpanda.cern.ch", 99)

    def test_raises_on_http_error_status(self) -> None:
        """RuntimeError is raised when the API returns a non-2xx status."""
        from askpanda_atlas.task_status_impl import fetch_and_analyse  # type: ignore[import]

        with patch("askpanda_atlas._cache.cached_fetch_jsonish",
                   MagicMock(return_value=_err_response(404))):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                fetch_and_analyse("https://bigpanda.cern.ch", 99)

    def test_base_url_trailing_slash_stripped(self) -> None:
        """Trailing slash on base_url does not produce double slashes."""
        from askpanda_atlas.task_status_impl import fetch_and_analyse  # type: ignore[import]

        raw = _make_raw_task()
        captured: list[str] = []

        def _capture(url: str, timeout: int = 30) -> tuple[int, str, str, dict[str, Any]]:
            captured.append(url)
            return _ok_response(raw)

        with patch("askpanda_atlas._cache.cached_fetch_jsonish", _capture):
            fetch_and_analyse("https://bigpanda.cern.ch/", 77)

        assert "//" not in captured[0].replace("https://", "")


# ---------------------------------------------------------------------------
# Tool call() — via panda_task_status_tool singleton
# ---------------------------------------------------------------------------


class TestToolCall:
    """Integration tests for ``panda_task_status_tool.call()``.

    Uses the same ``_unpack`` / monkeypatch pattern as ``test_task_status.py``
    and ``test_log_analysis.py``.  All ``fetch_jsonish`` mocks return 4-tuples.
    """

    def test_call_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful call returns evidence dict with task_id and total_jobs."""
        raw = _make_raw_task(n_failed=2, n_finished=1, task_id=48432100)
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, timeout=30: _ok_response(raw),
        )
        monkeypatch.setenv("PANDA_BASE_URL", "https://bigpanda.cern.ch")

        result = asyncio.run(panda_task_status_tool.call({"task_id": 48432100}))
        ev = _unpack(result)["evidence"]

        assert ev["task_id"] == 48432100
        assert ev["total_jobs"] == 3
        assert ev["jobs_by_status"]["failed"] == 2
        assert ev["jobs_by_status"]["finished"] == 1

    def test_call_missing_task_id(self) -> None:
        """Missing task_id returns an error evidence dict."""
        result = asyncio.run(panda_task_status_tool.call({}))
        ev = _unpack(result)["evidence"]
        assert "error" in ev
        assert "task_id" in ev["error"]

    def test_call_bad_task_id_type(self) -> None:
        """Non-integer task_id returns an error evidence dict."""
        result = asyncio.run(panda_task_status_tool.call({"task_id": "not-an-int"}))
        ev = _unpack(result)["evidence"]
        assert "error" in ev
        assert "integer" in ev["error"]

    def test_call_http_error_returns_error_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP failure (RuntimeError from fetch_jsonish) returns error evidence."""
        def _fail(url: str, timeout: int = 30) -> None:
            raise RuntimeError("connection refused")

        monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _fail)
        result = asyncio.run(panda_task_status_tool.call({"task_id": 99}))
        payload = _unpack(result)
        assert "error" in payload.get("evidence", payload)

    def test_call_non_json_response_returns_error_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-JSON (HTML) response from BigPanDA returns error evidence."""
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, timeout=30: _html_response(),
        )
        result = asyncio.run(panda_task_status_tool.call({"task_id": 9999}))
        payload = _unpack(result)
        assert "error" in payload.get("evidence", payload) or "text" in payload

    def test_call_evidence_json_serialisable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full tool result can be round-tripped through json.dumps."""
        raw = _make_raw_task(n_failed=3, n_finished=2, task_id=12345)
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, timeout=30: _ok_response(raw),
        )
        result = asyncio.run(panda_task_status_tool.call({"task_id": 12345}))
        assert isinstance(json.loads(result[0]["text"]), dict)

    def test_call_accepts_query_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Optional 'query' argument is accepted without error."""
        raw = _make_raw_task(task_id=99)
        monkeypatch.setattr(
            "askpanda_atlas._cache.cached_fetch_jsonish",
            lambda url, timeout=30: _ok_response(raw),
        )
        result = asyncio.run(panda_task_status_tool.call({
            "task_id": 99,
            "query": "why are jobs failing?",
        }))
        assert _unpack(result)["evidence"]["task_id"] == 99
