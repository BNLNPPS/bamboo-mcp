"""Tests for panda_log_analysis — canonical suite (askpanda_atlas plugin).

This is the authoritative test suite for the ATLAS log analysis implementation:
excerpt extraction (traceback-first and the pattern/tail fallbacks), failure
classification, the setup.stdout / payload.stdout / payload.stderr fetch paths,
exception evidence keys, and the job 7261310898 regression fixture.

A smaller, largely duplicated suite exists at ``tests/test_log_analysis.py`` in
the repo root, which exercises the same tool through the
``bamboo.tools.log_analysis`` core shim.  New tests belong here; changes to
behaviour covered by both files must be applied to both.

All external HTTP calls are patched; no network access is required.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

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
    _strip_payload_noise,
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


def test_classify_failure_stageout_timeout_not_misclassified_as_stagein() -> None:
    """Stage-out timeout (code 1152) is classified as stageout_timeout, not stagein_timeout.

    Regression test for a misclassification where the piloterrordiag for code
    1152 begins with "File transfer timed out during stage-out", which also
    matches the stagein_timeout keyword "file transfer timed out".
    stageout_timeout must appear before stagein_timeout in _FAILURE_PATTERNS,
    and its keyword list must include the exact diag prefix so the more
    specific match wins.
    """
    job = {
        **_SAMPLE_JOB_STAGEIN_TIMEOUT,
        "piloterrorcode": 1152,
        "piloterrordiag": (
            "File transfer timed out during stage-out: "
            "hc_test:output.1.10f2da00_79600.pool.root to CERN-PROD_SCRATCHDISK, "
            "copy command timed out: TimeoutException: Timeout reached, timeout=410 seconds"
        ),
    }
    result = classify_failure(job, "")
    assert result == "stageout_timeout", (
        f"Expected 'stageout_timeout', got '{result}'. "
        "Code 1152 diag starts with 'File transfer timed out during stage-out' — "
        "stageout_timeout must be checked before stagein_timeout."
    )


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


def test_extract_log_excerpt_uses_char_tail_for_payload() -> None:
    """extract_log_excerpt uses a char-based tail for payload.stdout (code 1305).

    A char-based tail guarantees that ERROR lines near the end of a long
    verbose stdout are captured even when the per-line budget would cut them
    off.  The excerpt should contain the final lines regardless of how many
    verbose INFO lines precede them.
    """
    # Simulate verbose init lines followed by ERROR lines at the very end
    info_lines = [f"INFO    Initializing tool_{i} from /cvmfs/data_{i}.root" for i in range(500)]
    error_lines = [
        "NTupleMaker   ERROR   Failed to call setupTree",
        "EventLoop     ERROR   Failed to call processInputs",
        "abort EL_JOBID=0",
    ]
    long_log = "\n".join(info_lines + error_lines)
    excerpt = extract_log_excerpt(
        long_log, "payload.stdout",
        pilot_error_code=1305,
        pilot_error_diag="",
    )
    # ERROR lines at the end must be captured
    assert "NTupleMaker" in excerpt
    assert "abort EL_JOBID=0" in excerpt
    # Beginning of a very long log should not be present
    assert "tool_0" not in excerpt


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
    """Pilot error 1305 fetches payload.stdout then payload.stderr.

    Both files are attempted: stdout is the primary log, stderr is fetched
    additionally since some failures (tracebacks, segfaults) only appear there.
    The stderr content should be present in the log excerpt.

    setup.stdout is confirmed zero-length in the file index so the tool skips
    it and goes straight to the payload logs, which is the scenario this test
    exercises.
    """
    fetched_filenames: list[str] = []

    def _capture_log(job_id: int, filename: str, base_url: str, timeout: int) -> str:
        fetched_filenames.append(filename)
        if filename == "payload.stderr":
            return "Segmentation fault (core dumped)\n" * 5
        return "Traceback (most recent call last):\n  AthenaMP crash\n" * 50

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr("askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log)
    # setup.stdout confirmed zero-length in index → skipped by _file_is_nonempty;
    # payload files are non-empty so they are downloaded as expected.
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({
            "setup.stdout": 0,
            "payload.stdout": 50000,
            "payload.stderr": 200,
        }),
    )

    import json
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    assert fetched_filenames == ["payload.stdout", "payload.stderr"]
    evidence = json.loads(result[0]["text"])["evidence"]
    assert evidence.get("stderr_url") is not None
    assert "payload.stderr" in (evidence.get("log_excerpt") or "")


def test_strip_payload_noise_removes_ls_sections() -> None:
    """_strip_payload_noise removes PanDA pilot ls directory listings.

    The pilot appends ``=== ls in <dir> ===`` sections between the application
    error output and the result footer.  These must be stripped before the
    char-tail is taken so the character budget is not wasted on file listings.
    """
    noisy = (
        "NTupleMaker   ERROR   Failed to call setupTree\n"
        "abort EL_JOBID=0\n"
        "\n=== ls in run dir : ./ ===\n"
        "total 408\n"
        "drwx------ 4 user user 4096 Apr 17 13:41 somedir\n"
        "lrwxrwxrwx 1 user user   44 Apr 17 13:41 DAOD.root -> /srv/DAOD.root\n"
        "-rw------- 1 user user  760 Apr 17 13:42 tmp.stderr.uuid\n"
        "\n=== ls in work dir : /srv ===\n"
        "total 572\n"
        "-rw------- 1 user user 375451 Apr 17 13:42 payload.stdout\n"
        "\n==== Result ====\n"
        "ERROR: payload execution failed with 1\n"
    )
    cleaned = _strip_payload_noise(noisy)
    # Application errors and result footer must be preserved
    assert "NTupleMaker   ERROR" in cleaned
    assert "abort EL_JOBID=0" in cleaned
    assert "==== Result ====" in cleaned
    assert "ERROR: payload execution failed" in cleaned
    # ls boilerplate must be removed
    assert "=== ls in run dir" not in cleaned
    assert "=== ls in work dir" not in cleaned
    assert "lrwxrwxrwx" not in cleaned
    assert "drwx------" not in cleaned
    # Result should be significantly shorter
    assert len(cleaned) < len(noisy) * 0.6


def test_classify_failure_pilot_monitoring_error_getpwuid() -> None:
    """pilot_monitoring_error is returned for getpwuid UID-not-found errors.

    Regression test for job 7099503721: the pilot's CPU monitoring code
    raised a KeyError when getpwuid() could not resolve UID 6435 on the
    worker node.  This is a pilot infrastructure issue, not a user payload
    failure — it must NOT be classified as payload_error.
    """
    log_excerpt = (
        "2026-04-17 02:05:23,070 | WARNING  | Exception caught: "
        "'getpwuid(): uid not found: 6435'\n"
        "2026-04-17 02:05:23,077 | WARNING  | Traceback (most recent call last):\n"
        "  File \".../pilot/util/monitoring.py\", line 193, in set_cpu_consumption_time\n"
        "    cpuconsumptiontime = get_current_cpu_consumption_time(job.pid)\n"
        "  File \".../pilot/util/processes.py\", line 619, "
        "in get_current_cpu_consumption_time\n"
        "    ps_cache = get_ps_cache()\n"
        "  File \".../pilot/util/processes.py\", line 191, in get_ps_cache\n"
        "    _ps_cache = list_processes_and_threads()\n"
        "  File \".../pilot/util/psutils.py\", line 428, "
        "in list_processes_and_threads\n"
        "    current_user = getpass.getuser()\n"
        "KeyError: 'getpwuid(): uid not found: 6435'\n"
    )
    job = {
        **_SAMPLE_JOB_STAGEIN_TIMEOUT,
        "pandaid": 7099503721,
        "piloterrorcode": 1354,
        "piloterrordiag": "Exception caught: 'getpwuid(): uid not found: 6435'",
        "taskbuffererrorcode": 0,
        "taskbuffererrordiag": "",
        "exeerrorcode": 0,
        "exeerrordiag": "",
        "jobsubstatus": "",
        "commandtopilot": "",
    }
    result = classify_failure(job, log_excerpt)
    assert result == "pilot_monitoring_error", (
        f"Expected 'pilot_monitoring_error', got '{result}'. "
        "getpwuid UID errors are pilot infrastructure failures, not payload errors."
    )


def test_extract_log_excerpt_pilot_error_1354() -> None:
    """extract_log_excerpt captures the full traceback for pilot error code 1354.

    The context window must include both the WARNING anchor line and the
    traceback lines that follow it.  With standard preceding-context extraction
    the function would return at the WARNING line and miss the File/KeyError
    lines entirely.

    Since the traceback-first extractor was introduced this no longer depends on
    1354 appearing in _TRAILING_CONTEXT_CODES — the traceback is located by its
    own format.  A bounded trailing window of _TRACEBACK_TRAILING_LINES lines is
    deliberately included after the traceback (the pilot logs the resulting
    error code and state transition there), so the assertion below checks that
    the tail is *bounded*, not that it is absent.  The failure mode being
    guarded against is the excerpt degenerating into a tail extraction that
    contains no traceback at all.
    """
    from askpanda_atlas.log_analysis_impl import (
        _CONTEXT_LINES,
        _TRACEBACK_TRAILING_LINES,
    )

    preamble = "INFO | some startup line\n" * 50
    traceback_block = (
        "2026-04-17 02:05:23,070 | WARNING  | Exception caught: "
        "'getpwuid(): uid not found: 6435'\n"
        "2026-04-17 02:05:23,077 | WARNING  | Traceback (most recent call last):\n"
        "  File \".../psutils.py\", line 428, in list_processes_and_threads\n"
        "    current_user = getpass.getuser()\n"
        "KeyError: 'getpwuid(): uid not found: 6435'\n"
        "\n"  # blank line terminates the traceback block — mirrors real pilot logs
    )
    # Append unrelated tail lines that would dominate if the anchor missed
    tail_line = "INFO | some unrelated pilot cleanup line"
    tail = (tail_line + "\n") * _CONTEXT_LINES
    log_text = preamble + traceback_block + tail

    excerpt = extract_log_excerpt(
        log_text,
        "pilotlog.txt",
        pilot_error_code=1354,
        pilot_error_diag="Exception caught: 'getpwuid(): uid not found: 6435'",
    )
    assert "getpwuid" in excerpt, "Excerpt must contain the getpwuid error line."
    assert "list_processes_and_threads" in excerpt, (
        "Excerpt must contain the traceback File line that follows the WARNING."
    )
    assert "KeyError" in excerpt, "Excerpt must contain the final KeyError line."

    # The traceback must come before the cleanup tail, i.e. the excerpt is
    # anchored on the traceback rather than being a tail extraction.
    assert excerpt.index("KeyError") < excerpt.index(tail_line), (
        "The traceback must precede the trailing context; if it does not, the "
        "excerpt degenerated into tail extraction."
    )
    # Only the bounded trailing window may bleed through, never all 40 lines.
    assert excerpt.count(tail_line) <= _TRACEBACK_TRAILING_LINES, (
        f"At most {_TRACEBACK_TRAILING_LINES} trailing lines may be included; "
        f"got {excerpt.count(tail_line)}."
    )


def test_extract_context_window_with_trailing() -> None:
    """_extract_context_window_with_trailing includes lines after the match."""
    from askpanda_atlas.log_analysis_impl import _extract_context_window_with_trailing

    log = (
        "line 1\n"
        "line 2\n"
        "ANCHOR line\n"
        "line after 1\n"
        "line after 2\n"
        "\n"                       # blank line — should stop collection here
        "line after blank\n"
    )
    result = _extract_context_window_with_trailing(log, "ANCHOR", n_before=10, n_trailing=20)
    assert "ANCHOR line" in result
    assert "line after 1" in result
    assert "line after 2" in result
    # Blank line itself is included as the stop sentinel, content after it is not
    assert "line after blank" not in result


def test_extract_context_window_with_trailing_no_match() -> None:
    """Returns empty string when the pattern is not found."""
    from askpanda_atlas.log_analysis_impl import _extract_context_window_with_trailing

    result = _extract_context_window_with_trailing(
        "INFO | nothing here\n", "MISSING", n_before=10, n_trailing=10
    )
    assert result == ""


def test_extract_context_window_with_trailing_respects_n_trailing() -> None:
    """Stops after n_trailing lines even without a blank line."""
    from askpanda_atlas.log_analysis_impl import _extract_context_window_with_trailing

    log = "ANCHOR\n" + "".join(f"line {i}\n" for i in range(50))
    result = _extract_context_window_with_trailing(log, "ANCHOR", n_before=5, n_trailing=3)
    assert "line 0" in result
    assert "line 1" in result
    assert "line 2" in result
    assert "line 3" not in result


def test_get_definition() -> None:
    """get_definition returns required MCP fields."""
    d = panda_log_analysis_tool.get_definition()
    assert d["name"] == "panda_log_analysis"
    assert "job_id" in d["inputSchema"]["properties"]
    assert d["inputSchema"]["required"] == ["job_id"]
    assert d["inputSchema"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Unit tests: new helpers (_setup_log_has_error, _file_is_nonempty,
#             _fetch_file_index, classify_failure setup pattern)
# ---------------------------------------------------------------------------

def test_setup_log_has_error_matches_no_release() -> None:
    """_setup_log_has_error returns True for 'No matched release is found'."""
    from askpanda_atlas.log_analysis_impl import _setup_log_has_error

    log = (
        "sourcing /srv/my_release_setup.sh\n"
        "AtlasSetup(WARNING): Deprecated tag ignored\n"
        "!!!ERROR!!! No matched release is found\n"
        "22:50:44 2026/04/19\n"
    )
    assert _setup_log_has_error(log) is True


def test_setup_log_has_error_matches_error_bang() -> None:
    """_setup_log_has_error returns True for bare !!!ERROR!!! line."""
    from askpanda_atlas.log_analysis_impl import _setup_log_has_error

    assert _setup_log_has_error("some preamble\n!!!ERROR!!! something bad\n") is True


def test_setup_log_has_error_case_insensitive() -> None:
    """_setup_log_has_error is case-insensitive."""
    from askpanda_atlas.log_analysis_impl import _setup_log_has_error

    assert _setup_log_has_error("No Matched Release Is Found\n") is True


def test_setup_log_has_error_clean_log_returns_false() -> None:
    """_setup_log_has_error returns False when setup completed without error."""
    from askpanda_atlas.log_analysis_impl import _setup_log_has_error

    clean = (
        "Info: /cvmfs mounted\n"
        "sourcing /srv/my_release_setup.sh\n"
        "Athena release 23.0.32 set up successfully\n"
    )
    assert _setup_log_has_error(clean) is False


def test_file_is_nonempty_positive() -> None:
    """_file_is_nonempty returns True for a file with size > 0."""
    from askpanda_atlas.log_analysis_impl import _file_is_nonempty

    index = {"setup.stdout": 4096, "payload.stdout": 0}
    assert _file_is_nonempty(index, "setup.stdout") is True


def test_file_is_nonempty_zero_size() -> None:
    """_file_is_nonempty returns False for a confirmed zero-length file."""
    from askpanda_atlas.log_analysis_impl import _file_is_nonempty

    index = {"payload.stdout": 0, "payload.stderr": 0}
    assert _file_is_nonempty(index, "payload.stdout") is False
    assert _file_is_nonempty(index, "payload.stderr") is False


def test_file_is_nonempty_missing_from_index() -> None:
    """_file_is_nonempty returns True when the filename is absent from the index.

    A file absent from the listing may simply not have been flushed yet;
    we attempt the download and rely on the HTTP layer for a definitive answer.
    """
    from askpanda_atlas.log_analysis_impl import _file_is_nonempty

    assert _file_is_nonempty({"pilotlog.txt": 1024}, "setup.stdout") is True


def test_file_is_nonempty_none_index_fail_open() -> None:
    """_file_is_nonempty returns True when the index is None (fail-open)."""
    from askpanda_atlas.log_analysis_impl import _file_is_nonempty

    assert _file_is_nonempty(None, "payload.stdout") is True
    assert _file_is_nonempty(None, "setup.stdout") is True


def test_fetch_file_index_parses_list_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_file_index parses a JSON list of {name, size} dicts.

    ``cached_fetch_jsonish`` is patched inside the impl module so that the
    real parsing logic inside ``_fetch_file_index`` is exercised end-to-end.
    """
    from askpanda_atlas.log_analysis_impl import _fetch_file_index

    fake_listing = [
        {"name": "setup.stdout", "size": 2048},
        {"name": "payload.stdout", "size": 0},
        {"name": "payload.stderr", "size": 0},
        {"name": "pilotlog.txt", "size": 98304},
    ]

    # _fetch_file_index calls cached_fetch_jsonish via a deferred import inside
    # the function body.  We patch it on the cache module that gets imported.
    import askpanda_atlas._cache as cache_mod
    monkeypatch.setattr(
        cache_mod,
        "cached_fetch_jsonish",
        lambda url, timeout: (200, "application/json", "", fake_listing),
    )

    result = _fetch_file_index(1, "https://bigpanda.cern.ch", 30)
    assert result == {
        "setup.stdout": 2048,
        "payload.stdout": 0,
        "payload.stderr": 0,
        "pilotlog.txt": 98304,
    }


# ---------------------------------------------------------------------------
# Recursive listing: path keying, error policy, top-level view
# ---------------------------------------------------------------------------

#: Trimmed excerpt of the real recursive listing for PanDA job 7263525363,
#: a job killed as looping (pilot error 1150) whose tarball contains a core
#: dump.  Reproduces the two properties that broke basename keying: the same
#: basename appears under several ``dirname`` values, and paths live in
#: ``dirname`` rather than in ``name``.  The ``error`` field carries the
#: advisory warning BigPanDA emits for a large job log.
_JOB_7263525363_LISTING: dict[str, Any] = {
    "error": "slow_downloading:The size of job log tarball is quite big (119.36MB).",
    "files": [
        {"name": "payload.stdout", "size": 454904, "dirname": "",
         "modification": "2026-08-19 06:08:46"},
        {"name": "payload.stderr", "size": 0, "dirname": "",
         "modification": "2026-08-19 01:42:08"},
        {"name": "my_release_setup.sh", "size": 225, "dirname": "",
         "modification": "2026-08-19 01:42:51"},
        {"name": "core.18277", "size": 1065033128, "dirname": "",
         "modification": "2026-08-19 08:18:20"},
        {"name": "output.root", "size": 10865224, "dirname": "/workDir",
         "modification": "2026-08-19 06:07:49"},
        {"name": "output.root", "size": 1648, "dirname": "/workDir/workDir/hist",
         "modification": "2026-08-19 01:43:21"},
        {"name": "output.root", "size": 1910, "dirname": "/workDir/workDir/input",
         "modification": "2026-08-19 01:43:21"},
        {"name": "setup.sh", "size": 6624,
         "dirname": "/workDir/usr/UserAnalysis/1.0.0/InstallArea/x86_64-el9-gcc15-opt",
         "modification": "2026-08-19 01:43:02"},
    ],
}


def _patch_listing_response(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
    status: int = 200,
) -> None:
    """Patch ``cached_fetch_jsonish`` so the real listing parser runs on *payload*.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        payload: Parsed-JSON value the cache layer should hand back.
        status: HTTP status the cache layer should report.
    """
    import askpanda_atlas._cache as cache_mod

    monkeypatch.setattr(
        cache_mod,
        "cached_fetch_jsonish",
        lambda url, timeout: (status, "application/json", "", payload),
    )


def test_file_index_keys_on_full_relative_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-basename files under different dirnames get distinct index keys.

    Basename keying let the last entry parsed win, so a nested ``output.root``
    could answer a size question about a root-level file of the same name.
    """
    from askpanda_atlas.log_analysis_impl import _fetch_file_index

    _patch_listing_response(monkeypatch, _JOB_7263525363_LISTING)
    index = _fetch_file_index(7263525363, "https://bigpanda.cern.ch", 30)

    assert index is not None
    assert index["workDir/output.root"] == 10865224
    assert index["workDir/workDir/hist/output.root"] == 1648
    assert index["workDir/workDir/input/output.root"] == 1910
    assert (
        index["workDir/usr/UserAnalysis/1.0.0/InstallArea/"
              "x86_64-el9-gcc15-opt/setup.sh"] == 6624
    )
    # Three distinct output.root entries survive rather than collapsing to one.
    assert sum(1 for key in index if key.endswith("output.root")) == 3


def test_file_index_leaves_root_level_names_unprefixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dirname of "" yields a bare basename key, so existing lookups still work."""
    from askpanda_atlas.log_analysis_impl import _fetch_file_index

    _patch_listing_response(monkeypatch, _JOB_7263525363_LISTING)
    index = _fetch_file_index(7263525363, "https://bigpanda.cern.ch", 30)

    assert index is not None
    assert index["payload.stdout"] == 454904
    assert index["payload.stderr"] == 0
    assert index["core.18277"] == 1065033128


def test_top_level_file_index_drops_nested_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top-level view keeps only job-root files, so namesakes cannot answer."""
    from askpanda_atlas.log_analysis_impl import (
        _fetch_file_index,
        _top_level_file_index,
    )

    _patch_listing_response(monkeypatch, _JOB_7263525363_LISTING)
    top_level = _top_level_file_index(
        _fetch_file_index(7263525363, "https://bigpanda.cern.ch", 30)
    )

    assert top_level is not None
    assert set(top_level) == {
        "payload.stdout", "payload.stderr", "my_release_setup.sh", "core.18277",
    }
    assert not any("/" in key for key in top_level)


def test_top_level_file_index_propagates_none() -> None:
    """An unavailable listing stays unavailable through the top-level view.

    Collapsing ``None`` to ``{}`` here would turn "listing unknown" into
    "no files listed", and every downstream fail-open check depends on the
    distinction.
    """
    from askpanda_atlas.log_analysis_impl import _top_level_file_index

    assert _top_level_file_index(None) is None


def test_listing_error_is_advisory_when_files_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty error field with a populated files array is not fatal.

    BigPanDA reports ``slow_downloading:...`` for a large job log while
    returning a complete listing; rejecting it would make every big-tarball
    job undiagnosable.
    """
    from askpanda_atlas.log_analysis_impl import _fetch_file_listing

    _patch_listing_response(monkeypatch, _JOB_7263525363_LISTING)
    listing = _fetch_file_listing(7263525363, "https://bigpanda.cern.ch", 30)

    assert listing is not None
    assert len(listing) == len(_JOB_7263525363_LISTING["files"])


def test_listing_error_with_no_files_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error field with no usable entries reports "unknown", not "empty"."""
    from askpanda_atlas.log_analysis_impl import _fetch_file_listing

    _patch_listing_response(
        monkeypatch, {"error": "job log not found", "files": []}
    )
    assert _fetch_file_listing(1111, "https://bigpanda.cern.ch", 30) is None


def test_listing_without_error_and_no_files_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean but empty listing is a definite "no files", distinct from None."""
    from askpanda_atlas.log_analysis_impl import _fetch_file_listing

    _patch_listing_response(monkeypatch, {"error": "", "files": []})
    assert _fetch_file_listing(1111, "https://bigpanda.cern.ch", 30) == []


def test_listing_normalisation_preserves_modification_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UTC modification string survives normalisation.

    Downstream acquisition restores it with ``os.utime`` so that the
    analyzer's core-versus-log mtime comparison stays meaningful; dropping it
    here would silently disable that.
    """
    from askpanda_atlas.log_analysis_impl import _fetch_file_listing

    _patch_listing_response(monkeypatch, _JOB_7263525363_LISTING)
    listing = _fetch_file_listing(7263525363, "https://bigpanda.cern.ch", 30)

    assert listing is not None
    by_path = {record["relative_path"]: record for record in listing}
    assert by_path["core.18277"]["modification"] == "2026-08-19 08:18:20"
    assert by_path["payload.stdout"]["modification"] == "2026-08-19 06:08:46"
    assert by_path["workDir/output.root"]["dirname"] == "workDir"


def test_listing_skips_unusable_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries that are not dicts or carry no name are dropped, not fatal."""
    from askpanda_atlas.log_analysis_impl import _fetch_file_listing

    _patch_listing_response(
        monkeypatch,
        {
            "error": "",
            "files": [
                "not-a-dict",
                {"size": 10, "dirname": ""},
                {"name": "pilotlog.txt", "size": "not-a-number", "dirname": ""},
            ],
        },
    )
    listing = _fetch_file_listing(1111, "https://bigpanda.cern.ch", 30)

    assert listing is not None
    assert len(listing) == 1
    assert listing[0]["relative_path"] == "pilotlog.txt"
    assert listing[0]["size_bytes"] == 0


# ---------------------------------------------------------------------------
# Core-dump probe
# ---------------------------------------------------------------------------

def _core_listing(
    size_bytes: int = 1065033128,
    name: str = "core.18277",
    dirname: str = "",
) -> list[dict[str, Any]]:
    """Return a minimal listing containing one core file plus a payload stream.

    Args:
        size_bytes: Size of the core file.
        name: Core filename.
        dirname: Directory the core file sits in.

    Returns:
        Normalised listing records.
    """
    return [
        {"relative_path": "payload.stdout", "name": "payload.stdout",
         "dirname": "", "size_bytes": 454904,
         "modification": "2026-08-19 06:08:46"},
        {"relative_path": f"{dirname}/{name}" if dirname else name, "name": name,
         "dirname": dirname, "size_bytes": size_bytes,
         "modification": "2026-08-19 08:18:20"},
    ]


def test_core_dump_probe_reports_present_core() -> None:
    """A non-empty core.<pid> in the listing is reported as present and usable."""
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    ev = _build_core_dump_evidence(_core_listing(), 1150)

    assert ev["core_dump_probe_state"] == "present"
    assert ev["core_dump_available"] is True
    assert ev["core_dump_total_bytes"] == 1065033128
    assert [c["name"] for c in ev["core_dump_candidates"]] == ["core.18277"]
    assert ev["core_dump_candidates"][0]["modification"] == "2026-08-19 08:18:20"


def test_core_dump_probe_reports_truncated_core() -> None:
    """A zero-length core means capture was interrupted, not that none exists.

    Collapsing this into "absent" would hide that the kernel had started
    writing a core and was killed mid-write.
    """
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    ev = _build_core_dump_evidence(_core_listing(size_bytes=0), 1150)

    assert ev["core_dump_probe_state"] == "truncated"
    assert ev["core_dump_available"] is False
    assert ev["core_dump_offer_md"] == ""


def test_core_dump_probe_reports_timed_out_for_looping_kill() -> None:
    """No core after a looping-job kill is reported as a timed-out capture."""
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    listing = [
        {"relative_path": "payload.stdout", "name": "payload.stdout",
         "dirname": "", "size_bytes": 100, "modification": ""},
    ]
    ev = _build_core_dump_evidence(listing, 1150)

    assert ev["core_dump_probe_state"] == "timed_out"
    assert ev["core_dump_available"] is False
    assert ev["core_dump_candidates"] == []
    assert ev["core_dump_total_bytes"] == 0


def test_core_dump_probe_reports_absent_for_other_failures() -> None:
    """No core on a non-looping failure is simply absent, not a timed-out capture."""
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    listing = [
        {"relative_path": "pilotlog.txt", "name": "pilotlog.txt",
         "dirname": "", "size_bytes": 100, "modification": ""},
    ]
    ev = _build_core_dump_evidence(listing, 1305)

    assert ev["core_dump_probe_state"] == "absent"
    assert ev["core_dump_available"] is False


def test_core_dump_probe_unavailable_listing_is_not_a_negative() -> None:
    """An unfetched listing reports None, never False.

    Reporting False would let the answer state that a job has no core dump on
    the strength of a failed HTTP request.
    """
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    ev = _build_core_dump_evidence(None, 1150)

    assert ev["core_dump_probe_state"] == "not_probed"
    assert ev["core_dump_available"] is None
    assert ev["core_dump_offer_md"] == ""


def test_core_dump_probe_ignores_lookalike_filenames() -> None:
    """Only core.<pid> matches; unrelated names containing "core" do not."""
    from askpanda_atlas.log_analysis_impl import _find_core_dump_candidates

    listing = [
        {"relative_path": n, "name": n, "dirname": "", "size_bytes": 10,
         "modification": ""}
        for n in (
            "core_dump_config.txt", ".corefile", "core.", "core", "hardcore.1",
            "core.18277.gz", "core.18277",
        )
    ]
    assert [c["name"] for c in _find_core_dump_candidates(listing)] == ["core.18277"]


def test_core_dump_probe_orders_candidates_largest_first() -> None:
    """Multiple cores are ordered by size so the offer names the usable one."""
    from askpanda_atlas.log_analysis_impl import _find_core_dump_candidates

    listing = [
        {"relative_path": "core.1", "name": "core.1", "dirname": "",
         "size_bytes": 0, "modification": ""},
        {"relative_path": "core.2", "name": "core.2", "dirname": "",
         "size_bytes": 2048, "modification": ""},
        {"relative_path": "core.3", "name": "core.3", "dirname": "",
         "size_bytes": 4096, "modification": ""},
    ]
    assert [c["name"] for c in _find_core_dump_candidates(listing)] == [
        "core.3", "core.2", "core.1",
    ]


def test_core_dump_offer_only_for_looping_job_kill() -> None:
    """A usable core on a non-looping failure is recorded but not offered.

    The evidence keys stay populated so an explicit request still works; only
    the proactive offer is narrowed.
    """
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    ev = _build_core_dump_evidence(_core_listing(), 1305)

    assert ev["core_dump_available"] is True
    assert ev["core_dump_offer_md"] == ""


def test_core_dump_offer_names_file_and_size() -> None:
    """The offer carries the real filename and size, not an LLM paraphrase."""
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    offer = _build_core_dump_evidence(_core_listing(), 1150)["core_dump_offer_md"]

    assert offer.startswith("\n\n")
    assert "`core.18277`" in offer
    assert "1.1 GB" in offer
    assert "further core file" not in offer
    assert offer.rstrip().endswith("Analyse it?")


def test_core_dump_offer_mentions_additional_cores() -> None:
    """With more than one usable core the offer says so rather than hiding them."""
    from askpanda_atlas.log_analysis_impl import _build_core_dump_evidence

    listing = _core_listing() + [
        {"relative_path": "core.999", "name": "core.999", "dirname": "",
         "size_bytes": 4096, "modification": ""},
    ]
    offer = _build_core_dump_evidence(listing, 1150)["core_dump_offer_md"]

    assert "`core.18277`" in offer
    assert "1 further core file present" in offer


def test_core_dump_offer_suppressed_when_tool_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no analysis tool registered the probe reports but never offers.

    This is the mirrored-plugin case: the ePIC copy sets
    ``_CORE_DUMP_ANALYSIS_AVAILABLE`` to False, and an offer that cannot be
    accepted must not reach the user.
    """
    import askpanda_atlas.log_analysis_impl as impl

    monkeypatch.setattr(impl, "_CORE_DUMP_ANALYSIS_AVAILABLE", False)
    ev = impl._build_core_dump_evidence(_core_listing(), 1150)

    assert ev["core_dump_available"] is True
    assert ev["core_dump_probe_state"] == "present"
    assert ev["core_dump_offer_md"] == ""


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0 B"),
        (225, "225 B"),
        (999, "999 B"),
        (1000, "1.0 kB"),
        (454904, "454.9 kB"),
        (1065033128, "1.1 GB"),
        (2 * 10 ** 12, "2.0 TB"),
    ],
)
def test_format_bytes(size_bytes: int, expected: str) -> None:
    """Sizes are rendered in decimal units matching the job monitor's figures."""
    from askpanda_atlas.log_analysis_impl import _format_bytes

    assert _format_bytes(size_bytes) == expected


def test_core_dump_evidence_reaches_tool_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe keys survive the full tool call for a looping-job kill.

    Guards the wiring, not the probe: a helper that is written but never
    called is the failure mode this catches.
    """
    looping_job = {
        **_SAMPLE_JOB_PAYLOAD,
        "jobstatus": "failed",
        "piloterrorcode": 1150,
        "piloterrordiag": "Looping job killed by pilot",
    }
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(looping_job),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: (
            "pilot log body without a traceback\n"
        ),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        lambda job_id, base_url, timeout: _core_listing(),
    )

    ev = _unpack(
        asyncio.run(panda_log_analysis_tool.call({"job_id": 7263525363}))
    )["evidence"]

    assert ev["core_dump_probe_state"] == "present"
    assert ev["core_dump_available"] is True
    assert ev["core_dump_total_bytes"] == 1065033128
    assert "`core.18277`" in ev["core_dump_offer_md"]


def test_classify_failure_setup_release_not_found() -> None:
    """setup_release_not_found is classified when setup log excerpt is used."""
    setup_excerpt = (
        "AtlasSetup(Warning): aarch64 not supported on x86_64\n"
        "!!!ERROR!!! No matched release is found\n"
        "22:50:44 2026/04/19\n"
    )
    job = {
        **_SAMPLE_JOB_PAYLOAD,
        "piloterrordiag": "General payload setup verification error",
    }
    result = classify_failure(job, setup_excerpt)
    assert result == "setup_release_not_found", (
        f"Expected 'setup_release_not_found', got '{result}'. "
        "The setup log excerpt should drive classification when the "
        "release is not found."
    )


# ---------------------------------------------------------------------------
# Integration tests: setup.stdout-first flow and zero-length guards
# ---------------------------------------------------------------------------

# Shared setup log fixtures
_SETUP_LOG_WITH_ERROR = (
    "Info: /cvmfs mounted\n"
    "sourcing /srv/my_release_setup.sh\n"
    "AtlasSetup(WARNING): Deprecated tag ignored\n"
    "AtlasSetup(Warning): aarch64 not supported on x86_64-like machine\n"
    "\t/cvmfs/atlas.cern.ch/repo/sw/software/23.0/.../aarch64-centos7-gcc11-opt\n"
    "!!!ERROR!!! No matched release is found\n"
    "22:50:44 2026/04/19\n"
)

_SETUP_LOG_CLEAN = (
    "Info: /cvmfs mounted\n"
    "sourcing /srv/my_release_setup.sh\n"
    "Athena release 23.0.32 set up successfully\n"
)


def _index_with_zero_payload() -> dict[str, int]:
    """Return a file index where setup.stdout has content but payload logs are empty."""
    return {
        "setup.stdout": len(_SETUP_LOG_WITH_ERROR),
        "setup.stderr": 0,
        "payload.stdout": 0,
        "payload.stderr": 0,
    }


def _listing_stub(
    sizes: dict[str, int] | None,
    modification: str = "2026-08-19 01:42:51",
) -> Callable[[int, str, int], list[dict[str, Any]] | None]:
    """Build a ``_fetch_file_listing`` replacement from a ``{name: size}`` map.

    The production seam returns normalised listing records rather than a size
    index, so tests that want to control which files appear non-empty supply
    the map they care about and let this helper expand it into root-level
    records.

    Args:
        sizes: Mapping of root-level filename to size in bytes, or ``None``
            to simulate an unavailable listing (the fail-open case).
        modification: UTC timestamp string applied to every record.

    Returns:
        A callable with the ``(job_id, base_url, timeout)`` signature of
        :func:`~askpanda_atlas.log_analysis_impl._fetch_file_listing`.
    """
    def _stub(
        job_id: int, base_url: str, timeout: int,
    ) -> list[dict[str, Any]] | None:
        if sizes is None:
            return None
        return [
            {
                "relative_path": name,
                "name": name,
                "dirname": "",
                "size_bytes": size,
                "modification": modification,
            }
            for name, size in sizes.items()
        ]

    return _stub


def test_setup_log_checked_before_payload_for_code_1305(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup.stdout is the first file fetched for pilot error 1305.

    The fetch order must be setup.stdout → (conditionally) payload.stdout →
    payload.stderr.  setup.stdout must appear first in the fetched list.
    """
    fetched: list[str] = []

    def _capture_log(
        job_id: int, filename: str, base_url: str, timeout: int
    ) -> str | None:
        fetched.append(filename)
        if filename == "setup.stdout":
            return _SETUP_LOG_CLEAN  # no error → fall through to payload
        if filename == "payload.stdout":
            return "Some payload output\n"
        return None

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({
            "setup.stdout": 100, "payload.stdout": 100, "payload.stderr": 0
        }),
    )

    asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    assert fetched[0] == "setup.stdout", (
        f"First fetched file should be setup.stdout, got {fetched}"
    )


def test_setup_error_skips_payload_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When setup.stdout contains a fatal error, payload logs are never fetched.

    The pilot error 1305 job has a fatal 'No matched release is found' in
    setup.stdout.  payload.stdout and payload.stderr would be empty anyway;
    fetching them wastes a round-trip and must be suppressed.
    """
    fetched: list[str] = []

    def _capture_log(
        job_id: int, filename: str, base_url: str, timeout: int
    ) -> str | None:
        fetched.append(filename)
        if filename == "setup.stdout":
            return _SETUP_LOG_WITH_ERROR
        # payload logs should never be reached
        return None

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub(_index_with_zero_payload()),
    )

    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    res = _unpack(result)
    ev = res["evidence"]

    assert "payload.stdout" not in fetched, (
        "payload.stdout must not be fetched when setup error found"
    )
    assert "payload.stderr" not in fetched, (
        "payload.stderr must not be fetched when setup error found"
    )
    assert ev["failure_type"] == "setup_release_not_found"
    assert ev["log_available"] is True
    assert ev["setup_log_url"] is not None
    assert "No matched release" in (ev["log_excerpt"] or "")


def test_setup_error_excerpt_used_as_primary_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When setup error found, the setup log excerpt appears in evidence.log_excerpt."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: (
            _SETUP_LOG_WITH_ERROR if filename == "setup.stdout" else None
        ),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub(_index_with_zero_payload()),
    )

    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    ev = _unpack(result)["evidence"]

    assert ev["setup_log_excerpt"] is not None
    assert "No matched release" in ev["setup_log_excerpt"]
    # The primary log_excerpt must also contain the setup content
    assert "No matched release" in (ev["log_excerpt"] or "")


def test_zero_length_payload_files_not_downloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """payload.stdout and payload.stderr with size=0 in the index are never fetched.

    When the file index confirms both payload files are zero-length and
    setup.stdout has no error, no download attempt should be made for the
    zero-length files.
    """
    fetched: list[str] = []

    def _capture_log(
        job_id: int, filename: str, base_url: str, timeout: int
    ) -> str | None:
        fetched.append(filename)
        return _SETUP_LOG_CLEAN if filename == "setup.stdout" else None

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({
            "setup.stdout": len(_SETUP_LOG_CLEAN),
            "payload.stdout": 0,
            "payload.stderr": 0,
        }),
    )

    asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))

    assert "payload.stdout" not in fetched, (
        "payload.stdout confirmed zero-length; must not be downloaded"
    )
    assert "payload.stderr" not in fetched, (
        "payload.stderr confirmed zero-length; must not be downloaded"
    )


def test_setup_log_nonempty_no_error_falls_through_to_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If setup.stdout has no error, payload.stdout is still fetched (if non-empty).

    A clean setup.stdout should not suppress payload log downloads —
    the payload may still have failed for other reasons.
    """
    fetched: list[str] = []

    def _capture_log(
        job_id: int, filename: str, base_url: str, timeout: int
    ) -> str | None:
        fetched.append(filename)
        if filename == "setup.stdout":
            return _SETUP_LOG_CLEAN
        if filename == "payload.stdout":
            return "AthenaMP ERROR: abort\n"
        return None

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({
            "setup.stdout": len(_SETUP_LOG_CLEAN),
            "payload.stdout": 100,
            "payload.stderr": 0,
        }),
    )

    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    ev = _unpack(result)["evidence"]

    assert "payload.stdout" in fetched, (
        "payload.stdout must be fetched when setup.stdout is clean"
    )
    assert ev["log_available"] is True


def test_file_index_unavailable_falls_through_to_old_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the file index cannot be fetched, downloads proceed as before.

    _fetch_file_index returning None must not suppress any log downloads;
    the system falls back to attempting all files (fail-open).
    """
    fetched: list[str] = []

    def _capture_log(
        job_id: int, filename: str, base_url: str, timeout: int
    ) -> str | None:
        fetched.append(filename)
        if filename == "setup.stdout":
            return _SETUP_LOG_CLEAN
        if filename == "payload.stdout":
            return "Some payload output\n"
        return None

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text", _capture_log
    )
    # Index unavailable — None → fail-open
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub(None),
    )

    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    ev = _unpack(result)["evidence"]

    assert "setup.stdout" in fetched
    assert "payload.stdout" in fetched
    assert ev["log_available"] is True


def test_setup_log_url_in_links_md(monkeypatch: pytest.MonkeyPatch) -> None:
    """links_md includes the Setup Log URL when setup.stdout was fetched."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_SAMPLE_JOB_PAYLOAD),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: (
            _SETUP_LOG_WITH_ERROR if filename == "setup.stdout" else None
        ),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub(_index_with_zero_payload()),
    )

    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 1111}))
    ev = _unpack(result)["evidence"]

    links_md = ev.get("links_md", "")
    assert "Setup Log" in links_md, (
        f"links_md must contain 'Setup Log' when setup.stdout was fetched. Got:\n{links_md}"
    )
    assert "setup.stdout" in links_md


def test_setup_log_url_absent_when_not_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup_log_url is None in evidence when no setup log was fetched.

    For non-1305 errors (pilotlog.txt path), setup.stdout is never attempted
    and setup_log_url must be None.
    """
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(
            _SAMPLE_JOB_STAGEIN_TIMEOUT
        ),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: _SAMPLE_PILOT_LOG,
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({"pilotlog.txt": 500}),
    )

    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 6799893074}))
    ev = _unpack(result)["evidence"]

    assert ev.get("setup_log_url") is None


# ---------------------------------------------------------------------------
# Regression: job 7261310898 — transform download timeout (pilot error 1310)
#
# Before the traceback-first extractor, this job produced a wholly wrong
# diagnosis.  Pilot error code 1310 has no useful entry in _PILOT_CODE_PATTERNS,
# so extraction fell back to using piloterrordiag as a literal regex.  The
# metadata reads "Exception caught during payload execution" while the log
# record reads "execute payloads caught an exception (cannot recover)" — no
# match — so the excerpt degraded to the tail of pilotlog.txt, which for a
# failed job is stage-out and log-archiving boilerplate: 'removed /tmp/...'
# lines, an `ls -lF` directory listing and a `tar cvfz` command.
#
# The LLM, given only that, inferred a cause from the *file sizes* in the
# directory listing and reported a "remote file open failure / stage-in"
# problem.  The real cause was a TimeoutError fetching the runGen transform
# over HTTP — the payload never started at all.
#
# failure_type also came out as "timeout", but only because the boilerplate
# contained the substring "using timeout=90 s" from the tar command.  Right
# answer, no evidence.
# ---------------------------------------------------------------------------

_JOB_7261310898: dict = {
    "pandaid": 7261310898,
    "jobstatus": "failed",
    "jobsubstatus": "",
    "computingsite": "DESY-HH",
    "cloud": "DE",
    "atlasrelease": "Atlas-25.2.7",
    "jeditaskid": None,
    "attemptnr": 1,
    "maxattempt": 3,
    "transformation": "runGen-00-00-02",
    "piloterrorcode": 1310,
    "piloterrordiag": "Exception caught during payload execution",
    "exeerrorcode": 0,
    "exeerrordiag": "",
    "taskbuffererrorcode": 0,
    "taskbuffererrordiag": "",
    "ddmerrorcode": 0,
    "ddmerrordiag": "",
    "starttime": "2026-08-17 08:37:05",
    "endtime": "2026-08-17 08:40:33",
    "duration": "0:0:03:28",
    "commandtopilot": "",
    "pilotid": "https://aipanda100.cern.ch/log.tgz|PR|3.14.0.22",
}

_JOB_7261310898_TRACEBACK: str = (
    "2026-08-17 08:38:24,986 | CRITICAL | pilot.control.payload            | "
    "execute_payloads          | execute payloads caught an exception "
    "(cannot recover): timed out, Traceback (most recent call last):\n"
    '  File "/tmp/atlas_QCSsk3r1/pilot3/pilot/control/payload.py", line 308, '
    "in execute_payloads\n"
    "    exit_code, diagnostics = payload_executor.run()\n"
    '  File "/tmp/atlas_QCSsk3r1/pilot3/pilot/control/payloads/generic.py", '
    "line 755, in get_payload_command\n"
    "    cmd = user.get_payload_command(self.__job, args=self.__args)\n"
    '  File "/tmp/atlas_QCSsk3r1/pilot3/pilot/user/atlas/common.py", line 911, '
    "in get_normal_payload_command\n"
    "    exitcode, diagnostics, trf_name = get_analysis_trf(job.transformation)\n"
    '  File "/tmp/atlas_QCSsk3r1/pilot3/pilot/user/atlas/setup.py", line 294, '
    "in get_analysis_trf\n"
    "    status, diagnostics = download_transform(trf, transform_name, workdir)\n"
    '  File "/tmp/atlas_QCSsk3r1/pilot3/pilot/user/atlas/setup.py", line 347, '
    "in download_transform\n"
    "    content = download_file(url)\n"
    '  File "/tmp/atlas_QCSsk3r1/pilot3/pilot/util/https.py", line 2301, '
    "in download_file\n"
    "    with urllib.request.urlopen(req, timeout=timeout) as response:\n"
    '  File "/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase/x86_64/python/'
    '3.14.6-x86_64-el9/lib/python3.14/socket.py", line 729, in readinto\n'
    "    return self._sock.recv_into(b)\n"
    "TimeoutError: timed out\n"
)

# The stage-out boilerplate that used to be returned as the whole excerpt.
_JOB_7261310898_TAIL: str = "".join([
    "2026-08-17 08:38:25,821 | DEBUG    | pilot.util.filehandling          | "
    "remove                    | removed /tmp/atlas_QCSsk3r1/PILOTVERSION\n",
    "2026-08-17 08:38:26,004 | DEBUG    | pilot.user.atlas.common          | "
    "list_work_dir             | total 72\n",
    "-rw-r--r--. 1 atlasprd000 atlasprd     0 Aug 17 10:37 payload.stderr\n",
    "-rw-r--r--. 1 atlasprd000 atlasprd     0 Aug 17 10:37 payload.stdout\n",
    "-rw-r--r--. 1 atlasprd000 atlasprd 28218 Aug 17 10:37 remote_open.stderr\n",
    "2026-08-17 08:38:26,005 | INFO     | pilot.control.data               | "
    "create_log                | will create archive /tmp/x.job.log.tgz using "
    "timeout=90 s for directory size=0.035 MB\n",
    "2026-08-17 08:38:26,005 | INFO     | pilot.util.container             | "
    "print_executable          | executing command: pwd;tar cvfz "
    "/tmp/x.job.log.tgz tarball_PandaJob_7261310898_DESY-HH; echo $?\n",
] * 8)

_JOB_7261310898_PILOTLOG: str = (
    "2026-08-17 08:36:41,102 | INFO     | pilot | main | pilot version: 3.14.0.22\n"
    + (
        "2026-08-17 08:37:00,000 | DEBUG    | pilot.util.something          | "
        "doing_work                | routine start-up chatter\n"
    ) * 150
    + _JOB_7261310898_TRACEBACK
    + _JOB_7261310898_TAIL
)


def _analyse_job_7261310898(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run the tool against the job 7261310898 fixture and return its evidence.

    Args:
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        The ``evidence`` sub-dict from the tool result.
    """
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_JOB_7261310898),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: _JOB_7261310898_PILOTLOG,
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({"pilotlog.txt": len(_JOB_7261310898_PILOTLOG)}),
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 7261310898}))
    return _unpack(result)["evidence"]


def test_job_7261310898_excerpt_contains_the_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The excerpt must contain the traceback, not the stage-out boilerplate."""
    ev = _analyse_job_7261310898(monkeypatch)
    excerpt = ev["log_excerpt"]
    assert "TimeoutError: timed out" in excerpt
    assert "download_transform" in excerpt
    assert "get_analysis_trf" in excerpt


def test_job_7261310898_excerpt_excludes_stageout_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The excerpt must not be dominated by the log-archiving boilerplate.

    The `tar cvfz` command and the `ls -lF` listing are what the old tail
    extraction returned; an excerpt built around them invites the LLM to invent
    a cause from file sizes.
    """
    ev = _analyse_job_7261310898(monkeypatch)
    excerpt = ev["log_excerpt"]
    assert "tar cvfz" not in excerpt
    assert "remote_open.stderr" not in excerpt


def test_job_7261310898_classified_as_transform_download_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure is a transform download timeout, not a generic timeout.

    The old classification of "timeout" came from the substring "timeout=90 s"
    in the tar command, not from any evidence about the failure.
    """
    ev = _analyse_job_7261310898(monkeypatch)
    assert ev["failure_type"] == "transform_download_timeout"


def test_job_7261310898_exception_evidence_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parsed exception is promoted to first-class evidence keys."""
    ev = _analyse_job_7261310898(monkeypatch)
    assert ev["traceback_available"] is True
    assert ev["exception_type"] == "TimeoutError"
    assert ev["exception_message"] == "timed out"
    deepest = ev["deepest_pilot_frame"]
    assert deepest["pilot_path"] == "pilot/util/https.py"
    assert deepest["lineno"] == 2301
    assert deepest["func"] == "download_file"


def test_job_7261310898_pilot_version_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pilot version is parsed from the full log for GitHub tag pinning."""
    ev = _analyse_job_7261310898(monkeypatch)
    assert ev["pilot_version"] == "3.14.0.22"


def test_job_7261310898_code_analysis_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follow-up offer names the real pilot frame from the traceback."""
    ev = _analyse_job_7261310898(monkeypatch)
    offer = ev["code_analysis_offer_md"]
    assert "pilot/util/https.py:2301" in offer
    assert "download_file" in offer


def test_job_7261310898_summary_mentions_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text summary reports the exception rather than the misleading diag.

    piloterrordiag says "during payload execution" but the traceback shows the
    failure happened while *building* the payload command, so the payload never
    ran.  The summary must not repeat the diag as the headline.
    """
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(_JOB_7261310898),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: _JOB_7261310898_PILOTLOG,
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({"pilotlog.txt": 5000}),
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 7261310898}))
    text = _unpack(result)["text"]
    assert "TimeoutError" in text
    assert "pilot/util/https.py:2301" in text


def test_pilot_version_falls_back_to_pilotid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the pilot log is unavailable the version comes from pilotid.

    Pilot error 1305 reads payload.stdout, so pilotlog.txt is never downloaded
    and the start-up version line is not seen.
    """
    job = {**_JOB_7261310898, "piloterrorcode": 1305}
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(job),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: "payload output\n",
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({"payload.stdout": 20}),
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 7261310898}))
    ev = _unpack(result)["evidence"]
    assert ev["pilot_version"] == "3.14.0.22"


# ---------------------------------------------------------------------------
# Exception-driven classification
# ---------------------------------------------------------------------------

def _exc(traceback_text: str, level: str = "CRITICAL"):
    """Parse traceback text into an ExceptionInfo for classification tests.

    Args:
        traceback_text: Traceback text to parse.
        level: Log level to attribute to the traceback.

    Returns:
        Parsed ``ExceptionInfo``.
    """
    from askpanda_atlas._traceback_parse import parse_exception
    return parse_exception(traceback_text, level)


def test_classify_from_exception_beats_substring_noise() -> None:
    """A parsed exception overrides misleading substrings in the excerpt.

    The excerpt here contains "timeout=90 s" (from the pilot's tar command),
    which the substring table would classify as a plain timeout.
    """
    noisy_excerpt = "will create archive using timeout=90 s for directory size"
    result = classify_failure(
        _JOB_7261310898, noisy_excerpt, _exc(_JOB_7261310898_TRACEBACK),
    )
    assert result == "transform_download_timeout"


def test_classify_failure_without_exception_uses_substring_table() -> None:
    """Omitting the exception preserves the original substring behaviour."""
    job = {**_JOB_7261310898, "piloterrordiag": "Segmentation fault"}
    assert classify_failure(job, "") == "segfault"


def test_classify_from_exception_pilot_exception_for_unknown() -> None:
    """An unrecognised exception in pilot code is not blamed on the payload.

    The substring table would match "traceback" and report payload_error, which
    is misleading when the payload never ran.
    """
    traceback_text = (
        "2026-08-17 08:00:00,000 | CRITICAL | pilot.x | f | boom, "
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/mystery.py", line 5, in weird\n'
        "    x()\n"
        "ZeroDivisionError: division by zero\n"
    )
    result = classify_failure(_JOB_7261310898, traceback_text, _exc(traceback_text))
    assert result == "pilot_exception"


def test_classify_from_exception_preserves_monitoring_category() -> None:
    """The getpwuid failure keeps its pilot_monitoring_error category.

    bamboo_answer and planner routing reference that category by name, so the
    exception-driven classifier must not rename it.
    """
    traceback_text = (
        "2026-04-17 02:05:23,077 | WARNING  | pilot.x | f | "
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/psutils.py", line 428, '
        "in list_processes_and_threads\n"
        "    current_user = getpass.getuser()\n"
        "KeyError: 'getpwuid(): uid not found: 6435'\n"
    )
    result = classify_failure({}, traceback_text, _exc(traceback_text, "WARNING"))
    assert result == "pilot_monitoring_error"


def test_classify_reassigned_wins_over_exception() -> None:
    """JEDI reassignment outranks any incidental exception in the log."""
    job = {
        "taskbuffererrordiag": "reassigned by JEDI",
        "commandtopilot": "tobekilled",
    }
    result = classify_failure(job, "", _exc(_JOB_7261310898_TRACEBACK))
    assert result == "reassigned_by_jedi"


def test_no_traceback_yields_false_evidence_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception evidence keys are always present, even with no traceback."""
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(
            _SAMPLE_JOB_STAGEIN_TIMEOUT
        ),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text",
        lambda job_id, filename, base_url, timeout: _SAMPLE_PILOT_LOG,
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({"pilotlog.txt": 500}),
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 6799893074}))
    ev = _unpack(result)["evidence"]
    assert ev["traceback_available"] is False
    assert ev["exception_type"] is None
    assert ev["deepest_pilot_frame"] is None
    assert ev["code_analysis_offer_md"] == ""


def test_payload_stderr_traceback_wins_over_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both payload files have a traceback, stderr's exception is reported.

    Python tracebacks and abort messages go to stderr, so that is where the
    exception that actually terminated the payload lives.
    """
    job = {**_SAMPLE_JOB_STAGEIN_TIMEOUT, "piloterrorcode": 1305}
    stdout_text = (
        "Traceback (most recent call last):\n"
        '  File "/x/warmup.py", line 1, in <module>\n'
        "    warn()\n"
        "UserWarning: harmless\n"
    )
    stderr_text = (
        "Traceback (most recent call last):\n"
        '  File "/x/analysis.py", line 9, in <module>\n'
        "    main()\n"
        "MemoryError: cannot allocate\n"
    )

    def _fetch(job_id: int, filename: str, base_url: str, timeout: int):
        if filename == "payload.stderr":
            return stderr_text
        if filename == "payload.stdout":
            return stdout_text
        return None

    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_metadata",
        lambda job_id, base_url, timeout: _make_metadata_response(job),
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_log_text", _fetch
    )
    monkeypatch.setattr(
        "askpanda_atlas.log_analysis_impl._fetch_file_listing",
        _listing_stub({
            "payload.stdout": len(stdout_text),
            "payload.stderr": len(stderr_text),
        }),
    )
    result = asyncio.run(panda_log_analysis_tool.call({"job_id": 6799893074}))
    ev = _unpack(result)["evidence"]
    assert ev["exception_type"] == "MemoryError"
    assert ev["failure_type"] == "memory"
