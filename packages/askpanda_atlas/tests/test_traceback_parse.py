"""Tests for askpanda_atlas._traceback_parse.

These cover the format invariants the traceback-first extractor depends on:
pilot log record structure, traceback block boundaries, frame and exception
parsing, pilot-vs-stdlib frame discrimination, pilot version detection and
budget-aware truncation.
"""
from __future__ import annotations

from askpanda_atlas._traceback_parse import (
    ExceptionInfo,
    find_primary_exception,
    find_traceback_blocks,
    parse_exception,
    parse_frames,
    parse_pilot_version,
    parse_pilot_version_from_pilotid,
    record_level,
    select_primary_traceback,
    truncate_traceback,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Real shape of the job 7261310898 failure: the CRITICAL record carries both the
# pilot's own message and the start of the traceback on one physical line, and
# the traceback body follows as unprefixed continuation lines.  Frames alternate
# between pilot3 code and the CVMFS standard library.
_PILOT_TRACEBACK: str = (
    "2026-08-17 08:38:24,986 | CRITICAL | pilot.control.payload            | "
    "execute_payloads          | execute payloads caught an exception "
    "(cannot recover): timed out, Traceback (most recent call last):\n"
    '  File "/tmp/atlas_QCSsk3r1/pilot3/pilot/control/payload.py", line 308, '
    "in execute_payloads\n"
    "    exit_code, diagnostics = payload_executor.run()\n"
    "                             ~~~~~~~~~~~~~~~~~~~~^^\n"
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

_CHAINED_TRACEBACK: str = (
    "2026-08-17 09:00:00,000 | ERROR    | pilot.x | f | boom, "
    "Traceback (most recent call last):\n"
    '  File "/tmp/p/pilot3/pilot/util/a.py", line 10, in inner\n'
    "    raise KeyError('k')\n"
    "KeyError: 'k'\n"
    "\n"
    "During handling of the above exception, another exception occurred:\n"
    "\n"
    "Traceback (most recent call last):\n"
    '  File "/tmp/p/pilot3/pilot/util/b.py", line 20, in outer\n'
    "    cleanup()\n"
    "RuntimeError: cleanup failed\n"
)


# ---------------------------------------------------------------------------
# record_level
# ---------------------------------------------------------------------------

def test_record_level_parses_pilot_prefix() -> None:
    """A timestamped pilot record line yields its log level."""
    line = "2026-08-17 08:38:24,986 | CRITICAL | pilot.control.payload | f | msg"
    assert record_level(line) == "CRITICAL"


def test_record_level_accepts_dot_subsecond_separator() -> None:
    """Both ',' and '.' are valid sub-second separators."""
    line = "2026-08-17 08:38:24.986 | ERROR | pilot.x | f | msg"
    assert record_level(line) == "ERROR"


def test_record_level_empty_for_continuation_line() -> None:
    """Traceback body lines carry no record prefix, so they have no level.

    This is the property that makes a traceback a *continuation* of one record
    rather than a series of records, and is why line-count windows are the wrong
    extraction unit.
    """
    assert record_level('  File "/tmp/x.py", line 1, in f') == ""
    assert record_level("TimeoutError: timed out") == ""


# ---------------------------------------------------------------------------
# find_traceback_blocks
# ---------------------------------------------------------------------------

def test_find_traceback_blocks_captures_whole_block() -> None:
    """The block spans from the marker line to the terminal exception line."""
    log = "INFO | before\n" + _PILOT_TRACEBACK + "INFO | after\n"
    blocks = find_traceback_blocks(log)
    assert len(blocks) == 1
    block = blocks[0]
    assert "Traceback (most recent call last):" in block.text
    assert block.text.rstrip().endswith("TimeoutError: timed out")
    assert "before" not in block.text
    assert "after" not in block.text


def test_find_traceback_blocks_captures_level_from_prefix() -> None:
    """The block records the level of the log record containing the marker."""
    blocks = find_traceback_blocks(_PILOT_TRACEBACK)
    assert blocks[0].level == "CRITICAL"


def test_find_traceback_blocks_unlevelled_payload_log() -> None:
    """Tracebacks in payload.stdout have no record prefix but are still found."""
    log = (
        "Py:Athena INFO initialising\n"
        "Traceback (most recent call last):\n"
        '  File "/x/run.py", line 3, in <module>\n'
        "    main()\n"
        "ValueError: bad config\n"
    )
    blocks = find_traceback_blocks(log)
    assert len(blocks) == 1
    assert blocks[0].level == ""
    assert "ValueError: bad config" in blocks[0].text


def test_find_traceback_blocks_chained_exception_is_one_block() -> None:
    """A 'During handling...' chain stays in a single block.

    Splitting at the first exception line would report KeyError as the failure
    when RuntimeError is what actually propagated.
    """
    blocks = find_traceback_blocks(_CHAINED_TRACEBACK)
    assert len(blocks) == 1
    assert "KeyError" in blocks[0].text
    assert "RuntimeError: cleanup failed" in blocks[0].text


def test_find_traceback_blocks_returns_empty_without_traceback() -> None:
    """A log with no traceback yields no blocks."""
    log = "2026-08-17 08:00:00,000 | INFO | pilot.x | f | all good\n" * 20
    assert find_traceback_blocks(log) == []


def test_find_traceback_blocks_truncated_traceback() -> None:
    """A traceback cut off mid-frame still yields a block with its frames."""
    log = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/a.py", line 5, in f\n'
    )
    blocks = find_traceback_blocks(log)
    assert len(blocks) == 1
    assert parse_frames(blocks[0].text)


# ---------------------------------------------------------------------------
# select_primary_traceback
# ---------------------------------------------------------------------------

def test_select_primary_traceback_prefers_higher_severity() -> None:
    """A CRITICAL traceback wins over an earlier WARNING one.

    Retried operations log at WARNING; the fatal one the pilot cannot recover
    from is CRITICAL.
    """
    warning = (
        "2026-08-17 08:00:00,000 | WARNING  | pilot.x | f | retrying, "
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/a.py", line 1, in f\n'
        "    x()\n"
        "OSError: transient\n"
    )
    log = warning + _PILOT_TRACEBACK
    blocks = find_traceback_blocks(log)
    assert len(blocks) == 2
    chosen = select_primary_traceback(blocks)
    assert chosen is not None
    assert chosen.level == "CRITICAL"
    assert "TimeoutError" in chosen.text


def test_select_primary_traceback_prefers_last_of_equal_severity() -> None:
    """Among equal-severity tracebacks the last one wins."""
    def _tb(msg: str) -> str:
        return (
            "2026-08-17 08:00:00,000 | ERROR    | pilot.x | f | boom, "
            "Traceback (most recent call last):\n"
            '  File "/tmp/p/pilot3/pilot/util/a.py", line 1, in f\n'
            "    x()\n"
            f"OSError: {msg}\n"
        )

    blocks = find_traceback_blocks(_tb("first") + _tb("second"))
    chosen = select_primary_traceback(blocks)
    assert chosen is not None
    assert "second" in chosen.text


def test_select_primary_traceback_none_when_empty() -> None:
    """Selecting from no blocks yields None rather than raising."""
    assert select_primary_traceback([]) is None


# ---------------------------------------------------------------------------
# parse_frames / parse_exception
# ---------------------------------------------------------------------------

def test_parse_frames_captures_line_numbers() -> None:
    """Frame line numbers are captured so they can be verified against source."""
    frames = parse_frames(_PILOT_TRACEBACK)
    https_frames = [f for f in frames if f.pilot_path == "pilot/util/https.py"]
    assert len(https_frames) == 1
    assert https_frames[0].lineno == 2301
    assert https_frames[0].func == "download_file"


def test_parse_frames_discriminates_pilot_from_stdlib() -> None:
    """CVMFS standard library frames are kept but not marked as pilot frames.

    They matter for diagnosis (a timeout inside socket.recv_into means the HTTP
    peer stopped responding) but must never be fetched from the pilot3 repo.
    """
    frames = parse_frames(_PILOT_TRACEBACK)
    pilot = [f for f in frames if f.is_pilot]
    stdlib = [f for f in frames if not f.is_pilot]
    assert len(pilot) == 3
    assert len(stdlib) == 1
    assert stdlib[0].func == "readinto"
    assert stdlib[0].pilot_path == ""


def test_parse_frames_does_not_match_pilot3_directory() -> None:
    """The 'pilot3/' scratch directory is not mistaken for the 'pilot/' package."""
    frames = parse_frames(_PILOT_TRACEBACK)
    for frame in frames:
        if frame.is_pilot:
            assert frame.pilot_path.startswith("pilot/")
            assert "pilot3" not in frame.pilot_path


def test_parse_exception_extracts_type_and_message() -> None:
    """The terminal exception line is split into type and message."""
    info = parse_exception(_PILOT_TRACEBACK, "CRITICAL")
    assert info.exc_type == "TimeoutError"
    assert info.message == "timed out"
    assert info.level == "CRITICAL"


def test_parse_exception_deepest_pilot_frame_is_innermost() -> None:
    """deepest_pilot_frame is the innermost pilot frame, not the outermost.

    The outermost frame (execute_payloads) is generic pilot plumbing; the
    innermost (download_file) is the code that actually failed.
    """
    info = parse_exception(_PILOT_TRACEBACK)
    deepest = info.deepest_pilot_frame
    assert deepest is not None
    assert deepest.pilot_path == "pilot/util/https.py"
    assert deepest.lineno == 2301
    assert deepest.func == "download_file"


def test_parse_exception_chained_reports_final_exception() -> None:
    """For a chained traceback the exception that propagated is reported."""
    info = parse_exception(_CHAINED_TRACEBACK)
    assert info.exc_type == "RuntimeError"
    assert info.message == "cleanup failed"


def test_parse_exception_dotted_type_keeps_both_forms() -> None:
    """A dotted custom exception keeps the short name and the full path."""
    log = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/a.py", line 1, in f\n'
        "    x()\n"
        "pilot.common.exception.StageInFailure: no replica\n"
    )
    info = parse_exception(log)
    assert info.exc_type == "StageInFailure"
    assert info.exc_type_full == "pilot.common.exception.StageInFailure"
    assert info.message == "no replica"


def test_parse_exception_ignores_colons_in_source_lines() -> None:
    """Indented source lines containing colons are not read as exception lines."""
    log = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/a.py", line 1, in f\n'
        '    config = {"key": "value", "other": 1}\n'
        "TypeError: unhashable\n"
    )
    info = parse_exception(log)
    assert info.exc_type == "TypeError"


def test_parse_exception_bare_exception_line() -> None:
    """An exception printed with no message is still recognised."""
    log = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/a.py", line 1, in f\n'
        "    x()\n"
        "MemoryError\n"
    )
    info = parse_exception(log)
    assert info.exc_type == "MemoryError"
    assert info.message == ""


def test_parse_exception_no_pilot_frames_gives_none_deepest() -> None:
    """A pure payload traceback has no pilot frame to analyse."""
    log = (
        "Traceback (most recent call last):\n"
        '  File "/x/analysis.py", line 3, in <module>\n'
        "    main()\n"
        "ValueError: bad\n"
    )
    info = parse_exception(log)
    assert info.frames
    assert info.deepest_pilot_frame is None


def test_exception_info_as_dict_shape() -> None:
    """as_dict produces the keys the evidence builder relies on."""
    info = parse_exception(_PILOT_TRACEBACK, "CRITICAL")
    data = info.as_dict()
    assert set(data) == {
        "type", "type_full", "message", "level", "frames", "deepest_pilot_frame",
    }
    assert data["deepest_pilot_frame"]["is_pilot"] is True


def test_empty_exception_info_is_safe() -> None:
    """A default ExceptionInfo does not raise on its properties."""
    info = ExceptionInfo()
    assert info.pilot_frames == []
    assert info.deepest_pilot_frame is None


# ---------------------------------------------------------------------------
# find_primary_exception
# ---------------------------------------------------------------------------

def test_find_primary_exception_end_to_end() -> None:
    """The convenience wrapper returns exception, block and block count."""
    log = "INFO | noise\n" * 10 + _PILOT_TRACEBACK
    info, block, count = find_primary_exception(log)
    assert info is not None and block is not None
    assert info.exc_type == "TimeoutError"
    assert count == 1


def test_find_primary_exception_none_without_traceback() -> None:
    """Logs with no traceback report no exception and a zero count."""
    info, block, count = find_primary_exception("nothing to see here\n")
    assert info is None
    assert block is None
    assert count == 0


def test_find_primary_exception_reports_discarded_alternatives() -> None:
    """The block count exposes that other tracebacks were passed over."""
    warning = (
        "2026-08-17 08:00:00,000 | WARNING  | pilot.x | f | retry, "
        "Traceback (most recent call last):\n"
        '  File "/tmp/p/pilot3/pilot/util/a.py", line 1, in f\n'
        "    x()\n"
        "OSError: transient\n"
    )
    _info, _block, count = find_primary_exception(warning + _PILOT_TRACEBACK)
    assert count == 2


# ---------------------------------------------------------------------------
# Pilot version detection
# ---------------------------------------------------------------------------

def test_parse_pilot_version_from_log() -> None:
    """The version logged at pilot start-up is extracted."""
    log = (
        "2026-08-17 08:36:41,102 | INFO     | pilot | main | "
        "pilot version: 3.14.0.22\n"
    )
    assert parse_pilot_version(log) == "3.14.0.22"


def test_parse_pilot_version_banner_form() -> None:
    """The '*** PanDA Pilot version X ***' banner form is also recognised."""
    assert parse_pilot_version("*** PanDA Pilot version 3.10.3.31 ***") == "3.10.3.31"


def test_parse_pilot_version_absent() -> None:
    """A log without a version line yields an empty string, not a guess."""
    assert parse_pilot_version("no version anywhere here\n") == ""


def test_parse_pilot_version_from_pilotid_prefers_four_components() -> None:
    """The four-component release version is preferred over shorter matches."""
    pilotid = "https://example.cern.ch/log.tgz|PR|3.14.0.22"
    assert parse_pilot_version_from_pilotid(pilotid) == "3.14.0.22"


def test_parse_pilot_version_from_pilotid_empty() -> None:
    """A missing pilotid yields an empty string."""
    assert parse_pilot_version_from_pilotid("") == ""


# ---------------------------------------------------------------------------
# truncate_traceback
# ---------------------------------------------------------------------------

def test_truncate_traceback_keeps_short_text_intact() -> None:
    """A traceback within budget is returned unchanged."""
    assert truncate_traceback(_PILOT_TRACEBACK, 100000) == _PILOT_TRACEBACK


def test_truncate_traceback_preserves_exception_line() -> None:
    """Truncation keeps the terminal exception line.

    This is the whole reason truncate_traceback exists: a plain text[:n] would
    discard the single most diagnostic line in the traceback.
    """
    long_tb = (
        "Traceback (most recent call last):\n"
        + ''.join(
            f'  File "/tmp/p/pilot3/pilot/util/m{i}.py", line {i}, in f{i}\n'
            f"    call_{i}()\n"
            for i in range(400)
        )
        + "TimeoutError: timed out\n"
    )
    budget = 2000
    result = truncate_traceback(long_tb, budget)
    assert len(result) <= budget
    assert "TimeoutError: timed out" in result
    assert result.startswith("Traceback (most recent call last):")
    assert "truncated by Bamboo" in result


def test_truncate_traceback_zero_budget() -> None:
    """A zero or negative budget yields an empty string rather than raising."""
    assert truncate_traceback(_PILOT_TRACEBACK, 0) == ""
    assert truncate_traceback(_PILOT_TRACEBACK, -5) == ""
