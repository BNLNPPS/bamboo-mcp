"""Tests for pilot_source_analysis tool (askpanda_epic plugin).

The ePIC plugin delegates to askpanda_atlas.pilot_source_analysis_impl.
These tests verify the delegation is wired correctly and that the tool
behaves identically for ePIC jobs, which use the same pilot3 codebase.

All external HTTP calls are patched; no network access is required.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from askpanda_epic.pilot_source_analysis import (
    extract_function_source,
    fetch_and_analyse_pilot_source,
    parse_exception_line,
    parse_traceback_frames,
    pilot_source_analysis_tool,
)


# ---------------------------------------------------------------------------
# Sample data — same traceback shape as ATLAS; pilot3 is experiment-agnostic
# ---------------------------------------------------------------------------

_SAMPLE_LOG_EXCERPT = """\
2026-04-17 02:05:23,070 | WARNING  | Exception caught: 'getpwuid(): uid not found: 6435'
2026-04-17 02:05:23,077 | WARNING  | Traceback (most recent call last):
  File "/tmp/epic_abc123/pilot3/pilot/util/monitoring.py", line 193, in set_cpu_consumption_time
    cpuconsumptiontime = get_current_cpu_consumption_time(job.pid)
  File "/tmp/epic_abc123/pilot3/pilot/util/processes.py", line 619, in get_current_cpu_consumption_time
    ps_cache = get_ps_cache()
  File "/tmp/epic_abc123/pilot3/pilot/util/psutils.py", line 428, in list_processes_and_threads
    current_user = getpass.getuser()
KeyError: 'getpwuid(): uid not found: 6435'
"""

_SAMPLE_PSUTILS_SOURCE = """\
import getpass
import os

def list_processes_and_threads():
    \"\"\"List processes on the worker node.\"\"\"
    try:
        current_user = getpass.getuser()
    except KeyError:
        current_user = str(os.getuid())
    return []
"""


# ---------------------------------------------------------------------------
# Delegation smoke tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_raw_github_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail every raw GitHub fetch unless a test patches _fetch_raw itself.

    resolve_source_ref probes candidate refs through _fetch_raw and keeps the
    response to seed the source cache, so patching only fetch_pilot_module leaves
    the probe reaching the real raw.githubusercontent.com. That would make these
    tests network-dependent and able to pass for the wrong reason, contradicting
    this module's "no network access is required" contract.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "askpanda_atlas.pilot_source_analysis_impl._fetch_raw",
        lambda url, timeout: (0, None),
    )


class TestEpicPilotSourceDelegation:
    """Verify ePIC re-export delegates correctly to the atlas implementation."""

    def test_parse_traceback_frames_works(self) -> None:
        frames = parse_traceback_frames(_SAMPLE_LOG_EXCERPT)
        paths = [f["pilot_path"] for f in frames]
        assert "pilot/util/psutils.py" in paths
        assert "pilot/util/processes.py" in paths
        assert "pilot/util/monitoring.py" in paths

    def test_excludes_non_pilot_frames(self) -> None:
        frames = parse_traceback_frames(_SAMPLE_LOG_EXCERPT)
        for f in frames:
            assert f["pilot_path"].startswith("pilot/")

    def test_parse_exception_line_works(self) -> None:
        exc = parse_exception_line(_SAMPLE_LOG_EXCERPT)
        assert "getpwuid" in exc
        assert "uid not found" in exc

    def test_extract_function_source_works(self) -> None:
        src = extract_function_source(_SAMPLE_PSUTILS_SOURCE, "list_processes_and_threads")
        assert src is not None
        assert "getpass.getuser()" in src

    def test_fetch_and_analyse_happy_path(self) -> None:
        source_map = {
            "pilot/util/psutils.py": _SAMPLE_PSUTILS_SOURCE,
            "pilot/util/processes.py": "def get_ps_cache(): pass\ndef get_current_cpu_consumption_time(pid): pass\n",
            "pilot/util/monitoring.py": "def set_cpu_consumption_time(job): pass\n",
        }

        def _fetch(
            pilot_path: str,
            timeout: int,
            ref: str = "master",
            repo: str = "PanDAWMS/pilot3",
        ) -> tuple[str | None, str]:
            """Serve source from source_map, ignoring the ref and repo.

            Args:
                pilot_path: Repo-relative module path being fetched.
                timeout: Unused.
                ref: Unused; which ref would have been fetched is covered by the
                    ATLAS resolve_source_ref tests.
                repo: Unused, for the same reason.

            Returns:
                Tuple of (source or None, error message).
            """
            if pilot_path in source_map:
                return source_map[pilot_path], ""
            return None, f"404 {pilot_path}"

        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_fetch,
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=9999001,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="Exception caught: 'getpwuid(): uid not found: 6435'",
            )

        evidence = result["evidence"]
        assert evidence["job_id"] == 9999001
        assert "getpwuid" in evidence["exception"]
        assert "pilot/util/psutils.py::list_processes_and_threads" in evidence["source_snippets"]


# ---------------------------------------------------------------------------
# Tool call interface
# ---------------------------------------------------------------------------

class TestEpicPilotSourceToolCall:

    @pytest.mark.asyncio
    async def test_missing_log_excerpt_returns_error(self) -> None:
        result = await pilot_source_analysis_tool.call({"job_id": 9999001})
        data = json.loads(result[0]["text"])
        assert "error" in data["evidence"]

    @pytest.mark.asyncio
    async def test_returns_mcp_content_structure(self) -> None:
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            return_value=(_SAMPLE_PSUTILS_SOURCE, ""),
        ):
            result = await pilot_source_analysis_tool.call({
                "job_id": 9999001,
                "log_excerpt": _SAMPLE_LOG_EXCERPT,
                "pilot_error_diag": "getpwuid error",
            })

        assert isinstance(result, list) and len(result) == 1
        assert result[0]["type"] == "text"
        data = json.loads(result[0]["text"])
        assert "evidence" in data
        assert "text" in data

    @pytest.mark.asyncio
    async def test_epic_job_id_preserved_in_evidence(self) -> None:
        """ePIC job ID is passed through to evidence correctly."""
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            return_value=(None, "HTTP 404"),
        ):
            result = await pilot_source_analysis_tool.call({
                "job_id": 9999001,
                "log_excerpt": _SAMPLE_LOG_EXCERPT,
            })
        data = json.loads(result[0]["text"])
        assert data["evidence"]["job_id"] == 9999001


# ---------------------------------------------------------------------------
# Entry point registration
# ---------------------------------------------------------------------------

class TestEpicEntryPointRegistration:
    """Verify the entry point is registered and resolvable."""

    def test_tool_has_get_definition(self) -> None:
        defn = pilot_source_analysis_tool.get_definition()
        assert defn["name"] == "pilot_source_analysis"
        assert "job_id" in defn["inputSchema"]["properties"]
        assert "log_excerpt" in defn["inputSchema"]["properties"]

    def test_tool_tags_include_pilot3(self) -> None:
        defn = pilot_source_analysis_tool.get_definition()
        assert "pilot3" in defn.get("tags", [])
