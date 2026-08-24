"""Detached worker for ``atlas.core_dump_analysis``.

Runs outside the MCP server process, in its own session, and drives one
analysis from start to finish::

    preparing → downloading → analyzing → complete | failed

Everything it knows arrives through the workspace: the manifest names the job
and the requested mode, and every transition is recorded back into the same
file.  That is why a server restart mid-run costs nothing — this process keeps
going and ``status`` keeps reading its manifest.

Run it by hand against an existing workspace when something needs debugging on
a deployment box::

    python -m askpanda_atlas._core_dump_worker /tmp/bamboo/core-analysis/job-7263525363

Standard output and error are the caller's ``worker.log``; the analyzer
subprocess inherits both, so its own progress lines land in the same file
interleaved with the worker's.

This module imports :mod:`askpanda_atlas.core_dump_analysis_impl` for the
manifest and lock primitives.  The dependency runs one way only: the tool
spawns this worker by module *name* and never imports it.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from askpanda_atlas._fallback_http import get_base_url
from askpanda_atlas._job_prep import JobPrepError, prepare_job_dir  # type: ignore[import]
from askpanda_atlas.core_dump_analysis_impl import (  # type: ignore[import]
    EVIDENCE_NAME,
    JOB_DIR_NAME,
    METADATA_TIMEOUT_S,
    STATE_ANALYZING,
    STATE_COMPLETE,
    STATE_DOWNLOADING,
    STATE_PREPARING,
    WORKER_LOG_NAME,
    WORKER_LOG_TAIL_LINES,
    build_analyzer_argv,
    container_timeout_s,
    hard_timeout_s,
    mark_failed,
    read_manifest,
    release_slot,
    resolve_failure_mode,
    update_manifest,
    utc_now,
)
from askpanda_atlas.log_analysis_impl import (  # type: ignore[import]
    _fetch_file_listing,
    _fetch_metadata,
)

logger: logging.Logger = logging.getLogger(__name__)

#: Exit status when the workspace itself is unusable, as distinct from a run
#: that started and failed.  The latter records its reason in the manifest;
#: this one has nowhere to record it.
EXIT_NO_WORKSPACE: int = 2


def _log(message: str) -> None:
    """Write a timestamped line to ``worker.log``.

    Args:
        message: Text to log.
    """
    print(f"[{utc_now()}] {message}", flush=True)


def worker_log_tail(workspace: Path, lines: int = WORKER_LOG_TAIL_LINES) -> str:
    """Return the last lines of the worker log, for a failure message.

    Args:
        workspace: Workspace directory.
        lines: Number of trailing lines to keep.

    Returns:
        The joined tail, or an empty string when the log is unreadable.
    """
    try:
        text = (workspace / WORKER_LOG_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    kept = [line for line in text.splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


def _error_headline(workspace: Path) -> str:
    """Return the reason a failed analysis failed, from the whole worker log.

    :func:`worker_log_tail` keeps the last twenty lines, which for a container
    failure are ALRB's message-of-the-day and command menu rather than the
    cause.  Job 7272161793 surfaced as six kilobytes of ROOT security notices
    with ``Error: unable to source setupfile /srv/my_release_setup.sh``
    scrolled off the top.

    Scans the full log rather than the tail, because the reason can sit
    arbitrarily far above the noise that follows it.

    Args:
        workspace: Workspace directory holding ``worker.log``.

    Returns:
        The error line, or ``""`` when the log is unreadable or holds no
        recognisable error — in which case the caller shows the tail alone,
        which is the pre-existing behaviour.
    """
    try:
        text = (workspace / WORKER_LOG_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        from askpanda_atlas._core_dump_analyzer import (  # noqa: PLC0415
            _last_error_line,
        )
    except ImportError:  # pragma: no cover - analyzer is a hard dependency
        return ""
    return _last_error_line(text)


def _prepare(workspace: Path, manifest: dict[str, Any]) -> Any:
    """Fetch the job's metadata and listing and rebuild its directory.

    Args:
        workspace: Workspace directory.
        manifest: Current manifest.

    Returns:
        The :class:`~askpanda_atlas._job_prep.PreparedJob`.

    Raises:
        JobPrepError: When the directory cannot be reconstructed.
    """
    job_id = int(manifest["job_id"])
    base_url = get_base_url()

    update_manifest(workspace, state=STATE_PREPARING, progress="fetching job metadata")
    metadata = _fetch_metadata(job_id, base_url, METADATA_TIMEOUT_S)
    listing = _fetch_file_listing(job_id, base_url, METADATA_TIMEOUT_S)

    failure_mode, mode_source = resolve_failure_mode(
        str(manifest.get("requested_mode") or "auto"), metadata
    )
    _log(f"failure mode resolved to {failure_mode} ({mode_source})")
    update_manifest(
        workspace, failure_mode=failure_mode, mode_source=mode_source,
        state=STATE_DOWNLOADING, progress="planning the file set",
    )

    def _progress(message: str) -> None:
        _log(message)
        update_manifest(workspace, state=STATE_DOWNLOADING, progress=message)

    prepared = prepare_job_dir(
        job_id, listing, metadata, workspace, base_url,
        failure_mode=failure_mode, progress=_progress,
    )
    plan = prepared.plan
    core = plan.core
    update_manifest(
        workspace,
        core={"relative_path": core.relative_path, "size_bytes": core.size_bytes} if core else None,
        fetched=list(prepared.fetched),
        created_empty=list(prepared.created_empty),
        skipped=[list(item) for item in plan.skipped],
        warnings=list(prepared.warnings),
        bytes_downloaded=int(prepared.bytes_downloaded),
    )
    return prepared


def _run_analyzer(workspace: Path, prepared: Any, failure_mode: str) -> tuple[int, str]:
    """Run the analyzer against the reconstructed job directory.

    The analyzer inherits this process's stdout and stderr, so its progress
    lines land in ``worker.log`` alongside the worker's own.

    Args:
        workspace: Workspace directory.
        prepared: The prepared job.
        failure_mode: Resolved ``"hang"`` or ``"crash"``.

    Returns:
        ``(exit_code, error)``.  *error* is empty on success and holds a
        user-facing message otherwise.
    """
    job_dir = workspace / JOB_DIR_NAME
    argv = build_analyzer_argv(prepared.core_path, job_dir, workspace, failure_mode)
    _log("running: " + " ".join(argv))

    # The container deadline belongs to the analyzer, which can shut its own
    # container down cleanly.  This outer bound only catches an analyzer that
    # never returns at all, so it sits above the inner one.
    outer_deadline = max(container_timeout_s() * 2, hard_timeout_s())
    try:
        completed = subprocess.run(argv, check=False, timeout=outer_deadline)  # noqa: S603
    except subprocess.TimeoutExpired:
        return 124, (
            f"gdb analysis of the core dump did not finish within {int(outer_deadline)}s "
            "and was stopped. The core may be too large for an interactive analysis."
        )
    except OSError as exc:
        return 1, f"The core-dump analyzer could not be started: {exc}"

    if completed.returncode != 0:
        tail = worker_log_tail(workspace)
        headline = _error_headline(workspace)
        detail = f"\n\nLast lines of the analysis log:\n{tail}" if tail else ""
        reason = f"\n{headline}" if headline else ""
        return completed.returncode, (
            f"gdb analysis of the core dump failed (exit status {completed.returncode})."
            f"{reason}{detail}"
        )
    return 0, ""


def _verify_evidence(workspace: Path, before_mtime: float | None) -> str:
    """Check the analyzer actually produced fresh evidence.

    A clean exit with no artifact is its own bug, and a stale artifact left by
    an earlier run in the same workspace would otherwise be reported as this
    run's result — which is exactly the failure mode that re-using the
    workspace introduces.

    Args:
        workspace: Workspace directory.
        before_mtime: Modification time of ``evidence.json`` before the run,
            or ``None`` when it did not exist.

    Returns:
        An empty string when the evidence is usable, a message otherwise.
    """
    path = workspace / EVIDENCE_NAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return (
            "The core-dump analyzer exited successfully but wrote no evidence file. "
            f"See {workspace / WORKER_LOG_NAME}."
        )
    if before_mtime is not None and mtime <= before_mtime:
        return (
            "The core-dump analyzer exited successfully but did not rewrite its evidence "
            f"file, so {path} still holds the previous run's result."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"The core-dump analyzer's evidence file at {path} could not be read: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), dict):
        return f"The core-dump analyzer's evidence file at {path} has no evidence object."
    return ""


def _evidence_mtime(workspace: Path) -> float | None:
    """Return the modification time of an existing evidence file.

    Args:
        workspace: Workspace directory.

    Returns:
        Epoch seconds, or ``None`` when the file does not exist.
    """
    try:
        return (workspace / EVIDENCE_NAME).stat().st_mtime
    except OSError:
        return None


def run(workspace: Path) -> int:
    """Drive one analysis to a terminal state.

    Every failure path records its reason in the manifest and releases the
    analysis slot.  Nothing is deleted, including a partial download: it is
    the input to a resumed retry.

    Args:
        workspace: Workspace directory holding a queued manifest.

    Returns:
        ``0`` on success, ``1`` on a recorded failure,
        :data:`EXIT_NO_WORKSPACE` when the workspace is unusable.
    """
    manifest = read_manifest(workspace)
    if manifest is None or "job_id" not in manifest:
        print(f"error: no usable run record at {workspace}", file=sys.stderr, flush=True)
        return EXIT_NO_WORKSPACE

    request_id = str(manifest.get("request_id") or "")
    root = workspace.parent
    try:
        _log(f"worker started for job {manifest['job_id']} (request {request_id})")
        try:
            prepared = _prepare(workspace, manifest)
        except JobPrepError as exc:
            # Written to be shown to the user verbatim; do not wrap it.
            mark_failed(workspace, str(exc))
            _log(f"preparation failed: {exc}")
            return 1

        current = read_manifest(workspace) or manifest
        failure_mode = str(current.get("failure_mode") or "hang")
        update_manifest(
            workspace, state=STATE_ANALYZING,
            progress=f"running gdb in the ATLAS release container ({failure_mode} mode)",
        )

        before = _evidence_mtime(workspace)
        code, error = _run_analyzer(workspace, prepared, failure_mode)
        if error:
            mark_failed(workspace, error, analyzer_exit_code=code)
            _log(f"analysis failed: {error}")
            return 1

        problem = _verify_evidence(workspace, before)
        if problem:
            mark_failed(workspace, problem, analyzer_exit_code=code)
            _log(f"evidence check failed: {problem}")
            return 1

        update_manifest(
            workspace, state=STATE_COMPLETE, analyzer_exit_code=code,
            progress="analysis complete", finished_utc=utc_now(), error=None,
        )
        _log("analysis complete")
        return 0
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("Core-dump worker failed for %s", workspace)
        mark_failed(workspace, (
            f"The core-dump analysis worker failed unexpectedly: {exc!r}. "
            f"See {workspace / WORKER_LOG_NAME}."
        ))
        return 1
    finally:
        release_slot(root, request_id)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument vector, or ``None`` to use ``sys.argv``.

    Returns:
        The exit status from :func:`run`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m askpanda_atlas._core_dump_worker",
        description="Run one core-dump analysis in an existing workspace.",
    )
    parser.add_argument("workspace", help="Workspace directory holding the run manifest.")
    args = parser.parse_args(argv)
    return run(Path(args.workspace).expanduser().resolve())


if __name__ == "__main__":
    sys.exit(main())
