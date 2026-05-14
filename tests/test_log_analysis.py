"""Tests for panda_log_analysis tool (askpanda_atlas plugin implementation).

All external HTTP calls are patched; no network access is required.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bamboo.tools.log_analysis import (
    panda_log_analysis_tool,
    classify_failure,
    extract_log_excerpt,
)
from askpanda_atlas.log_analysis_impl import (
    _extract_context_window,
    _extract_tail,
    _select_log_filename,
)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_SAMPLE_JOB_STAGEIN_TIMEOUT: dict = {
    "pandaid": 6799893074,
    "jobstatus": "failed",
    "jobsubstatus": "",
    "computingsite": "UKI-SCOTGRID-GLASGOW_CEPH",
    "cloud": "UK",
    "atlasrelease": "Atlas-25.2.66",
    "jeditaskid": 46249501,
    "attemptnr": 1,
    "maxattempt": 3,
    "transformation": "Athena",
    "piloterrorcode": 1151,
    "piloterrordiag": (
        "File transfer timed out during stage-in: "
        "data24_13p6TeV:data24_13p6TeV.00483532... timeout=6842 seconds"
    ),
    "exeerrorcode": 0,
    "exeerrordiag": "",
    "taskbuffererrorcode": 0,
    "taskbuffererrordiag": "",
    "ddmerrorcode": 0,
    "ddmerrordiag": "",
    "starttime": "2025-09-08 05:50:33",
    "endtime": "2025-09-08 10:32:20",
    "duration": "4:41:47",
    "commandtopilot": "",
}

_SAMPLE_JOB_REASSIGNED: dict = {
    **_SAMPLE_JOB_STAGEIN_TIMEOUT,
    "pandaid": 6837798305,
    "jobstatus": "closed",
    "jobsubstatus": "toreassign",
    "piloterrorcode": 0,
    "piloterrordiag": "",
    "taskbuffererrorcode": 100,
    "taskbuffererrordiag": "reassigned by JEDI",
    "commandtopilot": "tobekilled",
}

_SAMPLE_JOB_PAYLOAD: dict = {
    **_SAMPLE_JOB_STAGEIN_TIMEOUT,
    "pandaid": 1111,
    "piloterrorcode": 1305,
    "piloterrordiag": "Payload error: AthenaMP exited with code 1",
}

_SAMPLE_PAYLOAD: dict = {
    "job": _SAMPLE_JOB_STAGEIN_TIMEOUT,
    "files": [],
    "dsfiles": [],
}

_SAMPLE_PILOT_LOG = "\n".join([
    "2025-09-08 05:50:33 | INFO | startup",
    "2025-09-08 05:50:34 | INFO | stage-in starting",
    "2025-09-08 10:32:18 | INFO | handle_rucio_error | TimeoutException: Timeout reached, timeout=6842 seconds",
    "2025-09-08 10:32:18 | WARNING | failed to transfer_files: File transfer timed out during stage-in",
    "2025-09-08 10:32:19 | ERROR | pilot error set to 1151",
    "2025-09-08 10:32:20 | INFO | job ended",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unpack(result: list) -> dict:
    """Deserialise the JSON-wrapped MCPContent returned by the tool.

    Args:
        result: Return value of tool.call().

    Returns:
        Deserialised dict with ``evidence`` and ``text`` keys.
    """
    return json.loads(result[0]["text"])


def _make_metadata_response(job: dict) -> dict:
    """Build a metadata response dict as BigPanDA would return.

    Args:
        job: Job metadata dict.

    Returns:
        Full metadata response payload.
    """
    return {"job": job, "files": [], "dsfiles": []}


# ---------------------------------------------------------------------------
# Unit tests: pure functions
# ---------------------------------------------------------------------------

def test_classify_failure_stagein_timeout() -> None:
    """Stage-in timeout is correctly classified."""
    result = classify_failure(_SAMPLE_JOB_STAGEIN_TIMEOUT, _SAMPLE_PILOT_LOG)
    assert result == "stagein_timeout"


def test_classify_failure_reassigned() -> None:
    """JEDI reassignment is correctly classified from metadata."""
    result = classify_failure(_SAMPLE_JOB_REASSIGNED, "")
    assert result == "reassigned_by_jedi"


def test_classify_failure_unknown() -> None:
    """Unrecognised errors fall back to 'unknown'."""
    job = {k: "" for k in _SAMPLE_JOB_STAGEIN_TIMEOUT}
    result = classify_failure(job, "something completely unrecognised")
    assert result == "unknown"


def test_classify_failure_segfault_from_log() -> None:
    """Segfault classification is driven by log excerpt."""
    job = {**_SAMPLE_JOB_STAGEIN_TIMEOUT, "piloterrordiag": ""}
    result = classify_failure(job, "Segmentation fault in AthenaMP\n")
    assert result == "segfault"


def test_extract_context_window_finds_match() -> None:
    """Context window extraction returns lines up to the pattern match."""
    lines = ["line1\n", "line2\n", "ERROR: timeout\n", "line4\n"]
    log_text = "".join(lines)
    result = _extract_context_window(log_text, "timeout", n_lines=10)
    assert "ERROR: timeout" in result
    assert "line1" in result
    assert "line4" not in result


def test_extract_context_window_no_match_returns_empty() -> None:
    """Context window extraction returns empty string when pattern not found."""
    result = _extract_context_window("line1\nline2\n", "NOTPRESENT", n_lines=5)
    assert result == ""


def test_extract_tail_returns_last_n_lines() -> None:
    """Tail extraction returns only the last N lines."""
    log = "\n".join(f"line{i}" for i in range(20))
    result = _extract_tail(log, n_lines=5)
    assert "line19" in result
    assert "line15" in result
    assert "line14" not in result


def test_select_log_filename_payload_error() -> None:
    """Pilot error 1305 selects payload.stdout."""
    job = {**_SAMPLE_JOB_STAGEIN_TIMEOUT, "piloterrorcode": 1305}
    assert _select_log_filename(job) == "payload.stdout"


def test_select_log_filename_pilot_error() -> None:
    """All other pilot errors select pilotlog.txt."""
    assert _select_log_filename(_SAMPLE_JOB_STAGEIN_TIMEOUT) == "pilotlog.txt"


def test_extract_log_excerpt_uses_pattern_for_pilotlog() -> None:
    """extract_log_excerpt uses code-specific pattern for pilotlog.txt."""
    excerpt = extract_log_excerpt(
        _SAMPLE_PILOT_LOG, "pilotlog.txt",
        pilot_error_code=1151,
        pilot_error_diag="File transfer timed out",
    )
    assert "timed out" in excerpt.lower() or "timeout" in excerpt.lower()


def test_extract_log_excerpt_uses_tail_for_payload() -> None:
    """extract_log_excerpt returns tail for payload.stdout (code 1305)."""
    long_log = "\n".join(f"line{i}" for i in range(500))
    excerpt = extract_log_excerpt(
        long_log, "payload.stdout",
        pilot_error_code=1305,
        pilot_error_diag="",
    )
    # Should contain the end of the log, not the beginning
    assert "line499" in excerpt
    assert "line0" not in excerpt


# ---------------------------------------------------------------------------
# Integration tests: full tool.call() with HTTP mocked
# ---------------------------------------------------------------------------

def test_log_analysis_success_with_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful analysis: metadata fetched, log downloaded, failure classified."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_STAGEIN_TIMEOUT),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: _SAMPLE_PILOT_LOG,
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 6799893074}))
    res = _unpack(result)
    ev = res["evidence"]

    assert ev["job_id"] == 6799893074
    assert ev["failure_type"] == "stagein_timeout"
    assert ev["log_available"] is True
    assert ev["log_excerpt"] is not None
    assert ev["piloterrorcode"] == 1151
    assert "stagein_timeout" in res["text"]


def test_log_analysis_metadata_only_for_closed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """For non-failed jobs (closed/reassigned) no log is downloaded."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_REASSIGNED),
    )
    fetch_log_called = []

    def _no_log(*args, **kwargs):  # type: ignore[no-untyped-def]
        fetch_log_called.append(True)
        return None

    monkeypatch.setattr("askpanda_atlas.log_analysis_impl._fetch_log_text", _no_log)

    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 6837798305}))
    res = _unpack(result)

    assert not fetch_log_called, "Log should not be fetched for non-failed jobs"
    assert res["evidence"]["failure_type"] == "reassigned_by_jedi"


def test_log_analysis_log_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """When log download fails, analysis still succeeds using metadata only."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_STAGEIN_TIMEOUT),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: None,
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 6799893074}))
    res = _unpack(result)
    ev = res["evidence"]

    assert ev["log_available"] is False
    assert ev["log_excerpt"] is None
    # Failure type should still come from metadata
    assert ev["failure_type"] == "stagein_timeout"


def test_log_analysis_job_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When metadata fetch returns no job, not_found is set in evidence."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: {"job": None, "files": [], "dsfiles": []},
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 9999}))
    res = _unpack(result)
    assert res["evidence"].get("not_found") is True


def test_log_analysis_metadata_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When metadata HTTP request fails, an error is returned."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: None,
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 9999}))
    res = _unpack(result)
    assert "error" in res["evidence"]


def test_log_analysis_missing_job_id() -> None:
    """Missing job_id produces a validation error in evidence."""
    result = asyncio.run(panda_log_analysis_tool.call({}))
    assert _unpack(result)["evidence"]["error"] == "missing job_id"


def test_log_analysis_invalid_job_id() -> None:
    """Non-integer job_id produces a validation error in evidence."""
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": "not-a-number"}))
    assert "integer" in _unpack(result)["evidence"]["error"]


def test_log_analysis_invalid_arguments() -> None:
    """Non-dict arguments produce a validation error in evidence."""
    result = asyncio.run(panda_log_analysis_tool.call("bad"))  # type: ignore[arg-type]
    assert "dict" in _unpack(result)["evidence"]["error"]


def test_log_analysis_payload_error_uses_payload_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pilot error 1305 (payload failure) fetches payload.stdout, not pilotlog.txt."""
    fetched_filenames: list[str] = []

    def _capture_log(job_id: int, filename: str, base_url: str, timeout: int) -> str:
        fetched_filenames.append(filename)
        return "Traceback (most recent call last):\n  AthenaMP crash\n" * 50

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr("askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log)

    asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    assert fetched_filenames == ["payload.stdout"]


def test_get_definition() -> None:
    """get_definition returns required MCP fields."""
    d = panda_log_analysis_tool.get_definition()
    assert d["name"] == "panda_log_analysis"
    assert "job_id" in d["inputSchema"]["properties"]
    assert d["inputSchema"]["required"] == ["job_id"]
    assert d["inputSchema"]["additionalProperties"] is False
