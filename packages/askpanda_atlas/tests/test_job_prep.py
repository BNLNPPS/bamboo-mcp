"""Tests for the core-dump acquisition layer (``askpanda_atlas._job_prep``).

No network access: an autouse fixture makes bare ``requests.get``/``requests.head``
raise, so any code path that reaches the real HTTP layer fails loudly rather
than falling through to a 403 and quietly succeeding for the wrong reason.

The listing fixture reproduces the structure of a real looping-job tarball:
paths carried in ``dirname`` rather than in ``name``, the same basename under
several directories, an unpacked release under ``workDir/usr``, and a
zero-length ``payload.stderr``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from askpanda_atlas._job_prep import (  # type: ignore[import]
    DISK_RESERVE_BYTES,
    JobPrepError,
    MAX_LOG_BYTES,
    PreparedJob,
    build_media_root,
    latest_payload_modification,
    media_url,
    parse_http_date,
    parse_listing_mtime,
    preflight_disk,
    prepare_job_dir,
    select_files_for_fetch,
)

BASE_URL = "https://bigpanda.cern.ch"

#: Verified media coordinates for the reference job.
_LOG_GUID = "c2b778b2-602f-452d-b026-702069af45b8"
_MEDIA_ROOT = (
    f"{BASE_URL}/media/filebrowser/{_LOG_GUID}/panda/"
    "tarball_PandaJob_7263525363_CERN"
)

#: Timestamps of the reference job.  The gap between the core and the last
#: payload write is 7774 s, which is the value the round-trip test pins.
_CORE_MTIME = "2026-08-19 08:18:20"
_PAYLOAD_STDOUT_MTIME = "2026-08-19 06:08:46"
_WORKDIR_LOG_MTIME = "2026-08-19 06:07:49"
_STALE_MTIME = "2026-08-19 01:00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any unmocked HTTP call fail loudly.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    import requests  # type: ignore[import]

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"unexpected network access: {args!r} {kwargs!r}")

    monkeypatch.setattr(requests, "get", _forbidden)
    monkeypatch.setattr(requests, "head", _forbidden)


def _entry(
    name: str,
    size: int,
    dirname: str = "",
    modification: str = _WORKDIR_LOG_MTIME,
) -> dict[str, Any]:
    """Build one normalised listing record.

    Args:
        name: Basename.
        size: Size in bytes.
        dirname: Directory component, slash-stripped by the normaliser.
        modification: UTC listing timestamp.

    Returns:
        A record in the shape ``_fetch_file_listing`` returns.
    """
    clean = dirname.strip("/")
    return {
        "relative_path": f"{clean}/{name}" if clean else name,
        "name": name,
        "dirname": clean,
        "size_bytes": size,
        "modification": modification,
    }


def _reference_listing() -> list[dict[str, Any]]:
    """Return the reference looping-job listing.

    Returns:
        Normalised records covering every selection rule: the payload streams,
        the release setup, the core, a recent ``workDir`` log, a stale one, an
        unpacked release, repeated basenames, and root-level files that look
        log-like but are not discovered.
    """
    return [
        _entry("payload.stdout", 454904, "", _PAYLOAD_STDOUT_MTIME),
        _entry("payload.stderr", 0, "", "2026-08-19 01:42:08"),
        _entry("my_release_setup.sh", 225, "", "2026-08-19 01:42:51"),
        _entry("core.18277", 1065033128, "", _CORE_MTIME),
        _entry("pilotlog.txt", 2900000, "", "2026-08-19 08:18:25"),
        # Log-like by name but at the job root, where the analyzer only globs
        # payload* — fetching it would buy a request for an unopened file.
        _entry("remote_open.stderr", 348000, "", "2026-08-19 06:07:50"),
        # Arguably interesting, but not log-like by name and not discovered.
        _entry("memory_monitor_output.txt", 52000, "", "2026-08-19 08:17:23"),
        _entry("setup.sh", 1234, "", "2026-08-19 01:42:50"),
        # The one workDir log that was still being written near the payload's
        # last write: 57 s before payload.stdout fell silent.
        _entry("tmp.stdout.83d9c506-1f2e-4a77-9c31-5b0e2a4d7f10", 337391,
               "/workDir", _WORKDIR_LOG_MTIME),
        _entry("old-reference.log", 4000, "/workDir", _STALE_MTIME),
        _entry("in.txt", 2359, "/workDir", _WORKDIR_LOG_MTIME),
        _entry("output.root", 10865224, "/workDir", _WORKDIR_LOG_MTIME),
        _entry("output.root", 1648, "/workDir/workDir/hist", "2026-08-19 01:43:21"),
        _entry("output.root", 1910, "/workDir/workDir/input", "2026-08-19 01:43:21"),
        _entry("setup.sh", 6624,
               "/workDir/usr/UserAnalysis/1.0.0/InstallArea/x86_64-el9-gcc15-opt",
               "2026-08-19 01:43:02"),
        _entry("CMakeOutput.log", 8000,
               "/workDir/usr/UserAnalysis/CMakeFiles", "2026-08-19 01:43:02"),
    ]


def _metadata() -> dict[str, Any]:
    """Return job metadata carrying the log file entry.

    Returns:
        A response in the shape ``/job?pandaid=...&json`` returns.
    """
    return {
        "job": {"pandaid": 7263525363, "jobstatus": "failed"},
        "files": [
            {"type": "input", "lfn": "in.root", "guid": "aaaa", "scope": "mc23",
             "destinationse": "CERN"},
            {"type": "log", "lfn": "log.tgz", "guid": _LOG_GUID, "scope": "panda",
             "destinationse": "CERN/atlas/dq2"},
        ],
        "dsfiles": [],
    }


def _paths(plan: Any) -> set[str]:
    """Return the relative paths a plan will download.

    Args:
        plan: A ``FetchPlan``.

    Returns:
        Relative paths of the non-empty log downloads.
    """
    return {target.relative_path for target in plan.logs}


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_listing_timestamps_are_parsed_as_utc() -> None:
    """A listing timestamp is UTC, not local time.

    Parsing as local time still produces a plausible value and preserves the
    spacing between files, so the error is invisible until the restored mtimes
    are compared against anything outside the tarball.
    """
    import calendar

    parsed = parse_listing_mtime(_CORE_MTIME)
    expected = float(calendar.timegm((2026, 8, 19, 8, 18, 20, 0, 0, 0)))
    assert parsed == expected


def test_reference_job_payload_silence_is_7774_seconds() -> None:
    """The reference gap between the last payload write and the core is 7774 s."""
    core = parse_listing_mtime(_CORE_MTIME)
    payload = parse_listing_mtime(_PAYLOAD_STDOUT_MTIME)
    assert core is not None and payload is not None
    assert core - payload == 7774.0


@pytest.mark.parametrize("value", ["", "   ", "not a date", "2026-13-45 99:99:99"])
def test_unparsable_timestamps_return_none(value: str) -> None:
    """Unusable timestamps yield None rather than a wrong epoch value.

    Args:
        value: Raw listing value.
    """
    assert parse_listing_mtime(value) is None


def test_iso_separator_and_trailing_z_are_accepted() -> None:
    """An ISO ``T`` separator and a trailing ``Z`` parse to the same instant."""
    assert parse_listing_mtime("2026-08-19T08:18:20Z") == parse_listing_mtime(_CORE_MTIME)


def test_http_date_matches_the_listing_timestamp() -> None:
    """``Last-Modified`` and the listing agree for the reference core."""
    assert parse_http_date("Wed, 19 Aug 2026 08:18:20 GMT") == parse_listing_mtime(_CORE_MTIME)


def test_http_date_rejects_garbage() -> None:
    """An unparsable ``Last-Modified`` yields None."""
    assert parse_http_date("last tuesday") is None


# ---------------------------------------------------------------------------
# Media URLs
# ---------------------------------------------------------------------------


def test_media_root_is_built_from_the_log_file_entry() -> None:
    """The media root comes from the log entry's guid, scope and site."""
    assert build_media_root(_metadata(), 7263525363, BASE_URL) == _MEDIA_ROOT


def test_media_root_takes_only_the_first_segment_of_destinationse() -> None:
    """``destinationse`` carries a path; only its leading segment is the site."""
    root = build_media_root(_metadata(), 7263525363, BASE_URL)
    assert root is not None and root.endswith("_CERN")


@pytest.mark.parametrize("mutation", ["no_log_entry", "no_guid", "no_scope", "no_site"])
def test_media_root_is_none_when_metadata_is_incomplete(mutation: str) -> None:
    """Missing media coordinates yield None rather than a malformed URL.

    Args:
        mutation: Which field to remove.
    """
    metadata = _metadata()
    if mutation == "no_log_entry":
        metadata["files"] = [entry for entry in metadata["files"] if entry["type"] != "log"]
    else:
        field = {"no_guid": "guid", "no_scope": "scope", "no_site": "destinationse"}[mutation]
        metadata["files"][1][field] = ""
    assert build_media_root(metadata, 7263525363, BASE_URL) is None


def test_nested_media_url_keeps_the_path_separator() -> None:
    """A nested entry's URL separates dirname from name.

    BigPanDA's own ``media_link`` omits the separator here, producing
    ``.../workDirin.txt``.  That is why the URL is constructed rather than
    taken from the listing.
    """
    url = media_url(_MEDIA_ROOT, "workDir", "in.txt")
    assert url == f"{_MEDIA_ROOT}/workDir/in.txt"
    assert "workDirin.txt" not in url


def test_root_level_media_url_has_no_empty_segment() -> None:
    """A root-level entry's URL has no doubled slash."""
    assert media_url(_MEDIA_ROOT, "", "core.18277") == f"{_MEDIA_ROOT}/core.18277"


def test_media_url_tolerates_slash_wrapped_dirnames() -> None:
    """Leading and trailing slashes in dirname do not double up."""
    assert media_url(_MEDIA_ROOT + "/", "/workDir/", "a.log") == f"{_MEDIA_ROOT}/workDir/a.log"


# ---------------------------------------------------------------------------
# Selection policy
# ---------------------------------------------------------------------------


def test_core_is_selected() -> None:
    """The core file is chosen for analysis."""
    plan = select_files_for_fetch(_reference_listing())
    assert plan.core is not None
    assert plan.core.relative_path == "core.18277"
    assert plan.core.size_bytes == 1065033128


def test_release_setup_is_selected() -> None:
    """``my_release_setup.sh`` is fetched — the container backend requires it."""
    plan = select_files_for_fetch(_reference_listing())
    assert plan.release_setup is not None
    assert plan.release_setup.relative_path == "my_release_setup.sh"


def test_reference_job_fetches_exactly_the_expected_files() -> None:
    """The hang-mode plan is the payload stdout plus one workDir log."""
    plan = select_files_for_fetch(_reference_listing())
    assert _paths(plan) == {
        "payload.stdout",
        "workDir/tmp.stdout.83d9c506-1f2e-4a77-9c31-5b0e2a4d7f10",
    }


def test_non_core_download_stays_under_800_kb() -> None:
    """The whole non-core set is a fraction of the >100 MB job tarball."""
    plan = select_files_for_fetch(_reference_listing())
    assert plan.log_bytes == 454904 + 337391 + 225
    assert plan.log_bytes < 800 * 1024


def test_empty_payload_stream_is_created_locally() -> None:
    """A zero-length ``payload.stderr`` costs no HTTP request.

    Its existence and mtime are evidence; its contents cannot be.
    """
    plan = select_files_for_fetch(_reference_listing())
    assert [target.relative_path for target in plan.empty_files] == ["payload.stderr"]
    assert "payload.stderr" not in _paths(plan)


def test_repeated_basenames_are_all_skipped() -> None:
    """Three distinct ``output.root`` entries are each skipped on their own path."""
    plan = select_files_for_fetch(_reference_listing())
    skipped = dict(plan.skipped)
    for path in (
        "workDir/output.root",
        "workDir/workDir/hist/output.root",
        "workDir/workDir/input/output.root",
    ):
        assert path in skipped


def test_unpacked_release_is_skipped_even_when_log_like() -> None:
    """``workDir/usr`` is excluded by location, not by filename.

    A ``CMakeOutput.log`` under the unpacked release passes the log-like name
    test, so a name-only filter would pull in the build tree.
    """
    plan = select_files_for_fetch(_reference_listing())
    skipped = dict(plan.skipped)
    assert "workDir/usr/UserAnalysis/CMakeFiles/CMakeOutput.log" in skipped
    assert "usr" in skipped["workDir/usr/UserAnalysis/CMakeFiles/CMakeOutput.log"]


def test_root_level_log_like_file_is_not_fetched() -> None:
    """``remote_open.stderr`` is log-like but the analyzer never discovers it."""
    plan = select_files_for_fetch(_reference_listing())
    assert "remote_open.stderr" not in _paths(plan)
    assert "remote_open.stderr" in dict(plan.skipped)


def test_non_log_files_are_skipped() -> None:
    """Files that are not log-like by name are skipped."""
    plan = select_files_for_fetch(_reference_listing())
    skipped = dict(plan.skipped)
    assert "workDir/in.txt" in skipped
    assert "memory_monitor_output.txt" in skipped


def test_pilot_log_is_excluded_for_hang_analysis() -> None:
    """The pilot log records what the pilot did *after* declaring a loop."""
    plan = select_files_for_fetch(_reference_listing(), failure_mode="hang")
    assert "pilotlog.txt" not in _paths(plan)
    assert "pilot" in dict(plan.skipped)["pilotlog.txt"]


def test_pilot_log_is_fetched_for_crash_analysis() -> None:
    """For a non-hang failure the pilot log is useful and is fetched."""
    plan = select_files_for_fetch(_reference_listing(), failure_mode="crash")
    assert "pilotlog.txt" in _paths(plan)


def test_stale_workdir_log_is_dropped_in_hang_mode() -> None:
    """A log already stale when the payload fell silent is not evidence."""
    plan = select_files_for_fetch(_reference_listing(), failure_mode="hang")
    assert "workDir/old-reference.log" not in _paths(plan)
    assert "recency window" in dict(plan.skipped)["workDir/old-reference.log"]


def test_stale_workdir_log_is_kept_in_crash_mode() -> None:
    """The recency window applies to hang analysis only."""
    plan = select_files_for_fetch(_reference_listing(), failure_mode="crash")
    assert "workDir/old-reference.log" in _paths(plan)


def test_recency_window_is_anchored_on_the_payload_not_the_core() -> None:
    """Anchoring on the core would discard the workDir log that matters.

    The core is captured long after the payload stops, so a window measured
    backwards from the core excludes files that were live at the moment of
    interest.  The reference workDir log sits inside the payload-anchored
    window and outside a core-anchored one.
    """
    listing = _reference_listing()
    anchor = latest_payload_modification(listing)
    core = parse_listing_mtime(_CORE_MTIME)
    workdir_log = parse_listing_mtime(_WORKDIR_LOG_MTIME)
    assert anchor is not None and core is not None and workdir_log is not None

    window = 2 * 60 * 60
    assert workdir_log >= anchor - window
    assert workdir_log < core - window


def test_empty_payload_stream_does_not_anchor_the_window() -> None:
    """A zero-length stream carries no activity information.

    Letting it anchor the window would move the cutoff on the strength of a
    timestamp that describes nothing.
    """
    listing = _reference_listing()
    anchor = latest_payload_modification(listing)
    assert anchor == parse_listing_mtime(_PAYLOAD_STDOUT_MTIME)


def test_missing_anchor_keeps_all_workdir_logs() -> None:
    """With no non-empty payload stream the recency window cannot be applied."""
    listing = [
        _entry("core.1", 1024, "", _CORE_MTIME),
        _entry("my_release_setup.sh", 225),
        _entry("old.log", 4000, "/workDir", _STALE_MTIME),
    ]
    assert latest_payload_modification(listing) is None
    assert "workDir/old.log" in _paths(select_files_for_fetch(listing))


def test_zero_length_core_is_not_selected() -> None:
    """A truncated core means the kernel was still writing it — nothing to analyse."""
    listing = [
        _entry("core.99", 0, "", _CORE_MTIME),
        _entry("my_release_setup.sh", 225),
    ]
    plan = select_files_for_fetch(listing)
    assert plan.core is None
    assert "zero-length" in dict(plan.skipped)["core.99"]


def test_largest_core_wins_and_the_others_are_recorded() -> None:
    """Only one core is fetched; the rest are skipped with a reason."""
    listing = [
        _entry("core.100", 500, "", _CORE_MTIME),
        _entry("core.200", 9000, "", _CORE_MTIME),
        _entry("my_release_setup.sh", 225),
    ]
    plan = select_files_for_fetch(listing)
    assert plan.core is not None and plan.core.name == "core.200"
    assert "core.100" in dict(plan.skipped)
    assert "core.100" not in _paths(plan)


def test_file_count_is_bounded_and_payload_streams_survive() -> None:
    """The plan honours the analyzer's own file bound, best-ranked first."""
    listing = [
        _entry("core.1", 1024, "", _CORE_MTIME),
        _entry("my_release_setup.sh", 225),
        _entry("payload.stdout", 100, "", _PAYLOAD_STDOUT_MTIME),
    ]
    listing += [
        _entry(f"extra{index}.log", 100, "/workDir", _WORKDIR_LOG_MTIME)
        for index in range(20)
    ]
    plan = select_files_for_fetch(listing, max_log_files=5)
    assert len(plan.logs) + len(plan.empty_files) == 5
    assert "payload.stdout" in _paths(plan)
    assert any("discovery bound" in reason for _, reason in plan.skipped)


def test_byte_budget_drops_lowest_ranked_files_first() -> None:
    """A pathological workDir cannot turn a bounded fetch into an unbounded one."""
    listing = [
        _entry("core.1", 1024, "", _CORE_MTIME),
        _entry("my_release_setup.sh", 225),
        _entry("payload.stdout", 1000, "", _PAYLOAD_STDOUT_MTIME),
        _entry("huge.log", 5000, "/workDir", _WORKDIR_LOG_MTIME),
    ]
    plan = select_files_for_fetch(listing, max_log_bytes=2000)
    assert "payload.stdout" in _paths(plan)
    assert "workDir/huge.log" not in _paths(plan)
    assert any("budget" in reason for _, reason in plan.skipped)


def test_default_byte_budget_is_far_above_the_observed_need() -> None:
    """The budget bounds pathology; it does not constrain the normal case."""
    plan = select_files_for_fetch(_reference_listing())
    assert plan.log_bytes < MAX_LOG_BYTES / 50


def test_every_listing_entry_is_either_selected_or_explained() -> None:
    """No entry disappears silently — the selection is fully auditable."""
    listing = _reference_listing()
    plan = select_files_for_fetch(listing)
    accounted = _paths(plan) | {target.relative_path for target in plan.empty_files}
    accounted |= {path for path, _ in plan.skipped}
    if plan.core:
        accounted.add(plan.core.relative_path)
    if plan.release_setup:
        accounted.add(plan.release_setup.relative_path)
    assert {record["relative_path"] for record in listing} == accounted


# ---------------------------------------------------------------------------
# Disk preflight
# ---------------------------------------------------------------------------


def test_disk_preflight_requires_the_core_plus_reserve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Free space must cover the core itself plus a working reserve.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Temporary directory.
    """
    import shutil as shutil_mod

    core_bytes = 1_000_000_000

    def _usage(_path: Any) -> Any:
        return type("Usage", (), {"free": core_bytes + DISK_RESERVE_BYTES - 1})()

    monkeypatch.setattr(shutil_mod, "disk_usage", _usage)
    ok, message = preflight_disk(core_bytes, tmp_path)
    assert not ok
    assert "insufficient disk space" in message
    assert "reserve" in message


def test_disk_preflight_passes_with_headroom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Exactly the core plus the reserve is enough.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Temporary directory.
    """
    import shutil as shutil_mod

    core_bytes = 1_000_000_000

    def _usage(_path: Any) -> Any:
        return type("Usage", (), {"free": core_bytes + DISK_RESERVE_BYTES})()

    monkeypatch.setattr(shutil_mod, "disk_usage", _usage)
    ok, message = preflight_disk(core_bytes, tmp_path)
    assert ok
    assert message == ""


# ---------------------------------------------------------------------------
# Acquisition round trip
# ---------------------------------------------------------------------------


class _FakeInfo:
    """Stand-in for a ``HEAD`` preflight result."""

    def __init__(self, content_length: int, is_html: bool = False, status: int = 200) -> None:
        """Initialise the fake.

        Args:
            content_length: Reported size.
            is_html: Whether the endpoint answered with an HTML page.
            status: HTTP status.
        """
        self.content_length = content_length
        self.is_html = is_html
        self.status_code = status
        self.accept_ranges = True
        self.last_modified = ""

    @property
    def ok(self) -> bool:
        """Return True for a usable 2xx non-HTML response."""
        return 200 <= self.status_code < 300 and not self.is_html


#: Body written for ``payload.stdout`` by the fake transport.  Real content
#: matters because the round-trip test must not touch the file after
#: ``prepare_job_dir`` has stamped it — rewriting it would reset the mtime and
#: make the assertion circular.
_PAYLOAD_BODY: bytes = (
    b"INFO events processed 1000\n"
    b"INFO accepted 1000 out of 1000 events\n"
    b"INFO closing output file\n"
)


def _install_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
    sizes: dict[str, int],
    fail: set[str] | None = None,
    head: Any = None,
) -> list[str]:
    """Replace the HTTP primitives with in-memory writers.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        sizes: Mapping of media URL suffix to the byte count to write.
        fail: URL suffixes whose fetch should fail.
        head: Object to return from the ``HEAD`` preflight.

    Returns:
        A list that accumulates every requested URL, in order.
    """
    import askpanda_atlas._job_prep as job_prep

    requested: list[str] = []
    failures = fail or set()

    def _fake_head(url: str, timeout: float = 0.0) -> Any:
        requested.append(f"HEAD {url}")
        return head

    def _fake_stream(
        url: str, dest: Path, timeout: float = 0.0,
        expected_bytes: int | None = None, allow_resume: bool = False,
        **kwargs: Any,
    ) -> Any:
        requested.append(url)
        suffix = url.rsplit("tarball_PandaJob_7263525363_CERN/", 1)[-1]
        if suffix in failures:
            return type("Result", (), {
                "ok": False, "bytes_written": 0, "error": "simulated failure",
            })()
        size = sizes.get(suffix, 0)
        dest.write_bytes(_PAYLOAD_BODY if suffix == "payload.stdout" else b"x" * size)
        return type("Result", (), {"ok": True, "bytes_written": size, "error": ""})()

    monkeypatch.setattr(job_prep, "head_remote_file", _fake_head)
    monkeypatch.setattr(job_prep, "stream_to_file", _fake_stream)
    return requested


def _small_listing() -> list[dict[str, Any]]:
    """Return the reference listing with a small core.

    The timestamps are the reference job's; only the sizes are reduced so the
    round trip does not write a gigabyte.  Every assertion in the round-trip
    test is about timestamps, so nothing is lost.

    Returns:
        Normalised listing records.
    """
    listing = []
    for record in _reference_listing():
        record = dict(record)
        if record["name"].startswith("core."):
            record["size_bytes"] = 4096
        if record["relative_path"] == "payload.stdout":
            record["size_bytes"] = len(_PAYLOAD_BODY)
        listing.append(record)
    return listing


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listing: Any = None,
    fail: set[str] | None = None,
    head: Any = None,
) -> tuple[PreparedJob, list[str]]:
    """Run ``prepare_job_dir`` against the fake transport.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
        listing: Listing to use; defaults to the small reference listing.
        fail: URL suffixes whose fetch should fail.
        head: Object to return from the ``HEAD`` preflight.

    Returns:
        ``(prepared, requested_urls)``.
    """
    import shutil as shutil_mod

    records = _small_listing() if listing is None else listing
    sizes = {record["relative_path"]: record["size_bytes"] for record in records}
    requested = _install_fake_transport(
        monkeypatch, sizes, fail=fail, head=head or _FakeInfo(4096),
    )
    monkeypatch.setattr(
        shutil_mod, "disk_usage",
        lambda _path: type("Usage", (), {"free": 500 * 1024 ** 3})(),
    )
    prepared = prepare_job_dir(
        7263525363, records, _metadata(), tmp_path, BASE_URL, failure_mode="hang",
    )
    return prepared, requested


def test_prepared_job_dir_has_the_expected_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The reconstructed directory holds the core, the setup and the logs.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    prepared, _ = _prepare(monkeypatch, tmp_path)
    job_dir = prepared.job_dir

    assert job_dir == tmp_path / "job"
    assert (job_dir / "core.18277").is_file()
    assert (job_dir / "my_release_setup.sh").is_file()
    assert (job_dir / "payload.stdout").is_file()
    assert (job_dir / "payload.stderr").is_file()
    assert (job_dir / "workDir" / "tmp.stdout.83d9c506-1f2e-4a77-9c31-5b0e2a4d7f10").is_file()
    assert not (job_dir / "pilotlog.txt").exists()
    assert not (job_dir / "remote_open.stderr").exists()


def test_empty_stream_is_created_without_a_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``payload.stderr`` exists on disk but was never downloaded.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    prepared, requested = _prepare(monkeypatch, tmp_path)

    assert (prepared.job_dir / "payload.stderr").stat().st_size == 0
    assert prepared.created_empty == ["payload.stderr"]
    assert not any(url.endswith("payload.stderr") for url in requested)


def test_core_is_downloaded_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Small files are fetched first so a failure is cheap to discover.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    _, requested = _prepare(monkeypatch, tmp_path)
    downloads = [url for url in requested if not url.startswith("HEAD ")]
    assert downloads[-1].endswith("core.18277")
    assert requested[0].startswith("HEAD ")


def test_restored_mtimes_preserve_the_payload_silence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 7774 s gap survives the round trip through the filesystem.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    prepared, _ = _prepare(monkeypatch, tmp_path)
    core_mtime = (prepared.job_dir / "core.18277").stat().st_mtime
    payload_mtime = (prepared.job_dir / "payload.stdout").stat().st_mtime

    assert core_mtime - payload_mtime == 7774.0
    assert prepared.core_mtime == core_mtime


def test_analyzer_reads_the_silence_back_out_of_the_rebuilt_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: the analyzer derives 7774 s / "2h 09m 34s" from the rebuild.

    This is the assertion that makes ``os.utime()`` load-bearing.  Without the
    restored timestamps the analysis still runs and simply omits its strongest
    deterministic observation, so the value is pinned exactly rather than
    within a tolerance — a silent regression here is the failure mode being
    guarded against.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    from askpanda_atlas._core_dump_analyzer import collect_job_log_evidence

    prepared, _ = _prepare(monkeypatch, tmp_path)
    # Deliberately no write and no utime here: every timestamp under test was
    # applied by prepare_job_dir from the listing alone.
    evidence = collect_job_log_evidence(
        prepared.job_dir, core_mtime=prepared.core_mtime, failure_mode="hang",
    )
    activity = evidence["payload_activity"]

    assert activity["last_write_before_core_s"] == 7774.0
    assert activity["last_write_before_core_human"] == "2h 09m 34s"


def test_discovery_finds_exactly_the_fetched_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Nothing was fetched that discovery ignores, and nothing it needs is missing.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    from askpanda_atlas._core_dump_analyzer import discover_job_logs

    prepared, _ = _prepare(monkeypatch, tmp_path)
    discovered = {
        path.relative_to(prepared.job_dir).as_posix()
        for path in discover_job_logs(prepared.job_dir, failure_mode="hang")
    }
    assert discovered == {
        "payload.stdout",
        "payload.stderr",
        "workDir/tmp.stdout.83d9c506-1f2e-4a77-9c31-5b0e2a4d7f10",
    }


def test_failed_log_fetch_warns_but_does_not_abort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A supplementary log that cannot be fetched is not fatal.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    prepared, _ = _prepare(
        monkeypatch, tmp_path,
        fail={"workDir/tmp.stdout.83d9c506-1f2e-4a77-9c31-5b0e2a4d7f10"},
    )
    assert prepared.core_path.is_file()
    assert any("tmp.stdout" in warning for warning in prepared.warnings)


def test_failed_core_fetch_is_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Without the core there is nothing to analyse.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    with pytest.raises(JobPrepError, match="core dump"):
        _prepare(monkeypatch, tmp_path, fail={"core.18277"})


def test_failed_release_setup_fetch_is_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Without the release setup the container backend cannot run.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    with pytest.raises(JobPrepError, match="my_release_setup.sh"):
        _prepare(monkeypatch, tmp_path, fail={"my_release_setup.sh"})


def test_html_preflight_is_reported_as_an_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An HTML preflight means SSO, not a missing file.

    The SSO-gated endpoint answers with HTTP 200, so the status code alone
    cannot distinguish it from success.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    with pytest.raises(JobPrepError, match="SSO"):
        _prepare(monkeypatch, tmp_path, head=_FakeInfo(4096, is_html=True))


def test_unreachable_core_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A non-2xx preflight aborts before anything is written.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    with pytest.raises(JobPrepError, match="not retrievable"):
        _prepare(monkeypatch, tmp_path, head=_FakeInfo(0, status=404))


def test_preflight_size_disagreement_is_warned_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A listing/server size disagreement is surfaced, and the server wins.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    prepared, _ = _prepare(monkeypatch, tmp_path, head=_FakeInfo(9999))
    assert any("the server reports 9999" in warning for warning in prepared.warnings)


def test_missing_listing_is_reported_clearly(tmp_path: Path) -> None:
    """A missing listing names the reason rather than failing obscurely.

    Args:
        tmp_path: Workspace directory.
    """
    with pytest.raises(JobPrepError, match="listing"):
        prepare_job_dir(1, None, _metadata(), tmp_path, BASE_URL)


def test_missing_media_coordinates_are_reported_clearly(tmp_path: Path) -> None:
    """Metadata without a log entry cannot address any file.

    Args:
        tmp_path: Workspace directory.
    """
    with pytest.raises(JobPrepError, match="GUID"):
        prepare_job_dir(1, _reference_listing(), {"files": []}, tmp_path, BASE_URL)


def test_job_without_a_core_is_reported_clearly(tmp_path: Path) -> None:
    """A job with no core says so instead of failing during acquisition.

    Args:
        tmp_path: Workspace directory.
    """
    listing = [_entry("payload.stdout", 100, "", _PAYLOAD_STDOUT_MTIME)]
    with pytest.raises(JobPrepError, match="no usable core dump"):
        prepare_job_dir(1, listing, _metadata(), tmp_path, BASE_URL)


def test_job_without_release_setup_is_reported_clearly(tmp_path: Path) -> None:
    """A job missing its release setup says so before any download.

    Args:
        tmp_path: Workspace directory.
    """
    listing = [_entry("core.1", 4096, "", _CORE_MTIME)]
    with pytest.raises(JobPrepError, match="my_release_setup.sh"):
        prepare_job_dir(1, listing, _metadata(), tmp_path, BASE_URL)


def test_insufficient_disk_aborts_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The preflight refuses before the job directory is created.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    import shutil as shutil_mod

    _install_fake_transport(monkeypatch, {}, head=_FakeInfo(4096))
    monkeypatch.setattr(
        shutil_mod, "disk_usage", lambda _path: type("Usage", (), {"free": 1024})(),
    )
    with pytest.raises(JobPrepError, match="insufficient disk space"):
        prepare_job_dir(
            7263525363, _small_listing(), _metadata(), tmp_path, BASE_URL,
        )
    assert not (tmp_path / "job").exists()


def test_progress_callback_reports_the_core_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The long download is announced so a caller can surface it.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Workspace directory.
    """
    import shutil as shutil_mod

    records = _small_listing()
    sizes = {record["relative_path"]: record["size_bytes"] for record in records}
    _install_fake_transport(monkeypatch, sizes, head=_FakeInfo(4096))
    monkeypatch.setattr(
        shutil_mod, "disk_usage",
        lambda _path: type("Usage", (), {"free": 500 * 1024 ** 3})(),
    )
    messages: list[str] = []
    prepare_job_dir(
        7263525363, records, _metadata(), tmp_path, BASE_URL, progress=messages.append,
    )
    assert any("core dump" in message for message in messages)
