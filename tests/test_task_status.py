"""Tests for the panda_task_status tool (via the askpanda_atlas shim).

All tests go through ``ts_mod.panda_task_status_tool`` — the same object the
rest of the system uses — and mock ``askpanda_atlas._cache.cached_fetch_jsonish``
at the HTTP boundary, matching the pattern in ``test_log_analysis.py``.

All mocks return the 4-tuple ``(status_code, content_type, body_text,
parsed_json_or_none)`` that matches the ``cached_fetch_jsonish`` signature.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from askpanda_atlas import task_status as ts_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unpack(result: list[Any]) -> dict[str, Any]:
    """Deserialise the JSON-wrapped MCPContent returned by the tool.

    Args:
        result: Return value of ``panda_task_status_tool.call()``.

    Returns:
        Deserialised dict with ``"evidence"`` key (and optionally ``"text"``).
    """
    return json.loads(result[0]["text"])


def _ok_response(raw: dict[str, Any]) -> tuple[int, str, str, dict[str, Any]]:
    """Wrap a raw dict in the 4-tuple that ``fetch_jsonish`` returns on success.

    Args:
        raw: Parsed JSON dict to return as the response body.

    Returns:
        ``(200, "application/json", json_text, raw)`` tuple.
    """
    return (200, "application/json", json.dumps(raw), raw)


def _make_raw_task(
    task_id: int = 1234,
    status: str = "finished",
    n_failed: int = 0,
    n_finished: int = 5,
) -> dict[str, Any]:
    """Return a minimal BigPanDA ``/jobs/`` response dict for testing.

    Args:
        task_id: JEDI task identifier embedded in each job record.
        status: Dominant job status (used for selectionsummary).
        n_failed: Number of failed jobs to generate.
        n_finished: Number of finished jobs to generate.

    Returns:
        Minimal raw task dict matching the ``/jobs/?jeditaskid=`` endpoint shape.
    """
    jobs: list[dict[str, Any]] = []
    for i in range(n_failed):
        jobs.append({
            "pandaid": 1000 + i,
            "jobstatus": "failed",
            "computingsite": "AGLT2",
            "piloterrorcode": 1008,
            "piloterrordiag": "stage-in timeout",
            "attemptnr": 1,
            "jeditaskid": task_id,
            "reqid": 99,
            "processingtype": "managed",
        })
    for i in range(n_finished):
        jobs.append({
            "pandaid": 2000 + i,
            "jobstatus": "finished",
            "computingsite": "BNL",
            "piloterrorcode": 0,
            "piloterrordiag": None,
            "attemptnr": 1,
            "jeditaskid": task_id,
            "reqid": 99,
            "processingtype": "managed",
        })
    return {
        "jobs": jobs,
        "selectionsummary": [
            {
                "field": "taskname",
                "list": [{"value": f"mc21_task_{task_id}", "count": 1}],
            },
            {
                "field": "jobstatus",
                "list": [{"value": status, "count": n_failed + n_finished}],
            },
        ],
        "errsByCount": {"1008": n_failed} if n_failed else {},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_task_status_success_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful fetch returns evidence with task_id and job counts."""
    raw = _make_raw_task(task_id=1234, n_failed=0, n_finished=5)
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, timeout=30: _ok_response(raw),
    )
    monkeypatch.setenv("PANDA_BASE_URL", "https://bigpanda.cern.ch")

    res = asyncio.run(ts_mod.panda_task_status_tool.call({"task_id": 1234, "query": "status?"}))

    unpacked = _unpack(res)
    assert "evidence" in unpacked
    ev = unpacked["evidence"]
    assert ev["task_id"] == 1234
    assert ev["total_jobs"] == 5
    assert ev["jobs_by_status"].get("finished") == 5


def test_task_status_with_failed_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed jobs appear in jobs_by_status and failed_jobs_sample."""
    raw = _make_raw_task(task_id=5678, n_failed=3, n_finished=2)
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, timeout=30: _ok_response(raw),
    )

    res = asyncio.run(ts_mod.panda_task_status_tool.call({"task_id": 5678, "query": "why did jobs fail?"}))

    ev = _unpack(res)["evidence"]
    assert ev["jobs_by_status"]["failed"] == 3
    assert ev["jobs_by_status"]["finished"] == 2
    assert len(ev["failed_jobs_sample"]) == 3
    assert ev["jobs_by_piloterrorcode"].get("1008") == 3


def test_task_status_non_json_response_returns_error_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-JSON (HTML) response from BigPanDA returns error evidence."""
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, timeout=30: (200, "text/html", "<html>Maintenance</html>", None),
    )

    res = asyncio.run(ts_mod.panda_task_status_tool.call({"task_id": 9999, "query": "status?"}))

    payload = _unpack(res)
    ev = payload.get("evidence", {})
    assert "error" in ev or "text" in payload


def test_task_status_http_error_returns_error_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP failure (RuntimeError) returns error evidence, never raises."""
    def _fail(url: str, timeout: int = 30) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _fail)

    res = asyncio.run(ts_mod.panda_task_status_tool.call({"task_id": 9999, "query": "status?"}))

    payload = _unpack(res)
    ev = payload.get("evidence", {})
    assert "error" in ev or "text" in payload


def test_task_status_missing_task_id() -> None:
    """Missing task_id returns an error evidence dict without hitting HTTP."""
    res = asyncio.run(ts_mod.panda_task_status_tool.call({}))

    ev = _unpack(res)["evidence"]
    assert "error" in ev
    assert "task_id" in ev["error"]


def test_task_status_bad_task_id_type() -> None:
    """Non-integer task_id returns an error evidence dict."""
    res = asyncio.run(ts_mod.panda_task_status_tool.call({"task_id": "not-an-int"}))

    ev = _unpack(res)["evidence"]
    assert "error" in ev
    assert "integer" in ev["error"]


def test_task_status_url_uses_jobs_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /jobs/ endpoint is called with the correct jeditaskid parameter."""
    raw = _make_raw_task(task_id=42)
    captured: list[str] = []

    def _capture(url: str, timeout: int = 30) -> tuple[int, str, str, dict[str, Any]]:
        captured.append(url)
        return _ok_response(raw)

    monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _capture)
    monkeypatch.setenv("PANDA_BASE_URL", "https://bigpanda.cern.ch")

    asyncio.run(ts_mod.panda_task_status_tool.call({"task_id": 42}))

    assert len(captured) == 1
    assert "/jobs/" in captured[0]
    assert "jeditaskid=42" in captured[0]
    assert "json" in captured[0]


def test_task_status_result_is_json_serialisable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full tool result can be round-tripped through json.dumps."""
    raw = _make_raw_task(task_id=99, n_failed=1, n_finished=4)
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, timeout=30: _ok_response(raw),
    )

    res = asyncio.run(ts_mod.panda_task_status_tool.call({"task_id": 99}))

    payload = json.loads(res[0]["text"])
    assert isinstance(payload, dict)
    assert "evidence" in payload
