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

@pytest.fixture(autouse=True)
def _no_raw_github_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail every raw GitHub fetch unless a test patches _fetch_raw itself.

    resolve_source_ref probes candidate refs through _fetch_raw and now keeps the
    response to seed the source cache, so a test that patches only
    fetch_pilot_module would otherwise reach the real raw.githubusercontent.com
    and could pass for the wrong reason (or fail on a host without outbound
    internet). This fixture makes the default "no network"; tests that need a
    successful probe patch _fetch_raw explicitly, which takes precedence.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "askpanda_atlas.pilot_source_analysis_impl._fetch_raw",
        lambda url, timeout: (0, None),
    )


def _make_fetch_side_effect(source_map: dict[str, str]):
    """Return a side_effect for fetch_pilot_module keyed by pilot_path.

    Accepts the ``ref`` and ``repo`` arguments added when source selection
    became release/development aware.  Both are ignored: these tests supply the
    source directly, so which repo and ref would have been fetched is irrelevant
    to them and is covered separately by the resolve_source_ref tests.
    """
    def _fetch(
        pilot_path: str,
        timeout: int,
        ref: str = "master",
        repo: str = "PanDAWMS/pilot3",
    ):
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

        def _counting_fetch(
            pilot_path: str,
            timeout: int,
            ref: str = "master",
            repo: str = "PanDAWMS/pilot3",
        ):
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


# ---------------------------------------------------------------------------
# GitHub ref resolution (PILOTVERSION pinning)
#
# Pilot releases are tagged after release (e.g. tag 3.14.0.22), so traceback
# line numbers are only meaningful against the tag the job actually ran.
# Fetching master instead silently misreports line numbers for any module that
# has changed since — which for an actively developed file is most of the time.
#
# resolve_source_ref probes candidate refs through _fetch_raw, so these tests
# patch _fetch_raw rather than fetch_pilot_module.
# ---------------------------------------------------------------------------

class TestResolveSourceRef:
    """Release and development source selection.

    Released and unreleased pilots live in different places and the two paths
    are mutually exclusive: a tagged version reads the tag and never the
    development branch; an untagged version reads the development branch and
    never master. master is used only when the version is unknown.
    """

    def test_pins_to_bare_version_tag(self) -> None:
        """A version whose bare tag exists is pinned to that tag."""
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        seen: list[str] = []

        def _fetch(url: str, timeout: int):
            seen.append(url)
            return (200, "source") if "/3.14.0.22/" in url else (404, None)

        with patch("askpanda_atlas.pilot_source_analysis_impl._fetch_raw", _fetch):
            ref = resolve_source_ref("3.14.0.22", "pilot/util/https.py", 5)

        assert ref.repo == "PanDAWMS/pilot3"
        assert ref.ref == "3.14.0.22"
        assert ref.kind == "release_tag"
        assert ref.reachable is True
        assert len(seen) == 1, "The bare tag must be tried first."

    def test_falls_back_to_v_prefixed_tag(self) -> None:
        """When only the v-prefixed tag exists it is used."""
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        def _fetch(url: str, timeout: int):
            return (200, "source") if "/v3.9.1/" in url else (404, None)

        with patch("askpanda_atlas.pilot_source_analysis_impl._fetch_raw", _fetch):
            ref = resolve_source_ref("3.9.1", "pilot/util/a.py", 5)

        assert ref.ref == "v3.9.1"
        assert ref.kind == "release_tag"

    def test_release_tag_never_reads_development_branch(self) -> None:
        """A tagged version must not probe the development fork at all."""
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        seen: list[str] = []

        def _fetch(url: str, timeout: int):
            seen.append(url)
            return (200, "source")

        with patch("askpanda_atlas.pilot_source_analysis_impl._fetch_raw", _fetch):
            resolve_source_ref("3.14.0.22", "pilot/util/a.py", 5)

        assert not any("PalNilsson" in url or "/next/" in url for url in seen), (
            "A released version ran tagged code; the development branch is "
            "not a valid source for it."
        )

    def test_untagged_version_reads_development_branch(self) -> None:
        """An unreleased version resolves to the development fork's next branch.

        Job 7261310898 ran pilot 3.14.1.27, which has no release tag. master
        carries released code, so it is not what that job ran; the unreleased
        source lives on the development branch.
        """
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        def _fetch(url: str, timeout: int):
            return (200, "dev source") if "PalNilsson" in url else (404, None)

        with patch("askpanda_atlas.pilot_source_analysis_impl._fetch_raw", _fetch):
            ref = resolve_source_ref("3.14.1.27", "pilot/util/https.py", 5)

        assert ref.repo == "PalNilsson/pilot3"
        assert ref.ref == "next"
        assert ref.kind == "development_branch"
        assert "unreleased" in ref.resolution
        assert "indicative only" in ref.resolution

    def test_untagged_version_never_falls_back_to_master(self) -> None:
        """No candidate for an untagged version may be master."""
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        seen: list[str] = []

        def _fetch(url: str, timeout: int):
            seen.append(url)
            return (404, None)

        with patch("askpanda_atlas.pilot_source_analysis_impl._fetch_raw", _fetch):
            ref = resolve_source_ref("3.14.1.27", "pilot/util/a.py", 5)

        assert not any("/master/" in url for url in seen)
        assert ref.ref != "master"
        assert ref.kind == "development_branch"

    def test_all_candidates_unreachable_is_reported(self) -> None:
        """When nothing is reachable the choice is kept but flagged."""
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        with patch(
            "askpanda_atlas.pilot_source_analysis_impl._fetch_raw",
            lambda url, timeout: (404, None),
        ):
            ref = resolve_source_ref("3.14.1.27", "pilot/util/a.py", 5)

        assert ref.reachable is False
        assert ref.probe_text is None
        assert "No candidate ref was reachable" in ref.resolution

    def test_unknown_version_uses_release_branch(self) -> None:
        """An unknown version reads master, never the development branch.

        The build cannot be classified as released or unreleased, so reaching
        into the development fork on a guess would be wrong.
        """
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        seen: list[str] = []

        def _fetch(url: str, timeout: int):
            seen.append(url)
            return (200, "source")

        with patch("askpanda_atlas.pilot_source_analysis_impl._fetch_raw", _fetch):
            ref = resolve_source_ref("", "pilot/util/a.py", 5)

        assert ref.repo == "PanDAWMS/pilot3"
        assert ref.ref == "master"
        assert ref.kind == "unknown_version"
        assert not any("PalNilsson" in url for url in seen)

    def test_env_overrides_development_location(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The development repo and branch are overridable by environment.

        The fork is expected to move to a BNLNPPS organisation; the env vars mean
        that will not need a code change.
        """
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        monkeypatch.setenv("BAMBOO_PILOT3_DEV_REPO", "BNLNPPS/pilot3")
        monkeypatch.setenv("BAMBOO_PILOT3_DEV_BRANCH", "dev")

        def _fetch(url: str, timeout: int):
            return (200, "source") if "BNLNPPS" in url else (404, None)

        with patch("askpanda_atlas.pilot_source_analysis_impl._fetch_raw", _fetch):
            ref = resolve_source_ref("9.9.9.9", "pilot/util/a.py", 5)

        assert ref.repo == "BNLNPPS/pilot3"
        assert ref.ref == "dev"

    def test_probe_text_is_returned_for_cache_seeding(self) -> None:
        """The successful probe response is kept so the file is not refetched."""
        from askpanda_atlas.pilot_source_analysis_impl import resolve_source_ref

        with patch(
            "askpanda_atlas.pilot_source_analysis_impl._fetch_raw",
            lambda url, timeout: (200, "probed source"),
        ):
            ref = resolve_source_ref("3.14.0.22", "pilot/util/a.py", 5)

        assert ref.probe_text == "probed source"


class TestSourceSelectionEndToEnd:
    def test_github_urls_use_resolved_repo_and_ref(self) -> None:
        """Browse links point at the pinned tag in the release repo."""
        source_map = {
            "pilot/util/psutils.py": _SAMPLE_PSUTILS_SOURCE,
            "pilot/util/processes.py": _SAMPLE_PROCESSES_SOURCE,
            "pilot/util/monitoring.py": _SAMPLE_MONITORING_SOURCE,
        }
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_make_fetch_side_effect(source_map),
        ), patch(
            "askpanda_atlas.pilot_source_analysis_impl._fetch_raw",
            lambda url, timeout: (200, _SAMPLE_MONITORING_SOURCE),
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=7099503721,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="",
                pilot_version="3.14.0.22",
            )

        evidence = result["evidence"]
        assert evidence["github_repo"] == "PanDAWMS/pilot3"
        assert evidence["github_ref"] == "3.14.0.22"
        assert evidence["ref_kind"] == "release_tag"
        assert evidence["pilot_version"] == "3.14.0.22"
        for url in evidence["github_urls"].values():
            assert "/PanDAWMS/pilot3/blob/3.14.0.22/" in url
            assert "/blob/master/" not in url

    def test_unreleased_version_reports_development_source(self) -> None:
        """An untagged version surfaces the dev branch and its caveat."""
        source_map = {
            "pilot/util/psutils.py": _SAMPLE_PSUTILS_SOURCE,
            "pilot/util/processes.py": _SAMPLE_PROCESSES_SOURCE,
            "pilot/util/monitoring.py": _SAMPLE_MONITORING_SOURCE,
        }
        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_make_fetch_side_effect(source_map),
        ), patch(
            "askpanda_atlas.pilot_source_analysis_impl._fetch_raw",
            lambda url, timeout: (
                (200, _SAMPLE_MONITORING_SOURCE) if "PalNilsson" in url
                else (404, None)
            ),
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=7261310898,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="",
                pilot_version="3.14.1.27",
            )

        evidence = result["evidence"]
        assert evidence["github_repo"] == "PalNilsson/pilot3"
        assert evidence["github_ref"] == "next"
        assert evidence["ref_kind"] == "development_branch"
        assert "unreleased build" in result["text"]
        assert "indicative only" in result["text"]
        for url in evidence["github_urls"].values():
            assert "/PalNilsson/pilot3/blob/next/" in url

    def test_probe_avoids_a_duplicate_fetch(self) -> None:
        """The probed module is not downloaded a second time."""
        calls: list[str] = []

        def _counting_fetch(
            pilot_path: str,
            timeout: int,
            ref: str = "master",
            repo: str = "PanDAWMS/pilot3",
        ):
            calls.append(pilot_path)
            return _SAMPLE_PSUTILS_SOURCE, ""

        with patch(
            "askpanda_atlas.pilot_source_analysis_impl.fetch_pilot_module",
            side_effect=_counting_fetch,
        ), patch(
            "askpanda_atlas.pilot_source_analysis_impl._fetch_raw",
            lambda url, timeout: (200, _SAMPLE_MONITORING_SOURCE),
        ):
            result = fetch_and_analyse_pilot_source(
                job_id=1,
                log_excerpt=_SAMPLE_LOG_EXCERPT,
                pilot_error_diag="",
                pilot_version="3.14.0.22",
            )

        probed = result["evidence"]["traceback_frames"][0]["pilot_path"]
        assert probed not in calls, (
            "The probe already downloaded this module; fetching it again wastes "
            "a request."
        )


# ---------------------------------------------------------------------------
# Line-number verification / version skew
# ---------------------------------------------------------------------------

_TWO_FUNC_SOURCE = textwrap.dedent('''\
    """Module docstring."""


    def alpha():
        """First function."""
        return 1


    def beta():
        """Second function."""
        return 2
    ''')


class TestFunctionAtLine:
    def test_resolves_enclosing_function(self) -> None:
        """A line inside a function resolves to that function's name."""
        from askpanda_atlas.pilot_source_analysis_impl import function_at_line

        assert function_at_line(_TWO_FUNC_SOURCE, 6) == "alpha"
        assert function_at_line(_TWO_FUNC_SOURCE, 11) == "beta"

    def test_returns_none_outside_any_function(self) -> None:
        """A module-level line belongs to no function."""
        from askpanda_atlas.pilot_source_analysis_impl import function_at_line

        assert function_at_line(_TWO_FUNC_SOURCE, 1) is None

    def test_returns_none_for_unparseable_source(self) -> None:
        """Unparseable source yields None rather than raising."""
        from askpanda_atlas.pilot_source_analysis_impl import function_at_line

        assert function_at_line("def broken(:\n", 1) is None

    def test_prefers_innermost_nested_function(self) -> None:
        """A nested helper wins over its enclosing function."""
        from askpanda_atlas.pilot_source_analysis_impl import function_at_line

        source = textwrap.dedent('''\
            def outer():
                def inner():
                    return 1
                return inner
            ''')
        assert function_at_line(source, 3) == "inner"


class TestVerifyFrameLines:
    def test_matching_lines_report_no_skew(self) -> None:
        """Frames whose line numbers land on the named function verify clean."""
        from askpanda_atlas.pilot_source_analysis_impl import verify_frame_lines

        frames = [{"pilot_path": "pilot/util/a.py", "func": "alpha", "lineno": 6}]
        result = verify_frame_lines(frames, {"pilot/util/a.py": _TWO_FUNC_SOURCE})
        assert result["checked"] == 1
        assert result["version_skew"] is False
        assert result["mismatches"] == []

    def test_mismatched_line_flags_version_skew(self) -> None:
        """A line that lands on a different function flags skew.

        This is what a master fallback looks like when the module has shifted:
        the function names are still there but the line numbers have moved.
        """
        from askpanda_atlas.pilot_source_analysis_impl import verify_frame_lines

        frames = [{"pilot_path": "pilot/util/a.py", "func": "alpha", "lineno": 11}]
        result = verify_frame_lines(frames, {"pilot/util/a.py": _TWO_FUNC_SOURCE})
        assert result["version_skew"] is True
        assert result["mismatches"][0]["expected_func"] == "alpha"
        assert result["mismatches"][0]["found_func"] == "beta"

    def test_unfetched_file_is_skipped(self) -> None:
        """Frames whose module could not be fetched are not counted as skew."""
        from askpanda_atlas.pilot_source_analysis_impl import verify_frame_lines

        frames = [{"pilot_path": "pilot/util/a.py", "func": "alpha", "lineno": 6}]
        result = verify_frame_lines(frames, {"pilot/util/a.py": None})
        assert result["checked"] == 0
        assert result["version_skew"] is False


class TestFramesCarryLineNumbers:
    def test_parse_traceback_frames_includes_lineno(self) -> None:
        """Frames expose line numbers so they can be verified against source."""
        frames = parse_traceback_frames(_SAMPLE_LOG_EXCERPT)
        assert frames
        for frame in frames:
            assert isinstance(frame["lineno"], int)
            assert frame["lineno"] > 0

    def test_parse_exception_line_returns_type_and_message(self) -> None:
        """The exception line is returned as 'Type: message'."""
        result = parse_exception_line(
            'Traceback (most recent call last):\n'
            '  File "/tmp/p/pilot3/pilot/util/a.py", line 1, in f\n'
            "    x()\n"
            "TimeoutError: timed out\n"
        )
        assert result == "TimeoutError: timed out"
