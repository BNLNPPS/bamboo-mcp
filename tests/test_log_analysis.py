"""Core-level tests for panda_log_analysis via the bamboo.tools.log_analysis shim.

Exercises the tool as core sees it — through the ``bamboo.tools.log_analysis``
re-export rather than by importing the plugin package directly — so a broken or
incomplete shim is caught here rather than at runtime.

NOTE: this file substantially overlaps
``packages/askpanda_atlas/tests/test_log_analysis.py``: 20 of its 21 tests share
names with that file and 18 of those bodies are byte-identical.  The canonical
tests for the extraction and classification logic live in the package file; only
the shim-reachability aspect is unique here.  Changes to shared behaviour must be
applied to both files until the duplication is resolved.

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
    """extract_log_excerpt returns char-tail for payload.stdout (code 1305).

    The log is sized from the live ``_MAX_EXCERPT_CHARS`` value rather than a
    hardcoded line count, so raising the budget cannot silently turn this into
    a test that asserts nothing (a fixture that fits entirely in the budget is
    never truncated, so the "beginning absent" assertion would pass vacuously
    only because the whole log is present).
    """
    from askpanda_atlas.log_analysis_impl import _MAX_EXCERPT_CHARS

    # ~7 chars per line; use double the budget so truncation is guaranteed.
    n_lines = (_MAX_EXCERPT_CHARS * 2) // 7
    long_log = "\n".join(f"line{i}" for i in range(n_lines))
    assert len(long_log) > _MAX_EXCERPT_CHARS, "Fixture must exceed the excerpt budget."

    excerpt = extract_log_excerpt(
        long_log, "payload.stdout",
        pilot_error_code=1305,
        pilot_error_diag="",
    )
    # Tail should contain the end of the log but not the very beginning.
    assert f"line{n_lines - 1}" in excerpt
    assert not excerpt.startswith("line0")


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
    """Pilot error 1305 (payload failure) fetches payload.stdout and payload.stderr.

    setup.stdout is explicitly zero-length in the file index so the fail-open
    policy in _file_is_nonempty does not cause it to be downloaded.
    """
    fetched_filenames: list[str] = []

    def _capture_log(job_id: int, filename: str, base_url: str, timeout: int) -> str:
        fetched_filenames.append(filename)
        return "Traceback (most recent call last):\n  AthenaMP crash\n" * 50

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr("askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log)
    # Provide a concrete file index so _file_is_nonempty does not fall back to
    # fail-open (None).  setup.stdout is zero-length -> skipped; payload logs
    # are non-empty -> downloaded.
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_index",
        lambda job_id, base_url, timeout: {
            "setup.stdout": 0,
            "payload.stdout": 1000,
            "payload.stderr": 500,
        },
    )

    asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    assert fetched_filenames == ["payload.stdout", "payload.stderr"]


def test_get_definition() -> None:
    """get_definition returns required MCP fields."""
    d = panda_log_analysis_tool.get_definition()
    assert d["name"] == "panda_log_analysis"
    assert "job_id" in d["inputSchema"]["properties"]
    assert d["inputSchema"]["required"] == ["job_id"]
    assert d["inputSchema"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Presentation keys must not reach the synthesis LLM
#
# code_analysis_offer_md and links_md hold Markdown rendered for the *user*;
# bamboo_executor appends them programmatically after synthesis.  They used to
# be visible to the LLM inside the nested evidence dict, and the LLM copied the
# offer verbatim into its answer — so the rendered reply carried the offer
# twice, once from the model and once appended.  A prompt instruction not to
# reproduce it loses to a ready-made string sitting in the input, which is why
# they are now stripped from the LLM's view.
#
# They must still be present in _last_evidence_store, because
# _log_analysis_offer_md and _log_analysis_links_md read them back from there.
# ---------------------------------------------------------------------------

def test_strip_presentation_keys_removes_nested_markdown() -> None:
    """Presentation keys are removed from the nested evidence dict."""
    from bamboo.tools.bamboo_executor import _strip_presentation_keys

    unpacked = {
        "evidence": {
            "job_id": 7261310898,
            "exception_type": "TimeoutError",
            "links_md": "\n\nLinks:\n- [BigPanDA Monitor](https://example)",
            "code_analysis_offer_md": "\n\nAsk me to show the pilot source.",
        },
        "text": "Job failed.",
    }
    cleaned = _strip_presentation_keys(unpacked)

    assert "links_md" not in cleaned["evidence"]
    assert "code_analysis_offer_md" not in cleaned["evidence"]
    # Real evidence survives untouched.
    assert cleaned["evidence"]["exception_type"] == "TimeoutError"
    assert cleaned["text"] == "Job failed."


def test_strip_presentation_keys_does_not_mutate_input() -> None:
    """The caller's dict is left intact so the evidence store keeps the keys."""
    from bamboo.tools.bamboo_executor import _strip_presentation_keys

    unpacked = {
        "evidence": {"job_id": 1, "code_analysis_offer_md": "offer"},
    }
    _strip_presentation_keys(unpacked)

    assert unpacked["evidence"]["code_analysis_offer_md"] == "offer", (
        "Stripping for the LLM must not remove the key from the original dict; "
        "_log_analysis_offer_md reads it back from the evidence store."
    )


def test_strip_presentation_keys_tolerates_missing_evidence() -> None:
    """A result with no nested evidence dict is handled without raising."""
    from bamboo.tools.bamboo_executor import _strip_presentation_keys

    assert _strip_presentation_keys({"text": "hi"}) == {"text": "hi"}
    assert _strip_presentation_keys({"evidence": None}) == {"evidence": None}
