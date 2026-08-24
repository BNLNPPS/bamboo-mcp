"""Reconstruct a PanDA job directory from BigPanDA for core-dump analysis.

The core-dump analyzer works against a job directory laid out the way the
pilot left it: a core file, ``my_release_setup.sh``, the payload streams, and
whatever log-like artifacts the payload wrote under ``workDir``.  BigPanDA
serves those files individually through its unauthenticated media path, so
this module selects the ones that matter, fetches them, and restores their
modification times.

Why the file set is chosen here rather than downloaded wholesale
----------------------------------------------------------------
A job log tarball routinely exceeds 100 MB and is dominated by files the
analyzer will never open — build products under ``workDir/usr``, several
copies of ``output.root``, cmake modules.  On the reference job the entire
useful non-core set is under 800 kB.  :func:`select_files_for_fetch` mirrors
the analyzer's own :func:`~askpanda_atlas._core_dump_analyzer.discover_job_logs`
rules against the *listing* so that the reconstructed directory contains
what discovery would have chosen anyway, and nothing else.

Why modification times are restored
-----------------------------------
For a looping job the single most informative deterministic fact is how long
the payload had been silent when the core was captured, and that is computed
purely from file mtimes.  A directory rebuilt with "now" as every timestamp
loses the signal silently — the analysis still runs and simply omits its
strongest observation.  Every file written here therefore gets ``os.utime()``
applied from the listing's own ``modification`` field.

Trust boundaries
----------------
- Media URLs are **constructed**, never taken from the listing's
  ``media_link``: BigPanDA omits the ``dirname``/``name`` separator for nested
  entries, producing paths such as ``.../workDirin.txt``.
- The SSO-gated per-file endpoint answers an unauthenticated request with an
  HTTP 200 HTML login page, so every transfer goes through
  :func:`~askpanda_atlas._cache.stream_to_file`, which rejects HTML bodies.
- Nothing in this module deletes anything, including its own partial
  downloads.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from askpanda_atlas._cache import (  # type: ignore[import]
    DEFAULT_BINARY_TIMEOUT_S,
    RemoteFileInfo,
    head_remote_file,
    stream_to_file,
)
from askpanda_atlas._core_dump_analyzer import (  # type: ignore[import]
    DEFAULT_HANG_WORKDIR_LOG_RECENCY_S,
    DEFAULT_MAX_JOB_LOG_FILES,
    GENERATED_LOG_PREFIXES,
    _looks_like_log_file,
)
from askpanda_atlas.log_analysis_impl import (  # type: ignore[import]
    _find_core_dump_candidates,
    _format_bytes,
)

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Free space demanded on top of the core's own size.  gdb reads the core in
#: place and no second copy is ever made, so the multiplier that would suit a
#: tarball-extraction design does not apply here.
DISK_RESERVE_BYTES: int = 5 * 1024 * 1024 * 1024

#: Per-file deadline for steady-state media downloads.
DEFAULT_FILE_TIMEOUT_S: float = DEFAULT_BINARY_TIMEOUT_S

#: Deadline for the *first* media request of a job.  BigPanDA untars the job
#: log server-side on first access, so the opening request can take far longer
#: than every subsequent one.  A job whose listing carries the
#: ``slow_downloading`` advisory is exactly the case this exists for.
DEFAULT_FIRST_ACCESS_TIMEOUT_S: float = 600.0

#: Ceiling on the combined size of the non-core files.  The observed useful
#: set is three orders of magnitude below this; the bound exists so a
#: pathological ``workDir`` full of large log-like files cannot turn a
#: bounded fetch into an unbounded one.  Files are dropped lowest-rank first,
#: so the payload streams survive.
MAX_LOG_BYTES: int = 64 * 1024 * 1024

#: Release setup script the ATLAS container backend sources to reconstruct the
#: job's analysis release.  Without it ``--execution atlas-container`` cannot
#: run at all, so its absence is fatal rather than a warning.
RELEASE_SETUP_NAME: str = "my_release_setup.sh"

#: Canonical payload streams at the job-directory root.
PAYLOAD_STREAM_NAMES: tuple[str, ...] = ("payload.stdout", "payload.stderr")

PILOT_LOG_NAME: str = "pilotlog.txt"
WORK_DIR_NAME: str = "workDir"

#: Subdirectory of ``workDir`` holding the unpacked user release.  It can
#: contain thousands of build and configuration files, some of them log-like
#: by name, none of them runtime evidence.
WORK_DIR_RELEASE_SUBDIR: str = "usr"

#: Role ranking, mirroring
#: :func:`~askpanda_atlas._core_dump_analyzer._job_log_rank`.  Kept identical
#: so the acquisition layer's truncation and the analyzer's own truncation
#: cannot disagree about which files matter.
_ROLE_RANK: dict[str, int] = {
    "payload-stdout": 0,
    "payload-stderr": 0,
    "payload-log": 1,
    "workdir-log": 2,
    "pilot": 3,
    "other": 9,
}


class JobPrepError(RuntimeError):
    """Raised when a job directory cannot be reconstructed.

    The message is written to be shown to the user verbatim: it names what was
    missing and, where there is one, the reason it is missing.
    """


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaTarget:
    """One file selected from the job listing.

    Attributes:
        relative_path: Path relative to the job directory root.
        name: Basename.
        dirname: Directory component, ``""`` for a root-level file.
        size_bytes: Size reported by the listing.
        modification: Raw listing timestamp, in UTC.
        role: Evidence role, matching the analyzer's own role vocabulary.
        reason: Why this file was selected, for the manifest and for
            explaining a fetch to the user.
    """

    relative_path: str
    name: str
    dirname: str
    size_bytes: int
    modification: str
    role: str
    reason: str


@dataclass
class FetchPlan:
    """The decision about which job files to fetch, before any I/O.

    Attributes:
        core: Selected core file, or ``None`` when the job has none.
        release_setup: Selected ``my_release_setup.sh``, or ``None``.
        logs: Non-empty files to download, in rank order.
        empty_files: Zero-length files to create locally.  Their existence and
            mtime are evidence; their contents are not, so fetching them would
            spend a request on nothing.
        skipped: ``(relative_path, reason)`` for every listing entry that was
            not selected, so the choice is auditable after the fact.
    """

    core: MediaTarget | None = None
    release_setup: MediaTarget | None = None
    logs: list[MediaTarget] = field(default_factory=list)
    empty_files: list[MediaTarget] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def core_bytes(self) -> int:
        """Return the size of the selected core, or ``0`` when there is none."""
        return self.core.size_bytes if self.core else 0

    @property
    def log_bytes(self) -> int:
        """Return the combined size of the non-core files to be downloaded."""
        total = sum(target.size_bytes for target in self.logs)
        if self.release_setup:
            total += self.release_setup.size_bytes
        return total

    @property
    def request_count(self) -> int:
        """Return the number of media downloads this plan implies."""
        return len(self.logs) + (1 if self.core else 0) + (1 if self.release_setup else 0)


@dataclass
class PreparedJob:
    """A reconstructed job directory, ready for the analyzer.

    Attributes:
        job_dir: Root of the reconstructed directory.  Writable: the container
            backend creates temporary files inside it.
        core_path: Path of the fetched core file.
        core_mtime: Restored core mtime, the anchor for every
            time-since-last-write observation.
        release_setup_path: Path of the fetched release setup script.
        fetched: Relative paths downloaded.
        created_empty: Relative paths created locally without a request.
        bytes_downloaded: Total bytes transferred.
        warnings: Non-fatal problems worth surfacing, such as a log that could
            not be fetched or a timestamp that could not be restored.
        plan: The plan this directory was built from.
    """

    job_dir: Path
    core_path: Path
    core_mtime: float
    release_setup_path: Path
    fetched: list[str] = field(default_factory=list)
    created_empty: list[str] = field(default_factory=list)
    bytes_downloaded: int = 0
    warnings: list[str] = field(default_factory=list)
    plan: FetchPlan = field(default_factory=FetchPlan)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def parse_listing_mtime(value: str) -> float | None:
    """Parse a BigPanDA listing timestamp into an epoch seconds value.

    Listing timestamps carry no zone designator and are UTC.  Parsing them as
    local time would shift every restored mtime by the host's offset, which
    silently corrupts the payload-silence duration the analysis depends on —
    and does so without failing, since the *relative* spacing survives while
    the comparison against the core's own timestamp does not.

    Args:
        value: Raw ``modification`` field, e.g. ``"2026-08-19 08:18:20"``.
            An ISO ``T`` separator and a trailing ``Z`` are also accepted.

    Returns:
        Epoch seconds, or ``None`` when the value is empty or unparsable.
    """
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).timestamp()
    logger.debug("Unparsable listing timestamp: %r", value)
    return None


def parse_http_date(value: str) -> float | None:
    """Parse an RFC 1123 ``Last-Modified`` header into epoch seconds.

    Used as a fallback when a listing entry carries no usable ``modification``
    field but a ``HEAD`` preflight supplied the header.

    Args:
        value: Raw header value, e.g. ``"Wed, 19 Aug 2026 08:18:20 GMT"``.

    Returns:
        Epoch seconds, or ``None`` when absent or unparsable.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Media URL construction
# ---------------------------------------------------------------------------


def _log_file_entry(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the job metadata ``files[]`` entry describing the job log.

    Args:
        metadata: Parsed ``/job?pandaid=...&json`` response, or ``None``.

    Returns:
        The entry whose ``type`` is ``"log"``, or ``None`` when absent.
    """
    if not isinstance(metadata, dict):
        return None
    files = metadata.get("files")
    if not isinstance(files, list):
        return None
    for entry in files:
        if isinstance(entry, dict) and str(entry.get("type") or "").lower() == "log":
            return entry
    return None


def build_media_root(
    metadata: dict[str, Any] | None,
    job_id: int,
    base_url: str,
) -> str | None:
    """Build the media root URL under which a job's unpacked files are served.

    The unpacked tarball lives at a path derived from the log file's own GUID
    and scope, plus the destination site::

        {base_url}/media/filebrowser/{guid}/{scope}/tarball_PandaJob_{job_id}_{site}

    Args:
        metadata: Parsed job metadata response.
        job_id: PanDA job ID.
        base_url: BigPanDA base URL.

    Returns:
        The media root without a trailing slash, or ``None`` when the metadata
        does not carry the fields needed to build it.
    """
    entry = _log_file_entry(metadata)
    if entry is None:
        return None
    guid = str(entry.get("guid") or "").strip()
    scope = str(entry.get("scope") or "").strip()
    site = str(entry.get("destinationse") or "").split("/")[0].strip()
    if not guid or not scope or not site:
        logger.warning(
            "Job %d log metadata is incomplete (guid=%r scope=%r site=%r)",
            job_id, guid, scope, site,
        )
        return None
    root = base_url.rstrip("/")
    return f"{root}/media/filebrowser/{guid}/{scope}/tarball_PandaJob_{job_id}_{site}"


def media_url(media_root: str, dirname: str, name: str) -> str:
    """Build the media URL for one listing entry.

    The listing's own ``media_link`` is unusable for nested entries: BigPanDA
    concatenates ``dirname`` and ``name`` without a separator, so
    ``workDir`` + ``in.txt`` becomes ``workDirin.txt``.  The link is correct
    only when ``dirname`` is empty, which is precisely the case where it adds
    nothing.  The URL is therefore always constructed.

    Args:
        media_root: Root from :func:`build_media_root`.
        dirname: Entry's directory component, possibly slash-wrapped.
        name: Entry's basename.

    Returns:
        The full media URL.
    """
    root = media_root.rstrip("/")
    clean = str(dirname or "").strip("/")
    return f"{root}/{clean}/{name}" if clean else f"{root}/{name}"


# ---------------------------------------------------------------------------
# Selection policy
# ---------------------------------------------------------------------------


def _record_role(dirname: str, name: str) -> str:
    """Classify a listing entry the way the analyzer classifies a local path.

    Args:
        dirname: Entry's directory component, already stripped of slashes.
        name: Entry's basename.

    Returns:
        One of the roles in :data:`_ROLE_RANK`.
    """
    lowered = name.lower()
    if not dirname:
        if lowered == "payload.stdout":
            return "payload-stdout"
        if lowered == "payload.stderr":
            return "payload-stderr"
        if lowered == PILOT_LOG_NAME:
            return "pilot"
        if "payload" in lowered:
            return "payload-log"
        return "other"
    if dirname.split("/")[0] == WORK_DIR_NAME:
        return "workdir-log"
    return "other"


def latest_payload_modification(listing: list[dict[str, Any]]) -> float | None:
    """Return the newest mtime across the non-empty root payload streams.

    This anchors the recency window for ``workDir`` logs.  It is deliberately
    the payload streams and not the core: a looping job's payload has by
    definition been silent for a long time before the core is captured, so a
    window measured backwards from the core would discard exactly the files
    that were still being written when the payload stopped.

    Empty streams are excluded — a zero-length ``payload.stderr`` carries no
    activity information, and letting its timestamp anchor the window would
    move the cutoff for no reason.

    Args:
        listing: Normalised listing records.

    Returns:
        Epoch seconds of the newest qualifying stream, or ``None``.
    """
    mtimes: list[float] = []
    for record in listing:
        if str(record.get("dirname") or ""):
            continue
        if str(record.get("name") or "") not in PAYLOAD_STREAM_NAMES:
            continue
        if int(record.get("size_bytes") or 0) <= 0:
            continue
        parsed = parse_listing_mtime(str(record.get("modification") or ""))
        if parsed is not None:
            mtimes.append(parsed)
    return max(mtimes, default=None)


def _workdir_skip_reason(
    record: dict[str, Any],
    failure_mode: str,
    anchor: float | None,
) -> str:
    """Return why a ``workDir`` entry is not payload evidence, or ``""``.

    Args:
        record: Normalised listing record with a ``workDir`` dirname.
        failure_mode: Resolved analysis mode; ``"hang"`` enables the recency
            window.
        anchor: Newest payload-stream mtime, or ``None``.

    Returns:
        A reason string, or ``""`` when the entry should be fetched.
    """
    dirname = str(record.get("dirname") or "")
    name = str(record.get("name") or "")
    parts = dirname.split("/")
    if len(parts) > 1 and parts[1] == WORK_DIR_RELEASE_SUBDIR:
        return "under workDir/usr — unpacked release, not runtime output"
    if name.lower().startswith(GENERATED_LOG_PREFIXES):
        return "artifact of a previous core analysis"
    if not _looks_like_log_file(Path(name)):
        return "not a log-like filename"
    if failure_mode == "hang" and anchor is not None:
        mtime = parse_listing_mtime(str(record.get("modification") or ""))
        if mtime is None:
            return "no usable timestamp to test against the recency window"
        if mtime < anchor - DEFAULT_HANG_WORKDIR_LOG_RECENCY_S:
            return (
                "already stale when the payload fell silent "
                f"(outside the {DEFAULT_HANG_WORKDIR_LOG_RECENCY_S}s recency window)"
            )
    return ""


def _root_skip_reason(record: dict[str, Any], failure_mode: str) -> str:
    """Return why a root-level entry is not fetched as a log, or ``""``.

    Mirrors :func:`~askpanda_atlas._core_dump_analyzer._payload_stream_logs`,
    which globs ``payload*`` only.  A root-level ``remote_open.stderr`` is
    log-like by name but is not discovered by the analyzer, so fetching it
    would cost a request for a file that is never opened.

    Args:
        record: Normalised listing record with an empty dirname.
        failure_mode: Resolved analysis mode.

    Returns:
        A reason string, or ``""`` when the entry should be fetched.
    """
    name = str(record.get("name") or "")
    lowered = name.lower()
    if lowered in PAYLOAD_STREAM_NAMES:
        return ""
    if lowered == PILOT_LOG_NAME:
        if failure_mode == "hang":
            return (
                "pilot log excluded for hang analysis — it records what the "
                "pilot did after deciding the payload was looping"
            )
        return ""
    if lowered.startswith("payload") and _looks_like_log_file(Path(name)):
        return ""
    return "not a payload stream — the analyzer does not discover it"


def _rank_key(target: MediaTarget, core_mtime: float | None) -> tuple[int, float, str]:
    """Return the sort key mirroring the analyzer's own log ranking.

    Args:
        target: Candidate file.
        core_mtime: Core capture time, or ``None``.

    Returns:
        ``(role_rank, distance_from_core, relative_path)``.
    """
    recency = float("inf")
    if core_mtime is not None:
        parsed = parse_listing_mtime(target.modification)
        if parsed is not None:
            recency = abs(parsed - core_mtime)
    return (_ROLE_RANK.get(target.role, 9), recency, target.relative_path)


def _select_core(
    listing: list[dict[str, Any]],
    plan: FetchPlan,
) -> tuple[MediaTarget | None, set[str]]:
    """Choose the core file to analyse, recording the rejected ones.

    Selection is delegated to
    :func:`~askpanda_atlas.log_analysis_impl._find_core_dump_candidates` so
    the file analysed here is necessarily the same one the probe named when it
    offered the analysis.  Re-deriving the choice locally would let the offer
    and the analysis drift apart without any test noticing.

    Args:
        listing: Normalised listing records.
        plan: Plan to record skipped candidates on.

    Returns:
        ``(chosen, core_paths)`` where *chosen* is the largest non-empty core
        or ``None``, and *core_paths* holds every core file's relative path so
        the caller can exclude all of them from the log candidates.
    """
    candidates = _find_core_dump_candidates(listing)
    chosen: MediaTarget | None = None
    core_paths: set[str] = set()
    for candidate in candidates:
        dirname = str(candidate.get("dirname") or "").strip("/")
        name = str(candidate["name"])
        relative_path = f"{dirname}/{name}" if dirname else name
        size = int(candidate["size_bytes"])
        core_paths.add(relative_path)
        if chosen is not None:
            plan.skipped.append((relative_path, "smaller additional core file"))
            continue
        if size <= 0:
            plan.skipped.append(
                (relative_path, "zero-length core — kernel was still writing it")
            )
            continue
        chosen = MediaTarget(
            relative_path=relative_path,
            name=name,
            dirname=dirname,
            size_bytes=size,
            modification=str(candidate.get("modification") or ""),
            role="core",
            reason="largest usable core file in the job log",
        )
    return chosen, core_paths


def _apply_bounds(
    ranked: list[MediaTarget],
    plan: FetchPlan,
    max_log_files: int,
    max_log_bytes: int,
) -> None:
    """Split ranked candidates into fetches and local creations, within bounds.

    Args:
        ranked: Candidates in rank order, best first.
        plan: Plan to populate.
        max_log_files: Upper bound on the number of files, matching the
            analyzer's own discovery bound.
        max_log_bytes: Upper bound on their combined size.
    """
    for target in ranked[max_log_files:]:
        plan.skipped.append(
            (target.relative_path, f"beyond the {max_log_files}-file discovery bound")
        )

    running = 0
    for target in ranked[:max_log_files]:
        if target.size_bytes <= 0:
            plan.empty_files.append(target)
            continue
        if running + target.size_bytes > max_log_bytes:
            plan.skipped.append((
                target.relative_path,
                f"would exceed the {_format_bytes(max_log_bytes)} non-core budget",
            ))
            continue
        running += target.size_bytes
        plan.logs.append(target)


def select_files_for_fetch(
    listing: list[dict[str, Any]],
    failure_mode: str = "hang",
    max_log_files: int = DEFAULT_MAX_JOB_LOG_FILES,
    max_log_bytes: int = MAX_LOG_BYTES,
) -> FetchPlan:
    """Decide which files to fetch from a job listing.  Pure; performs no I/O.

    The rules mirror
    :func:`~askpanda_atlas._core_dump_analyzer.discover_job_logs` applied to
    the listing instead of to a local directory, so the reconstructed job
    directory contains what discovery would have selected and nothing more.
    Every entry that is not selected is recorded in
    :attr:`FetchPlan.skipped` with its reason.

    Args:
        listing: Normalised records from
            :func:`~askpanda_atlas.log_analysis_impl._fetch_file_listing`.
        failure_mode: ``"hang"`` for a looping-job kill, which excludes the
            pilot log and applies the ``workDir`` recency window; anything
            else keeps both.
        max_log_files: Upper bound on non-core files.
        max_log_bytes: Upper bound on their combined size.

    Returns:
        The populated :class:`FetchPlan`.
    """
    plan = FetchPlan()
    plan.core, core_paths = _select_core(listing, plan)
    core_mtime = parse_listing_mtime(plan.core.modification) if plan.core else None
    anchor = latest_payload_modification(listing)

    candidates: list[MediaTarget] = []
    for record in listing:
        relative_path = str(record.get("relative_path") or "")
        name = str(record.get("name") or "")
        dirname = str(record.get("dirname") or "").strip("/")
        size = int(record.get("size_bytes") or 0)

        if relative_path in core_paths:
            continue
        if not dirname and name == RELEASE_SETUP_NAME:
            plan.release_setup = MediaTarget(
                relative_path=relative_path, name=name, dirname=dirname,
                size_bytes=size, modification=str(record.get("modification") or ""),
                role="release-setup",
                reason="required to reconstruct the job's analysis release",
            )
            continue

        role = _record_role(dirname, name)
        if dirname.split("/")[0] == WORK_DIR_NAME:
            reason = _workdir_skip_reason(record, failure_mode, anchor)
        elif not dirname:
            reason = _root_skip_reason(record, failure_mode)
        else:
            reason = "outside the job root and workDir"

        if reason:
            plan.skipped.append((relative_path, reason))
            continue

        candidates.append(MediaTarget(
            relative_path=relative_path, name=name, dirname=dirname,
            size_bytes=size, modification=str(record.get("modification") or ""),
            role=role, reason="payload evidence discovered by the analyzer",
        ))

    candidates.sort(key=lambda target: _rank_key(target, core_mtime))
    _apply_bounds(candidates, plan, max_log_files, max_log_bytes)
    return plan


# ---------------------------------------------------------------------------
# Disk preflight
# ---------------------------------------------------------------------------


def preflight_disk(core_bytes: int, path: Path) -> tuple[bool, str]:
    """Check there is room for the core plus a working reserve.

    gdb reads the core in place and nothing extracts a second copy, so the
    requirement is the core's own size plus headroom for the analyzer's
    temporary files and for whatever else shares the filesystem.

    Args:
        core_bytes: Authoritative core size.
        path: An existing directory on the target filesystem.

    Returns:
        ``(ok, message)``.  The message names the shortfall on failure and is
        empty on success.
    """
    required = int(core_bytes) + DISK_RESERVE_BYTES
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        return False, f"could not determine free space on {path}: {exc}"
    if free >= required:
        return True, ""
    return False, (
        f"insufficient disk space on {path}: {_format_bytes(free)} free but "
        f"{_format_bytes(required)} required "
        f"({_format_bytes(int(core_bytes))} core + {_format_bytes(DISK_RESERVE_BYTES)} reserve)"
    )


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def _apply_mtime(path: Path, modification: str, fallback: str = "") -> bool:
    """Restore a file's modification time from the listing.

    Args:
        path: File to stamp.
        modification: Listing ``modification`` value, parsed as UTC.
        fallback: ``Last-Modified`` header to fall back to.

    Returns:
        ``True`` when a timestamp was applied.
    """
    mtime = parse_listing_mtime(modification) or parse_http_date(fallback)
    if mtime is None:
        return False
    try:
        os.utime(path, (mtime, mtime))
    except OSError as exc:
        logger.warning("Could not set mtime on %s: %s", path, exc)
        return False
    return True


def _create_empty_file(job_dir: Path, target: MediaTarget, prepared: PreparedJob) -> None:
    """Create a zero-length file locally instead of requesting it.

    A zero-length ``payload.stderr`` still matters: the analyzer globs for its
    existence, and its mtime participates in the evidence.  Neither fact
    requires a transfer, and its content is by definition empty.

    Args:
        job_dir: Reconstructed job directory root.
        target: The zero-length entry.
        prepared: Result object to record the creation on.
    """
    path = job_dir / target.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    if not _apply_mtime(path, target.modification):
        prepared.warnings.append(
            f"{target.relative_path}: no usable timestamp in the listing"
        )
    prepared.created_empty.append(target.relative_path)


def _fetch_target(
    job_dir: Path,
    media_root: str,
    target: MediaTarget,
    timeout: float,
    prepared: PreparedJob,
    expected_bytes: int | None = None,
    allow_resume: bool = False,
) -> Path | None:
    """Download one file into the job directory and restore its mtime.

    Args:
        job_dir: Reconstructed job directory root.
        media_root: Root from :func:`build_media_root`.
        target: File to fetch.
        timeout: Request deadline in seconds.
        prepared: Result object to record the outcome on.
        expected_bytes: Authoritative size for verification.  Supplied for the
            core, where a ``HEAD`` preflight has confirmed it; left ``None``
            for small logs, where a stale listing size is a poor reason to
            fail an otherwise usable transfer.
        allow_resume: Permit a byte-range resume of a previous partial fetch.

    Returns:
        The written path, or ``None`` when the fetch failed.
    """
    url = media_url(media_root, target.dirname, target.name)
    path = job_dir / target.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)

    result = stream_to_file(
        url, path, timeout=timeout,
        expected_bytes=expected_bytes, allow_resume=allow_resume,
    )
    if not result.ok:
        prepared.warnings.append(f"{target.relative_path}: {result.error}")
        return None

    prepared.bytes_downloaded += result.bytes_written
    prepared.fetched.append(target.relative_path)
    if expected_bytes is None and target.size_bytes and result.bytes_written != target.size_bytes:
        prepared.warnings.append(
            f"{target.relative_path}: listing reported {target.size_bytes} bytes "
            f"but {result.bytes_written} were transferred"
        )
    if not _apply_mtime(path, target.modification):
        prepared.warnings.append(
            f"{target.relative_path}: no usable timestamp in the listing"
        )
    return path


def _core_preflight(
    media_root: str,
    core: MediaTarget,
    timeout: float,
) -> tuple[RemoteFileInfo | None, int]:
    """Probe the core with ``HEAD`` to obtain its authoritative size.

    Args:
        media_root: Root from :func:`build_media_root`.
        core: Selected core file.
        timeout: Request deadline in seconds.

    Returns:
        ``(info, size_bytes)``.  *info* is ``None`` when the server could not
        be reached, in which case the listing's size is used.

    Raises:
        JobPrepError: When the endpoint answers with an HTML page or a non-2xx
            status, both of which mean the core cannot be retrieved at all.
    """
    url = media_url(media_root, core.dirname, core.name)
    info = head_remote_file(url, timeout)
    if info is None:
        logger.warning("Core preflight unavailable; using listing size for %s", url)
        return None, core.size_bytes
    if info.is_html:
        raise JobPrepError(
            "The core dump endpoint returned an HTML page rather than file data, "
            "which means the request was redirected to CERN SSO. The unauthenticated "
            "media path is the only supported route to job files."
        )
    if not info.ok:
        raise JobPrepError(
            f"The core dump is not retrievable: HTTP {info.status_code} for {url}."
        )
    size = info.content_length if info.content_length is not None else core.size_bytes
    return info, int(size)


def _require(plan: FetchPlan, job_id: int) -> None:
    """Fail early when the plan is missing something the analysis needs.

    Args:
        plan: Plan to validate.
        job_id: PanDA job ID, for the message.

    Raises:
        JobPrepError: When no core or no release setup was found.
    """
    if plan.core is None:
        raise JobPrepError(
            f"Job {job_id} has no usable core dump in its job log, so there is "
            "nothing to analyse."
        )
    if plan.release_setup is None:
        raise JobPrepError(
            f"Job {job_id} has no {RELEASE_SETUP_NAME} in its job log. It is "
            "required to reconstruct the analysis release inside the container, "
            "and gdb cannot resolve the payload's symbols without it."
        )


def prepare_job_dir(
    job_id: int,
    listing: list[dict[str, Any]] | None,
    metadata: dict[str, Any] | None,
    workspace: Path,
    base_url: str,
    failure_mode: str = "hang",
    timeout: float = DEFAULT_FILE_TIMEOUT_S,
    first_access_timeout: float = DEFAULT_FIRST_ACCESS_TIMEOUT_S,
    progress: Callable[[str], None] | None = None,
) -> PreparedJob:
    """Reconstruct a job directory under *workspace* for core-dump analysis.

    Ordering is deliberate.  The core is probed first, because its size drives
    the disk preflight and a redirect to SSO must be discovered before
    anything is written.  It is then *downloaded last*, after the small files:
    a failure in the ~800 kB log set is far cheaper to learn from than one
    that surfaces after a gigabyte has been transferred.

    Args:
        job_id: PanDA job ID.
        listing: Normalised listing records, or ``None`` when unavailable.
        metadata: Parsed job metadata response, needed for the media root.
        workspace: Directory to build under.  ``job/`` is created inside it
            and must be writable — the container backend writes temporary
            files there.
        base_url: BigPanDA base URL.
        failure_mode: ``"hang"`` for a looping-job kill.
        timeout: Per-file request deadline.
        first_access_timeout: Deadline for the first media request, which may
            trigger a server-side untar of the job log.
        progress: Optional callback invoked with short status lines.

    Returns:
        The populated :class:`PreparedJob`.

    Raises:
        JobPrepError: When the directory cannot be reconstructed — no listing,
            no media root, no core, no release setup, insufficient disk, or a
            failed core or release-setup transfer.
    """
    def _say(message: str) -> None:
        logger.info("Job %d prep: %s", job_id, message)
        if progress is not None:
            progress(message)

    if listing is None:
        raise JobPrepError(
            f"The file listing for job {job_id} could not be fetched, so its "
            "core dump cannot be located."
        )

    media_root = build_media_root(metadata, job_id, base_url)
    if media_root is None:
        raise JobPrepError(
            f"Job {job_id} metadata does not carry the log GUID, scope and "
            "destination site needed to address its files."
        )

    plan = select_files_for_fetch(listing, failure_mode=failure_mode)
    _require(plan, job_id)
    core = plan.core
    release_setup = plan.release_setup
    assert core is not None and release_setup is not None  # narrowed by _require

    info, core_bytes = _core_preflight(media_root, core, first_access_timeout)
    ok, message = preflight_disk(core_bytes, workspace)
    if not ok:
        raise JobPrepError(message)

    job_dir = workspace / "job"
    job_dir.mkdir(parents=True, exist_ok=True)
    prepared = PreparedJob(
        job_dir=job_dir, core_path=job_dir / core.relative_path, core_mtime=0.0,
        release_setup_path=job_dir / release_setup.relative_path, plan=plan,
    )
    if info is not None and info.content_length is not None and info.content_length != core.size_bytes:
        prepared.warnings.append(
            f"{core.relative_path}: listing reported {core.size_bytes} bytes but "
            f"the server reports {info.content_length}"
        )

    for target in plan.empty_files:
        _create_empty_file(job_dir, target, prepared)

    _say(f"fetching {len(plan.logs)} log file(s), {_format_bytes(plan.log_bytes)}")
    for index, target in enumerate(plan.logs):
        _fetch_target(
            job_dir, media_root, target,
            first_access_timeout if index == 0 else timeout, prepared,
        )

    if _fetch_target(job_dir, media_root, release_setup, timeout, prepared) is None:
        raise JobPrepError(
            f"Could not fetch {RELEASE_SETUP_NAME} for job {job_id}: "
            f"{prepared.warnings[-1] if prepared.warnings else 'unknown error'}"
        )

    _say(f"fetching core dump {core.name} ({_format_bytes(core_bytes)})")
    core_path = _fetch_target(
        job_dir, media_root, core, timeout, prepared,
        expected_bytes=core_bytes,
        allow_resume=bool(info is not None and info.accept_ranges),
    )
    if core_path is None:
        raise JobPrepError(
            f"Could not fetch the core dump for job {job_id}: "
            f"{prepared.warnings[-1] if prepared.warnings else 'unknown error'}"
        )

    last_modified = info.last_modified if info is not None else ""
    if not parse_listing_mtime(core.modification) and last_modified:
        _apply_mtime(core_path, "", last_modified)
    prepared.core_path = core_path
    prepared.core_mtime = core_path.stat().st_mtime
    _say(f"job directory ready at {job_dir}")
    return prepared
