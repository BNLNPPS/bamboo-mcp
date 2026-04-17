"""Tests for pilot_source_analysis tool (askpanda_atlas plugin).

No network access: GitHub fetches are patched throughout.
"""
from __future__ import annotations

import json
import textwrap
from unittest.mock import patch

import pytest

from askpanda_atlas.pilot_source_analysis_impl import (
    extract_function_source,
    fetch_and_analyse_pilot_source,
    parse_exception_line,
    parse_traceback_frames,
)


# ---------------------------------------------------------------------------
# Sample data — mirrors the real traceback from job 7099503721
# ---------------------------------------------------------------------------

_SAMPLE_LOG_EXCERPT = """\
2026-04-17 02:05:23,070 | WARNING  | Exception caught: 'getpwuid(): uid not found: 6435'
2026-04-17 02:05:23,077 | WARNING  | Traceback (most recent call last):
  File "/tmp/atlas_8GX3ynDr/pilot3/pilot/util/monitoring.py", line 193, in set_cpu_consumption_time
    cpuconsumptiontime = get_current_cpu_consumption_time(job.pid)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/tmp/atlas_8GX3ynDr/pilot3/pilot/util/processes.py", line 619, in get_current_cpu_consumption_time
    ps_cache = get_ps_cache()
               ^^^^^^^^^^^^^^
  File "/tmp/atlas_8GX3ynDr/pilot3/pilot/util/processes.py", line 191, in get_ps_cache
    _ps_cache = list_processes_and_threads()
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/tmp/atlas_8GX3ynDr/pilot3/pilot/util/psutils.py", line 428, in list_processes_and_threads
    current_user = getpass.getuser()
                   ^^^^^^^^^^^^^^^^^
  File "/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase/x86_64/python/3.11.10-x86_64-el9/lib/python3.11/getpass.py", line 169, in getuser
    return pwd.getpwuid(os.getuid())[0]
           ^^^^^^^^^^^^^^^^^^^^^^^^^
KeyError: 'getpwuid(): uid not found: 6435'
"""

_SAMPLE_PSUTILS_SOURCE = textwrap.dedent("""\
    import getpass
    import os

    def some_other_function():
        pass

    def list_processes_and_threads():
        \"\"\"List all processes and threads on the worker node.\"\"\"
        try:
            current_user = getpass.getuser()
        except KeyError:
            current_user = str(os.getuid())
        return []
""")

_SAMPLE_PROCESSES_SOURCE = textwrap.dedent("""\
    def get_ps_cache():
        return _list()

    def get_current_cpu_consumption_time(pid):
        ps_cache = get_ps_cache()
        return 0
""")

_SAMPLE_MONITORING_SOURCE = textwrap.dedent("""\
    def set_cpu_consumption_time(job):
        cpuconsumptiontime = get_current_cpu_consumption_time(job.pid)
        job.cpuconsumptiontime = cpuconsumptiontime
""")


# ---------------------------------------------------------------------------
# parse_traceback_frames
# ---------------------------------------------------------------------------

class TestParseTracebackFrames:
    def test_extracts_pilot_frames(self) -> None:
        frames = parse_traceback_frames(_SAMPLE_LOG_EXCERPT)
        paths = [f["pilot_path"] for f in frames]
        assert "pilot/util/monitoring.py" in paths
        assert "pilot/util/processes.py" in paths
        assert "pilot/util/psutils.py" in paths

    def test_excludes_cvmfs_frames(self) -> None:
        """Non-pilot3 frames (e.g. cpython stdlib in CVMFS) must be excluded."""
        frames = parse_traceback_frames(_SAMPLE_LOG_EXCERPT)
        for f in frames:
            assert not f["pilot_path"].startswith("/cvmfs")
            assert f["pilot_path"].startswith("pilot/")

    def test_deduplicates_frames(self) -> None:
        """processes.py appears twice in the traceback; must yield one entry per function."""
        frames = parse_traceback_frames(_SAMPLE_LOG_EXCERPT)
        keys = [(f["pilot_path"], f["func"]) for f in frames]
        assert len(keys) == len(set(keys))

    def test_empty_on_no_traceback(self) -> None:
        assert parse_traceback_frames("INFO | everything is fine") == []

    def test_correct_function_names(self) -> None:
        frames = parse_traceback_frames(_SAMPLE_LOG_EXCERPT)
        funcs = {f["func"] for f in frames}
        assert "list_processes_and_threads" in funcs
        assert "get_ps_cache" in funcs
        assert "get_current_cpu_consumption_time" in funcs
        assert "set_cpu_consumption_time" in funcs


# ---------------------------------------------------------------------------
# parse_exception_line
# ---------------------------------------------------------------------------

class TestParseExceptionLine:
    def test_extracts_keyerror(self) -> None:
        exc = parse_exception_line(_SAMPLE_LOG_EXCERPT)
        assert "getpwuid" in exc
        assert "uid not found" in exc

    def test_falls_back_to_exception_caught(self) -> None:
        log = "WARNING | Exception caught: 'some error'\n"
        exc = parse_exception_line(log)
        assert "some error" in exc

    def test_empty_on_no_exception(self) -> None:
        assert parse_exception_line("INFO | all good") == ""


# ---------------------------------------------------------------------------
# extract_function_source
# ---------------------------------------------------------------------------

class TestExtractFunctionSource:
    def test_extracts_named_function(self) -> None:
        src = extract_function_source(_SAMPLE_PSUTILS_SOURCE, "list_processes_and_threads")
        assert src is not None
        assert "def list_processes_and_threads" in src
        assert "getpass.getuser()" in src

    def test_returns_none_for_missing_function(self) -> None:
        assert extract_function_source(_SAMPLE_PSUTILS_SOURCE, "nonexistent_function") is None

    def test_returns_none_for_invalid_syntax(self) -> None:
        assert extract_function_source("def (broken syntax!!!", "broken") is None

    def test_excludes_other_functions(self) -> None:
        src = extract_function_source(_SAMPLE_PSUTILS_SOURCE, "list_processes_and_threads")
        assert src is not None
        assert "some_other_function" not in src

    def test_extracts_function_with_decorator(self) -> None:
        source = textwrap.dedent("""\
            def plain(): pass

            @some_decorator
            def decorated():
                return 42
        """)
        src = extract_function_source(source, "decorated")
        assert src is not None
        assert "@some_decorator" in src
        assert "return 42" in src


# ---------------------------------------------------------------------------
# fetch_and_analyse_pilot_source (network patched)
# ---------------------------------------------------------------------------

def _make_fetch_side_effect(source_map: dict[str, str]):
    """Return a side_effect for fetch_pilot_module keyed by pilot_path."""
    def _fetch(pilot_path: str, timeout: int):
        if pilot_path in source_map:
            return source_map[pilot_path], ""
        return None, f"HTTP 404 fetching {pilot_path}"
    return _fetch


class TestFetchAndAnalysePilotSource:
    def test_happy_path(self) -> None:
        source_map = {
            "pilot/util/psutils.py": _SAMPLE_PSUTILS_SOURCE,
            "pilot/util/processes.py": _SAMPLE_PROCESSES_SOURCE,
            "pilot/util/monitoring.py": _SAMPLE_MONITORING_SOURCE,
        }
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_make_fetch_side_effect(source_map),
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=7099503721,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="Exception caught: 'getpwuid(): uid not found: 6435'",
            )

        evidence = result["evidence"]
        assert evidence["job_id"] == 7099503721
        assert "getpwuid" in evidence["exception"]
        assert len(evidence["traceback_frames"]) > 0

        # The deepest pilot frame is list_processes_and_threads in psutils
        snippets = evidence["source_snippets"]
        assert "pilot/util/psutils.py::list_processes_and_threads" in snippets
        src = snippets["pilot/util/psutils.py::list_processes_and_threads"]
        assert "getpass.getuser()" in src

    def test_no_traceback_frames(self) -> None:
        result = fetch_and_analyse_pilot_source(
            job_id=1,
            log_excerpt="WARNING | no traceback here",
            pilot_error_diag="",
        )
        assert "error" in result["evidence"]
        assert "No pilot3 traceback frames" in result["evidence"]["error"]

    def test_fetch_error_recorded(self) -> None:
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            return_value=(None, "HTTP 404"),
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=2,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="",
            )
        evidence = result["evidence"]
        assert evidence["fetch_errors"]
        # No snippets if all fetches fail
        assert evidence["source_snippets"] == {}

    def test_files_fetched_list(self) -> None:
        source_map = {
            "pilot/util/psutils.py": _SAMPLE_PSUTILS_SOURCE,
            # processes.py and monitoring.py intentionally missing
        }
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_make_fetch_side_effect(source_map),
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=3,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="",
            )
        evidence = result["evidence"]
        assert "pilot/util/psutils.py" in evidence["files_fetched"]
        assert "pilot/util/processes.py" not in evidence["files_fetched"]

    def test_github_urls_present(self) -> None:
        source_map = {"pilot/util/psutils.py": _SAMPLE_PSUTILS_SOURCE}
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_make_fetch_side_effect(source_map),
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=4,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="",
            )
        urls = result["evidence"]["github_urls"]
        assert "pilot/util/psutils.py" in urls
        assert "github.com/PanDAWMS/pilot3" in urls["pilot/util/psutils.py"]

    def test_each_file_fetched_only_once(self) -> None:
        """processes.py appears twice in the traceback — must be fetched once."""
        call_counts: dict[str, int] = {}

        def _counting_fetch(pilot_path: str, timeout: int):
            call_counts[pilot_path] = call_counts.get(pilot_path, 0) + 1
            return _SAMPLE_PROCESSES_SOURCE if "processes" in pilot_path else _SAMPLE_PSUTILS_SOURCE, ""

        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_counting_fetch,
        ):
            fetch_and_analyse_pilot_source(
                job_id=5,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="",
            )

        for path, count in call_counts.items():
            assert count == 1, f"{path} was fetched {count} times, expected 1"


# ---------------------------------------------------------------------------
# Tool call interface (async)
# ---------------------------------------------------------------------------

class TestPilotSourceAnalysisToolCall:
    @pytest.mark.asyncio
    async def test_missing_job_id(self) -> None:
        from askpanda_atlas.pilot_source_analysis_impl import pilot_source_analysis_tool
        result = await pilot_source_analysis_tool.call({"log_excerpt": "some log"})
        data = json.loads(result[0]["text"])
        assert "error" in data["evidence"]

    @pytest.mark.asyncio
    async def test_missing_log_excerpt(self) -> None:
        from askpanda_atlas.pilot_source_analysis_impl import pilot_source_analysis_tool
        result = await pilot_source_analysis_tool.call({"job_id": 123})
        data = json.loads(result[0]["text"])
        assert "error" in data["evidence"]

    @pytest.mark.asyncio
    async def test_returns_mcp_content_structure(self) -> None:
        from askpanda_atlas.pilot_source_analysis_impl import pilot_source_analysis_tool
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            return_value=(_SAMPLE_PSUTILS_SOURCE, ""),
        ):
            result = await pilot_source_analysis_tool.call({
                "job_id": 7099503721,
                "log_excerpt": _SAMPLE_LOG_EXCERPT,
                "pilot_error_diag": "getpwuid error",
            })

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        data = json.loads(result[0]["text"])
        assert "evidence" in data
        assert "text" in data
