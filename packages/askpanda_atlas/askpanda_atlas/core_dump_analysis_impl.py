"""ATLAS core-dump analysis tool — canonical implementation.

Reconstructs a PanDA job directory from BigPanDA, runs gdb against its core
file inside the matching ATLAS release container, and returns the analyzer's
**deterministic evidence**.  No LLM is called from here.

Why no LLM
----------
:func:`~askpanda_atlas._core_dump_analyzer._complete_via_bamboo` refuses to run
inside a live event loop and names the alternative in its own error text: an
async caller should collect evidence with ``--no-llm`` and synthesise through
its own provider stack.  This tool is that async caller.  Synthesis and
:func:`~askpanda_atlas._core_dump_analyzer.reconcile_llm_analysis` therefore
belong to ``bamboo_executor``, alongside the prompt log and the model
configuration, and this module never writes an ``analysis`` artifact.

Execution model
---------------
A core is routinely a gigabyte, so the work runs in a **detached worker**
process and the tool waits inline for a bounded period.  When the analysis
finishes inside that window — the common case, roughly a minute — the caller
gets the full result in the same turn and never sees a handle.  Otherwise it
gets a handle and asks for the result later.

The manifest file *is* the state store.  There is no in-process registry, so a
server restart mid-run loses nothing: ``status`` still answers from disk.

Workspace layout
----------------
::

    $BAMBOO_CORE_ANALYSIS_ROOT/
        .busy.lock                      single slot, holder recorded inside
        job-<job_id>/                   workspace, keyed on the job alone
            .bamboo-core-analysis.json  manifest, atomically replaced
            job/                        prepare_job_dir's job_dir
            evidence.json               analyzer --json
            gdb_raw.txt                 analyzer --raw-gdb
            worker.log                  worker stdout and stderr

Keying the workspace on the job alone is what makes a retry cheap: a failed
core transfer leaves a ``.part`` file, and that file is only resume input if
the next attempt lands in the same directory.

Nothing here deletes anything — not partial downloads, not failed workspaces,
not superseded evidence.  Reaping is a separate concern and deliberately
absent.  The one exception is the busy lock, whose *content* is rewritten in
place to release the slot; the file itself is never removed either.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from askpanda_atlas._core_dump_analyzer import (  # type: ignore[import]
    DEFAULT_ATLAS_LOCAL_ROOT_BASE,
    core_evidence_from_dict,
    enforce_global_budget,
)
from askpanda_atlas._fallback_http import get_base_url
from askpanda_atlas.log_analysis_impl import _format_bytes  # type: ignore[import]

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Names and constants
# ---------------------------------------------------------------------------

MANIFEST_NAME: str = ".bamboo-core-analysis.json"
MANIFEST_VERSION: int = 1
LOCK_NAME: str = ".busy.lock"
WORKER_LOG_NAME: str = "worker.log"
EVIDENCE_NAME: str = "evidence.json"
GDB_RAW_NAME: str = "gdb_raw.txt"
JOB_DIR_NAME: str = "job"

#: Module executed as the detached worker.  Referenced by name, never
#: imported, so there is no import cycle between the two.
WORKER_MODULE: str = "askpanda_atlas._core_dump_worker"
ANALYZER_MODULE: str = "askpanda_atlas._core_dump_analyzer"

DEFAULT_ROOT: str = "/tmp/bamboo/core-analysis"

#: How long ``start`` waits before handing back a handle.  Comfortably under
#: the 300 s ``BAMBOO_MCP_CLIENT_TIMEOUT`` ceiling, which is the real limit.
DEFAULT_INLINE_WAIT_S: float = 120.0

#: Age past which a run that is still non-terminal is declared failed.  This
#: covers a worker that is alive but wedged; a worker that has *died* is
#: detected immediately by its pid, without waiting for this.
DEFAULT_HARD_TIMEOUT_S: float = 900.0

#: Whole-container deadline handed to the analyzer, well below its own 1800 s
#: default because the caller here is an interactive session.
DEFAULT_CONTAINER_TIMEOUT_S: int = 600

#: Ceiling on the bytes held under the analysis root.  Nothing reaps, so this
#: is what stops an unattended deployment filling ``/tmp``.
DEFAULT_QUOTA_BYTES: int = 50 * 1024 * 1024 * 1024

#: Character budget for the evidence handed to synthesis.
#:
#: The analyzer's own default is 50 000, chosen for a CLI that may be talking
#: to a small model.  That is far too tight here: 50 000 characters is roughly
#: 12 500 tokens against a 200 000-token context, and the last stage of
#: ``enforce_global_budget`` is ``primary_thread.backtrace`` — so a job with
#: many shared libraries and several distinct thread stacks spends its budget
#: on cheaper evidence and then truncates the single most valuable field.
#:
#: Job 7272161793 did exactly that: the XRootD shutdown chain from ``Py_Exit``
#: down to ``PollerBuiltIn::Stop`` was cut out of the model's copy while
#: sitting complete in ``evidence.json`` and ``gdb_raw.txt`` on disk.
DEFAULT_EVIDENCE_CHARS: int = 120_000

POLL_INTERVAL_S: float = 2.0

#: Lines of ``worker.log`` quoted when the analyzer exits non-zero.
WORKER_LOG_TAIL_LINES: int = 20

#: Entries of ``FetchPlan.skipped`` carried into the tool's return value.  The
#: manifest keeps the full list; this is the sample a reader can absorb.
SKIPPED_SAMPLE_SIZE: int = 10

#: Pilot error code for a looping-job kill.  The pilot requests a core dump at
#: that point, which is what makes this the hang case.
LOOPING_JOB_PILOT_CODE: int = 1150

STATE_QUEUED: str = "queued"
STATE_PREPARING: str = "preparing"
STATE_DOWNLOADING: str = "downloading"
STATE_ANALYZING: str = "analyzing"
STATE_COMPLETE: str = "complete"
STATE_FAILED: str = "failed"

TERMINAL_STATES: frozenset[str] = frozenset({STATE_COMPLETE, STATE_FAILED})
ACTIONS: tuple[str, ...] = ("start", "status", "result")
MODES: tuple[str, ...] = ("auto", "hang", "crash")

#: Timeout for the metadata and listing requests the worker makes before it
#: can plan anything.  Matches ``panda_log_analysis``.
METADATA_TIMEOUT_S: int = 60


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    """Interpret an environment variable as a boolean flag.

    Accepts the spellings an operator is likely to reach for when setting a
    flag by hand in a shell or a systemd unit.

    Args:
        value: Raw environment value, or ``None`` when unset.

    Returns:
        True when *value* spells an affirmative.
    """
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Read a positive integer from the environment.

    Args:
        name: Variable name.
        default: Value to use when unset, unparsable or non-positive.

    Returns:
        The resolved value.
    """
    try:
        value = int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment.

    Args:
        name: Variable name.
        default: Value to use when unset, unparsable or non-positive.

    Returns:
        The resolved value.
    """
    try:
        value = float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def analysis_root() -> Path:
    """Return the root directory holding every analysis workspace.

    Returns:
        ``$BAMBOO_CORE_ANALYSIS_ROOT``, or :data:`DEFAULT_ROOT`.
    """
    return Path(os.getenv("BAMBOO_CORE_ANALYSIS_ROOT") or DEFAULT_ROOT)


def inline_wait_s() -> float:
    """Return how long ``start`` waits before returning a handle.

    Returns:
        Seconds, from ``$BAMBOO_CORE_ANALYSIS_INLINE_WAIT``.
    """
    return _env_float("BAMBOO_CORE_ANALYSIS_INLINE_WAIT", DEFAULT_INLINE_WAIT_S)


def hard_timeout_s() -> float:
    """Return the age at which a non-terminal run is declared failed.

    Returns:
        Seconds, from ``$BAMBOO_CORE_ANALYSIS_HARD_TIMEOUT``.
    """
    return _env_float("BAMBOO_CORE_ANALYSIS_HARD_TIMEOUT", DEFAULT_HARD_TIMEOUT_S)


def container_timeout_s() -> int:
    """Return the whole-container deadline passed to the analyzer.

    Returns:
        Seconds, from ``$BAMBOO_CORE_ANALYSIS_CONTAINER_TIMEOUT``.
    """
    return int(_env_float("BAMBOO_CORE_ANALYSIS_CONTAINER_TIMEOUT", float(DEFAULT_CONTAINER_TIMEOUT_S)))


def quota_bytes() -> int:
    """Return the byte ceiling for the whole analysis root.

    Returns:
        Bytes, from ``$BAMBOO_CORE_ANALYSIS_MAX_BYTES``.
    """
    return int(_env_float("BAMBOO_CORE_ANALYSIS_MAX_BYTES", float(DEFAULT_QUOTA_BYTES)))


# ---------------------------------------------------------------------------
# Time and process helpers
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string with a ``Z`` suffix.

    Returns:
        Timestamp such as ``"2026-08-20T09:14:02Z"``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> float | None:
    """Parse a timestamp written by :func:`utc_now`.

    Args:
        value: Timestamp string.

    Returns:
        Epoch seconds, or ``None`` when the value is missing or unparsable.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def elapsed_s(manifest: dict[str, Any]) -> float:
    """Return how long a run has taken, or has been running.

    Args:
        manifest: Manifest dict.

    Returns:
        Seconds between ``created_utc`` and ``finished_utc`` (or now),
        rounded to one decimal.  ``0.0`` when the start time is unreadable.
    """
    started = parse_utc(str(manifest.get("created_utc") or ""))
    if started is None:
        return 0.0
    finished = parse_utc(str(manifest.get("finished_utc") or ""))
    end = finished if finished is not None else datetime.now(timezone.utc).timestamp()
    return round(max(0.0, end - started), 1)


def pid_alive(pid: int | None) -> bool:
    """Report whether a process id still exists.

    ``os.kill(pid, 0)`` raising :class:`PermissionError` means the process is
    there but owned by somebody else, which for this purpose is alive.

    Args:
        pid: Process id, or ``None``.

    Returns:
        ``True`` when the process exists.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


def workspace_for(job_id: int, root: Path | None = None) -> Path:
    """Return the workspace directory for a job.

    Keyed on the job alone, deliberately: see the module docstring.

    Args:
        job_id: PanDA job ID.
        root: Analysis root, or ``None`` for :func:`analysis_root`.

    Returns:
        The workspace path, which may not exist yet.
    """
    return (root if root is not None else analysis_root()) / f"job-{int(job_id)}"


def workspace_usage_bytes(root: Path) -> int:
    """Return the total bytes held under the analysis root.

    Args:
        root: Analysis root.

    Returns:
        Sum of file sizes, ``0`` when the root does not exist.  Unreadable
        entries are skipped rather than raising, since the figure is used for
        a refusal threshold and not for accounting.
    """
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def check_quota(root: Path) -> tuple[bool, str]:
    """Check that the analysis root is below its byte ceiling.

    Args:
        root: Analysis root.

    Returns:
        ``(ok, message)``; the message names the usage on refusal.
    """
    limit = quota_bytes()
    used = workspace_usage_bytes(root)
    if used < limit:
        return True, ""
    return False, (
        f"The core-dump analysis workspace at {root} holds {_format_bytes(used)}, "
        f"at or above its {_format_bytes(limit)} ceiling. Nothing is deleted "
        "automatically; free space there before starting another analysis."
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def manifest_path(workspace: Path) -> Path:
    """Return the manifest path for a workspace.

    Args:
        workspace: Workspace directory.

    Returns:
        Path of the manifest file.
    """
    return workspace / MANIFEST_NAME


def new_manifest(job_id: int, request_id: str, requested_mode: str) -> dict[str, Any]:
    """Build the manifest for a run that has not started yet.

    The state is :data:`STATE_QUEUED` and is written *before* the worker is
    spawned, so a crash in the gap between the two is visible on disk rather
    than leaving an empty directory.

    Args:
        job_id: PanDA job ID.
        request_id: Identifier for this run.
        requested_mode: ``"auto"``, ``"hang"`` or ``"crash"`` as asked for.

    Returns:
        A fresh manifest dict.
    """
    now = utc_now()
    return {
        "manifest_version": MANIFEST_VERSION,
        "job_id": int(job_id),
        "request_id": request_id,
        "state": STATE_QUEUED,
        "requested_mode": requested_mode,
        "failure_mode": None,
        "mode_source": "",
        "created_utc": now,
        "updated_utc": now,
        "finished_utc": None,
        "worker_pid": None,
        "progress": "queued",
        "bytes_downloaded": 0,
        "core": None,
        "fetched": [],
        "created_empty": [],
        "skipped": [],
        "warnings": [],
        "analyzer_exit_code": None,
        "error": None,
    }


def read_manifest(workspace: Path) -> dict[str, Any] | None:
    """Read a workspace's manifest.

    A manifest that cannot be read or parsed returns ``None`` rather than
    raising: a half-written run record is a reason to report the run as
    unknown, not a reason to fail the tool call.

    Args:
        workspace: Workspace directory.

    Returns:
        The manifest dict, or ``None``.
    """
    path = manifest_path(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("No manifest at %s", path)
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable manifest at %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("Manifest at %s is not an object", path)
        return None
    return payload


def write_manifest(workspace: Path, manifest: dict[str, Any]) -> None:
    """Write a manifest atomically.

    The temporary file sits in the workspace so the rename stays on one
    filesystem, which is what makes it atomic.

    Args:
        workspace: Workspace directory.
        manifest: Manifest dict to persist.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    path = manifest_path(workspace)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def update_manifest(workspace: Path, **changes: Any) -> dict[str, Any]:
    """Apply changes to a manifest and rewrite it.

    Unknown keys already present are preserved, so a manifest written by a
    newer version does not lose fields on a rewrite by an older one.

    Args:
        workspace: Workspace directory.
        **changes: Fields to set.

    Returns:
        The updated manifest.
    """
    manifest = read_manifest(workspace) or {}
    manifest.update(changes)
    manifest["updated_utc"] = utc_now()
    write_manifest(workspace, manifest)
    return manifest


def claim_worker(workspace: Path, pid: int) -> dict[str, Any]:
    """Record the worker's pid without overwriting its own progress.

    From the moment the worker is spawned it owns the manifest, and the two
    processes are writing to the same file.  A plain
    ``update_manifest(state=preparing)`` after :func:`spawn_worker` therefore
    races the worker's first transition and can drag a run that has already
    advanced — in the worst case one that has already *finished* — back to
    ``preparing``, where nothing would ever move it forward again.

    The pid is still written unconditionally, because without it
    :func:`reconcile_state` cannot detect a worker that died immediately and
    would wait out the whole hard timeout instead.

    Args:
        workspace: Workspace directory.
        pid: Detached worker's process id.

    Returns:
        The updated manifest.
    """
    manifest = read_manifest(workspace) or {}
    manifest["worker_pid"] = int(pid)
    if str(manifest.get("state") or "") == STATE_QUEUED:
        manifest["state"] = STATE_PREPARING
        manifest["progress"] = "worker started"
    manifest["updated_utc"] = utc_now()
    write_manifest(workspace, manifest)
    return manifest


def mark_failed(workspace: Path, error: str, **changes: Any) -> dict[str, Any]:
    """Record a terminal failure.

    Args:
        workspace: Workspace directory.
        error: Message to show the user verbatim.
        **changes: Further fields to set.

    Returns:
        The updated manifest.
    """
    return update_manifest(
        workspace, state=STATE_FAILED, error=error,
        finished_utc=utc_now(), **changes,
    )


# ---------------------------------------------------------------------------
# Busy lock
# ---------------------------------------------------------------------------


@contextmanager
def _lock_transaction(root: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Hold an exclusive advisory lock while reading and rewriting the slot.

    The :mod:`fcntl` lock guards only the read-modify-write of the slot file;
    ownership itself is the *content* of that file, which outlives the
    transaction and survives the death of whichever process wrote it.

    Args:
        root: Analysis root.

    Yields:
        ``(fd, holder)`` where *holder* is the current slot content, ``{}``
        when the slot is free.
    """
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(root / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 65536).decode("utf-8", errors="replace").strip()
        try:
            holder = json.loads(raw) if raw else {}
        except ValueError:
            holder = {}
        yield fd, holder if isinstance(holder, dict) else {}
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_slot(fd: int, holder: dict[str, Any]) -> None:
    """Overwrite the slot file's contents in place.

    Args:
        fd: Open descriptor for the slot file.
        holder: Holder record, or ``{}`` to free the slot.
    """
    payload = json.dumps(holder).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)


def acquire_slot(root: Path, job_id: int, request_id: str) -> tuple[bool, dict[str, Any]]:
    """Claim the single analysis slot.

    A slot whose recorded holder is no longer running is taken over: gdb
    inside a container can be killed by anything from an OOM to a reboot, and
    a dead holder must not wedge the tool permanently.

    Args:
        root: Analysis root.
        job_id: Job this run is for.
        request_id: Identifier for this run.

    Returns:
        ``(acquired, holder)``.  On refusal *holder* describes the run that
        owns the slot.
    """
    with _lock_transaction(root) as (fd, holder):
        if holder and pid_alive(holder.get("pid")):
            return False, holder
        if holder:
            logger.info("Taking over the analysis slot from dead pid %s", holder.get("pid"))
        _write_slot(fd, {
            "pid": os.getpid(),
            "job_id": int(job_id),
            "request_id": request_id,
            "started_utc": utc_now(),
        })
    return True, {}


def bind_slot(root: Path, request_id: str, worker_pid: int) -> None:
    """Transfer slot ownership from the server process to the worker.

    Between :func:`acquire_slot` and this call the slot names the server,
    which is alive, so no second run can start.  Afterwards it names the
    worker, so the slot frees itself if the worker dies.

    Args:
        root: Analysis root.
        request_id: Run that should own the slot.
        worker_pid: Detached worker's process id.
    """
    with _lock_transaction(root) as (fd, holder):
        if holder.get("request_id") != request_id:
            return
        holder["pid"] = int(worker_pid)
        _write_slot(fd, holder)


def release_slot(root: Path, request_id: str) -> None:
    """Free the slot if this run owns it.

    The file is emptied rather than removed, so nothing in this package
    deletes a path.

    Args:
        root: Analysis root.
        request_id: Run releasing the slot.
    """
    with _lock_transaction(root) as (fd, holder):
        if holder and holder.get("request_id") != request_id:
            return
        _write_slot(fd, {})


# ---------------------------------------------------------------------------
# Container runtime detection
# ---------------------------------------------------------------------------

#: Runtime binaries accepted, in preference order.  ``singularity`` is kept
#: because ALRB still resolves to it on older EL7 deployments, and either one
#: satisfies ``atlasLocalSetup.sh -c``.
RUNTIME_BINARIES: tuple[str, ...] = ("apptainer", "singularity")

#: Directory, relative to the CVMFS repository holding ATLASLocalRootBase, in
#: which ALRB ships its own apptainer.  When this is present ALRB supplies the
#: runtime to the container setup itself and the host needs none of its own.
ALRB_RUNTIME_SUBPATH: tuple[str, ...] = ("containers", "sw", "apptainer")

#: Seconds allowed for the login-shell probe.  A login shell sources
#: ``/etc/profile.d`` in full, which is the point of the probe and also the
#: only slow part of it.
RUNTIME_PROBE_TIMEOUT_S: float = 15.0

#: Memoised login-shell probe result.  ``False`` means "not probed yet"; a
#: string or ``None`` is a completed probe.  A *negative* result is cached too,
#: so a wedged or absent shell costs one subprocess per process rather than one
#: per ``start``.  Entry points aside, nothing invalidates this: a host that
#: gains apptainer needs the server restarted, or :func:`reset_runtime_cache`.
_LOGIN_SHELL_RUNTIME: str | None | bool = False


def reset_runtime_cache() -> None:
    """Discard the memoised login-shell probe result.

    Needed by tests, and by any caller that installs a container runtime into
    a host with a long-running server on it.

    Returns:
        None.
    """
    global _LOGIN_SHELL_RUNTIME  # pylint: disable=global-statement
    _LOGIN_SHELL_RUNTIME = False


def _probe_login_shell_runtime() -> str | None:
    """Look for a container runtime on a **login** shell's PATH.

    This exists because the two PATHs differ and only one of them matters.
    ``_collect_evidence_atlas_container`` runs the setup as ``bash -lc``, so
    the PATH that decides whether the analysis can start is a login shell's —
    assembled from ``/etc/profile`` and ``/etc/profile.d`` — not the one the
    MCP server process inherited from systemd, which is typically far narrower.
    Checking the server's own PATH was reporting "apptainer is not on PATH" on
    hosts where the analysis would in fact have run.

    Failures of every kind resolve to ``None``: a host with no ``bash``, a
    profile script that hangs past :data:`RUNTIME_PROBE_TIMEOUT_S`, or a
    non-zero exit.  The caller has one more avenue after this one, and a
    diagnostic probe must never raise into the tool.

    Returns:
        Absolute path to the runtime binary, or ``None``.
    """
    global _LOGIN_SHELL_RUNTIME  # pylint: disable=global-statement
    if _LOGIN_SHELL_RUNTIME is not False:
        return _LOGIN_SHELL_RUNTIME  # type: ignore[return-value]

    resolved: str | None = None
    probe = "; ".join(f"command -v {name}" for name in RUNTIME_BINARIES)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no user input
            ["bash", "-lc", probe],
            capture_output=True, text=True, check=False,
            timeout=RUNTIME_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Login-shell runtime probe failed: %s", exc)
    else:
        for line in completed.stdout.splitlines():
            candidate = line.strip()
            # command -v can print a shell function or builtin name, which is
            # not something the container setup can exec.  Only an absolute
            # path is evidence.
            if candidate.startswith("/") and os.access(candidate, os.X_OK):
                resolved = candidate
                break

    _LOGIN_SHELL_RUNTIME = resolved
    return resolved


def find_container_runtime(
    environ: dict[str, str] | None = None,
    alrb: Path | None = None,
) -> tuple[str | None, str]:
    """Locate the container runtime that will actually start the release.

    Four avenues are tried, in order of cost and directness:

    1. ``$BAMBOO_CORE_DUMP_APPTAINER`` — an explicit override, for a host where
       the runtime is installed somewhere neither PATH nor CVMFS advertises.
    2. The server process's own PATH.  Free, and correct when it hits.
    3. A login shell's PATH — see :func:`_probe_login_shell_runtime`.  This is
       the avenue that matches how the analyzer runs.
    4. ALRB's bundled apptainer under the CVMFS repository.  When present, the
       container setup supplies its own runtime and the host needs none.

    Only avenue 4 is not a binary the caller could exec; it is reported as a
    directory because that is all the caller needs to know.  Nothing here
    changes the no-local-gdb rule: this decides whether the *container* can
    start, never whether to run gdb without one.

    Args:
        environ: Environment mapping, or ``None`` for :data:`os.environ`.
        alrb: Resolved ATLASLocalRootBase, used to derive avenue 4.  ``None``
            skips that avenue.

    Returns:
        ``(location, source)``.  *location* is ``None`` when every avenue
        missed, in which case *source* is empty; otherwise *source* is a short
        phrase naming the avenue, for the audit trail.
    """
    env = environ if environ is not None else dict(os.environ)

    override = (env.get("BAMBOO_CORE_DUMP_APPTAINER") or "").strip()
    if override:
        if os.access(override, os.X_OK) and Path(override).is_file():
            return override, "BAMBOO_CORE_DUMP_APPTAINER"
        logger.warning(
            "BAMBOO_CORE_DUMP_APPTAINER is set to %s, which is not an executable "
            "file; falling through to discovery.", override,
        )

    for name in RUNTIME_BINARIES:
        found = shutil.which(name)
        if found:
            return found, "the server's PATH"

    probed = _probe_login_shell_runtime()
    if probed:
        return probed, "a login shell's PATH"

    if alrb is not None:
        bundled = alrb.parent.joinpath(*ALRB_RUNTIME_SUBPATH)
        if bundled.is_dir():
            return str(bundled), "ALRB's own CVMFS-provided apptainer"

    return None, ""


# ---------------------------------------------------------------------------
# ATLAS environment preflight
# ---------------------------------------------------------------------------


def preflight_atlas_environment(environ: dict[str, str] | None = None) -> tuple[bool, str]:
    """Check the host can run the ATLAS release container.

    Run before the lock and before any download, so a box without CVMFS
    refuses in the same turn rather than three minutes into a transfer.

    There is deliberately no fallback to ``--execution local``.  A gdb outside
    the job's release resolves the payload's symbols against the wrong
    binaries and produces a confident, wrong answer, which is worse than no
    answer at all.  That rule is about *fallback*; the runtime check below is
    about *detection*, and the two are independent — a detection false
    negative refuses an analysis that would have been correct, which is its
    own kind of wrong answer.

    ``BAMBOO_CORE_DUMP_SKIP_RUNTIME_CHECK=1`` drops the runtime check
    entirely, leaving the three CVMFS checks in place.  It exists so a
    detection gap can never again block a live investigation: the analyzer
    reports a missing runtime perfectly well on its own, a few seconds later.

    Args:
        environ: Environment mapping, or ``None`` for :data:`os.environ`.

    Returns:
        ``(ok, message)``.  Each failure names the missing piece distinctly so
        the reader knows which one to fix.
    """
    env = environ if environ is not None else dict(os.environ)
    base = Path(env.get("ATLAS_LOCAL_ROOT_BASE") or DEFAULT_ATLAS_LOCAL_ROOT_BASE)

    if not base.is_dir():
        return False, (
            f"ATLASLocalRootBase is not present at {base}. Core-dump analysis needs "
            "CVMFS mounted on this host to reconstruct the job's release container."
        )
    if not os.access(base, os.R_OK):
        return False, (
            f"ATLASLocalRootBase at {base} exists but is not readable. This is usually "
            "a CVMFS mount that has gone stale."
        )
    setup = base / "user" / "atlasLocalSetup.sh"
    if not setup.is_file():
        return False, (
            f"atlasLocalSetup.sh is missing at {setup}, so the release container cannot "
            "be set up. The CVMFS repository looks incomplete."
        )

    if _truthy(env.get("BAMBOO_CORE_DUMP_SKIP_RUNTIME_CHECK")):
        logger.info("Container runtime check skipped by BAMBOO_CORE_DUMP_SKIP_RUNTIME_CHECK.")
        return True, ""

    location, source = find_container_runtime(env, base)
    if location is None:
        names = " or ".join(RUNTIME_BINARIES)
        bundled = base.parent.joinpath(*ALRB_RUNTIME_SUBPATH)
        return False, (
            f"No container runtime was found, so the ATLAS release container cannot be "
            f"started. Looked for {names} on this process's PATH, on a login shell's "
            f"PATH (which is what atlasLocalSetup.sh runs under), and for ALRB's own "
            f"apptainer at {bundled}. Set BAMBOO_CORE_DUMP_APPTAINER to an explicit "
            f"path, or BAMBOO_CORE_DUMP_SKIP_RUNTIME_CHECK=1 to let the analyzer decide. "
            f"Core-dump analysis will not fall back to the host's own gdb: a mismatched "
            f"release resolves the payload's symbols incorrectly."
        )

    logger.debug("Container runtime resolved via %s: %s", source, location)
    return True, ""


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def resolve_failure_mode(requested: str, metadata: dict[str, Any] | None) -> tuple[str, str]:
    """Resolve the analysis framing for a job.

    The resolved value is used twice and the two must not diverge: it selects
    which files are fetched (a hang excludes ``pilotlog.txt`` and applies the
    ``workDir`` recency window) and it frames the analysis itself.  Note that
    the analyzer's own ``--mode auto`` infers from the *terminating signal*,
    which is a different question from the pilot's error code — so a resolved
    ``hang`` or ``crash`` is passed rather than ``auto``.

    Args:
        requested: ``"auto"``, ``"hang"`` or ``"crash"``.
        metadata: Parsed job metadata, or ``None``.

    Returns:
        ``(failure_mode, mode_source)``.
    """
    if requested in ("hang", "crash"):
        return requested, "requested explicitly"
    job = (metadata or {}).get("job")
    code = 0
    if isinstance(job, dict):
        try:
            code = int(job.get("piloterrorcode") or 0)
        except (TypeError, ValueError):
            code = 0
    if code == LOOPING_JOB_PILOT_CODE:
        return "hang", f"pilot error code {LOOPING_JOB_PILOT_CODE} (looping-job kill)"
    if code:
        return "crash", f"pilot error code {code} is not a looping-job kill"
    return "crash", "no looping-job pilot error code on the job"


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------


def build_analyzer_argv(
    core_path: Path,
    job_dir: Path,
    workspace: Path,
    failure_mode: str,
    container_timeout: int | None = None,
) -> list[str]:
    """Build the analyzer command line.

    ``--exe`` is deliberately absent: the analyzer resolves the ELF
    interpreter itself via ``AT_EXECFN``, then ``NT_FILE``, then the recorded
    command line, and it must never be handed a same-named system binary in
    place of a missing absolute path.

    ``-q`` is deliberately absent too — the analyzer's default stderr progress
    is the only view of a running analysis on a deployment box, and it costs
    nothing to keep it in ``worker.log``.

    Args:
        core_path: Core file inside the reconstructed job directory.
        job_dir: Reconstructed job directory.
        workspace: Workspace holding the output artifacts.
        failure_mode: Resolved ``"hang"`` or ``"crash"``.
        container_timeout: Whole-container deadline, or ``None`` for the
            configured default.

    Returns:
        The argument vector.
    """
    timeout = container_timeout if container_timeout is not None else container_timeout_s()
    argv = [
        sys.executable, "-m", ANALYZER_MODULE, str(core_path),
        "--execution", "atlas-container",
        "--job-dir", str(job_dir),
        "--release-setup", str(job_dir / "my_release_setup.sh"),
        "--mode", failure_mode,
        "--no-llm",
        "--json", str(workspace / EVIDENCE_NAME),
        "--raw-gdb", str(workspace / GDB_RAW_NAME),
        "--container-timeout", str(int(timeout)),
    ]
    helper = (os.getenv("BAMBOO_CORE_DUMP_PYTHON_GDB") or "").strip()
    if helper:
        argv += ["--python-gdb-helper", helper]
    return argv


def build_worker_argv(workspace: Path) -> list[str]:
    """Build the detached worker's command line.

    Args:
        workspace: Workspace the worker should operate on.

    Returns:
        The argument vector.
    """
    return [sys.executable, "-m", WORKER_MODULE, str(workspace)]


def spawn_worker(workspace: Path) -> int:
    """Launch the detached worker and return its pid.

    ``start_new_session=True`` is load-bearing: without it the worker shares
    the MCP server's process group and dies with it, discarding a
    part-finished gigabyte download on every server restart.

    Args:
        workspace: Workspace to hand to the worker.

    Returns:
        The worker's process id.

    Raises:
        OSError: When the process cannot be started.
    """
    log_path = workspace / WORKER_LOG_NAME
    handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - owned by the child
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            build_worker_argv(workspace),
            stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, cwd=str(workspace),
        )
    finally:
        handle.close()
    return int(process.pid)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def evidence_chars() -> int:
    """Return the character budget for the evidence handed to synthesis.

    Returns:
        Characters, from ``$BAMBOO_CORE_ANALYSIS_MAX_EVIDENCE_CHARS``.
    """
    return _env_int("BAMBOO_CORE_ANALYSIS_MAX_EVIDENCE_CHARS", DEFAULT_EVIDENCE_CHARS)


def load_core_evidence(workspace: Path, limit: int | None = None) -> dict[str, Any] | None:
    """Load the analyzer's evidence, shrunk to the prompt budget.

    The budget is applied here rather than downstream so the MCP payload is
    bounded at the same size the synthesis step already assumes, and step 5
    never has to shrink it a second time.

    This is the *only* place the budget is applied on this path: with
    ``--no-llm`` the analyzer never calls ``enforce_global_budget`` at all, so
    ``evidence.json`` on disk carries the full per-section evidence and
    ``gdb_raw.txt`` is not budgeted at any point.  Whatever this trims is
    trimmed from the model's copy alone and remains recoverable from the
    workspace.

    Args:
        workspace: Workspace directory.
        limit: Character budget, or ``None`` for :func:`evidence_chars`.

    Returns:
        ``{"core_evidence", "core_evidence_schema_version", "analyzer_version"}``,
        or ``None`` when the artifact is absent or unparsable.
    """
    budget = limit if limit is not None else evidence_chars()
    path = workspace / EVIDENCE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable evidence at %s: %s", path, exc)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), dict):
        logger.warning("Evidence at %s has no evidence object", path)
        return None
    evidence = enforce_global_budget(core_evidence_from_dict(payload["evidence"]), budget)
    return {
        "core_evidence": evidence.to_dict(),
        "core_evidence_schema_version": payload.get("schema_version"),
        "analyzer_version": payload.get("tool_version"),
    }


# ---------------------------------------------------------------------------
# State reconciliation
# ---------------------------------------------------------------------------


def reconcile_state(workspace: Path, manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    """Bring a stale manifest in line with what is actually running.

    Two cases, and only these two.  A worker that has *died* without recording
    anything leaves a non-terminal state and a pid that no longer exists; that
    is detected at once.  A worker that is alive but wedged is caught by the
    hard timeout instead.  The tool never signals a live worker — it records
    what it sees and leaves the process alone.

    Args:
        workspace: Workspace directory.
        manifest: Manifest as read from disk.
        root: Analysis root, for releasing the slot.

    Returns:
        The manifest, updated when a correction was applied.
    """
    state = str(manifest.get("state") or "")
    if state in TERMINAL_STATES:
        return manifest

    pid = manifest.get("worker_pid")
    if pid is not None and not pid_alive(pid):
        updated = mark_failed(workspace, (
            "The core-dump analysis worker exited without recording a result. "
            f"See {workspace / WORKER_LOG_NAME} for what it managed to log."
        ))
        release_slot(root, str(manifest.get("request_id") or ""))
        return updated

    if elapsed_s(manifest) > hard_timeout_s():
        updated = mark_failed(workspace, (
            f"The core-dump analysis for job {manifest.get('job_id')} passed its "
            f"{int(hard_timeout_s())}s deadline while in the '{state}' stage and has been "
            f"abandoned. The worker may still be running; see {workspace / WORKER_LOG_NAME}."
        ))
        release_slot(root, str(manifest.get("request_id") or ""))
        return updated

    return manifest


# ---------------------------------------------------------------------------
# Response construction
# ---------------------------------------------------------------------------


def _acquisition_block(manifest: dict[str, Any]) -> dict[str, Any]:
    """Summarise what the acquisition layer did.

    Args:
        manifest: Manifest dict.

    Returns:
        The ``acquisition`` sub-dict for the tool's evidence.
    """
    skipped = list(manifest.get("skipped") or [])
    return {
        "bytes_downloaded": int(manifest.get("bytes_downloaded") or 0),
        "fetched": list(manifest.get("fetched") or []),
        "created_empty": list(manifest.get("created_empty") or []),
        "skipped_count": len(skipped),
        "skipped_sample": skipped[:SKIPPED_SAMPLE_SIZE],
        "warnings": list(manifest.get("warnings") or []),
    }


def _running_text(manifest: dict[str, Any], seconds: float) -> str:
    """Build the summary for a run that has not finished.

    Args:
        manifest: Manifest dict.
        seconds: Elapsed seconds.

    Returns:
        A short status line naming the handle.
    """
    return (
        f"Core-dump analysis of job {manifest.get('job_id')} is still running "
        f"({manifest.get('state')}, {seconds:.0f}s elapsed): {manifest.get('progress')}. "
        f"Ask for the result when you are ready; the request ID is "
        f"`{manifest.get('request_id')}`."
    )


def _complete_text(manifest: dict[str, Any], seconds: float) -> str:
    """Build the summary for a finished run.

    Args:
        manifest: Manifest dict.
        seconds: Elapsed seconds.

    Returns:
        A short status line describing what was collected.
    """
    core = manifest.get("core") or {}
    fetched = len(manifest.get("fetched") or [])
    downloaded = _format_bytes(int(manifest.get("bytes_downloaded") or 0))
    warnings = manifest.get("warnings") or []
    caveat = f" {len(warnings)} acquisition warning(s) were recorded." if warnings else ""
    return (
        f"Core-dump analysis of job {manifest.get('job_id')} completed in {seconds:.0f}s. "
        f"gdb evidence was collected from `{core.get('relative_path', 'the core file')}` "
        f"({_format_bytes(int(core.get('size_bytes') or 0))}) with {fetched} job file(s) "
        f"fetched, {downloaded} in total.{caveat}"
    )


def build_response(
    manifest: dict[str, Any],
    workspace: Path,
    include_evidence: bool,
    replayed: bool = False,
) -> dict[str, Any]:
    """Build the tool's ``{"evidence", "text"}`` return payload.

    Args:
        manifest: Manifest dict.
        workspace: Workspace directory.
        include_evidence: Whether to embed the analyzer's evidence when the
            run is complete.
        replayed: True when this is a stored result rather than a run that just
            happened.  Marked because the two are otherwise indistinguishable
            to the reader: gdb did not run, so nothing about the host, the
            release container or the analyzer's own version reflects the
            present, and evidence collected before a fix will keep reporting
            the behaviour that fix addressed.

    Returns:
        The payload, ready to be JSON-serialised into MCP content.
    """
    state = str(manifest.get("state") or STATE_FAILED)
    seconds = elapsed_s(manifest)
    job_id = manifest.get("job_id")
    evidence: dict[str, Any] = {
        "job_id": job_id,
        "request_id": manifest.get("request_id"),
        "state": state,
        "elapsed_s": seconds,
        "progress": manifest.get("progress"),
        "failure_mode": manifest.get("failure_mode"),
        "mode_source": manifest.get("mode_source"),
        "workspace": str(workspace),
        "monitor_url": f"{get_base_url()}/job?pandaid={job_id}",
        "acquisition": _acquisition_block(manifest),
    }

    if state == STATE_FAILED:
        evidence["error"] = manifest.get("error")
        return {"evidence": evidence, "text": str(manifest.get("error") or "The analysis failed.")}

    if state != STATE_COMPLETE:
        return {"evidence": evidence, "text": _running_text(manifest, seconds)}

    if include_evidence:
        loaded = load_core_evidence(workspace)
        if loaded is None:
            evidence["error"] = (
                f"The analysis of job {job_id} reported success but its evidence file at "
                f"{workspace / EVIDENCE_NAME} is missing or unreadable."
            )
            evidence["state"] = STATE_FAILED
            return {"evidence": evidence, "text": evidence["error"]}
        evidence.update(loaded)
    evidence["replayed"] = replayed
    text = _complete_text(manifest, seconds)
    if replayed:
        finished = str(manifest.get("finished_utc") or manifest.get("updated_utc") or "")
        when = f" from {finished}" if finished else ""
        text = (
            f"Stored result{when} — gdb did not run again. "
            f"Pass restart=true to re-analyse the core in place.\n\n{text}"
        )
    return {"evidence": evidence, "text": text}


def _refusal(job_id: int, message: str) -> dict[str, Any]:
    """Build the payload for a request that was refused before any work.

    Args:
        job_id: PanDA job ID.
        message: Reason, shown verbatim.

    Returns:
        The payload.
    """
    return {
        "evidence": {
            "job_id": job_id,
            "state": STATE_FAILED,
            "error": message,
            "monitor_url": f"{get_base_url()}/job?pandaid={job_id}",
        },
        "text": message,
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _busy_text(holder: dict[str, Any]) -> str:
    """Describe the run currently holding the slot.

    Args:
        holder: Slot content.

    Returns:
        A deterministic refusal message.
    """
    started = parse_utc(str(holder.get("started_utc") or ""))
    age = ""
    if started is not None:
        age = f" for {datetime.now(timezone.utc).timestamp() - started:.0f}s"
    return (
        f"An analysis of job {holder.get('job_id')} has been running{age}. Only one "
        "core-dump analysis runs at a time, because each one holds a multi-gigabyte "
        "core and a container. Ask for its result, or try again once it finishes."
    )


async def _wait_inline(workspace: Path, root: Path, wait_s: float) -> dict[str, Any]:
    """Poll the manifest until the run finishes or the budget runs out.

    Args:
        workspace: Workspace directory.
        root: Analysis root.
        wait_s: Seconds to wait before returning a handle.

    Returns:
        The tool payload.
    """
    waited = 0.0
    manifest = read_manifest(workspace) or {}
    while True:
        manifest = reconcile_state(workspace, manifest, root)
        if str(manifest.get("state")) in TERMINAL_STATES:
            return build_response(manifest, workspace, include_evidence=True)
        if waited >= wait_s:
            return build_response(manifest, workspace, include_evidence=False)
        await asyncio.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
        manifest = read_manifest(workspace) or manifest


async def start_analysis(
    job_id: int,
    requested_mode: str = "auto",
    restart: bool = False,
    wait_s: float | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Start (or adopt, or re-use) an analysis of a job's core dump.

    Idempotent by design.  A run already in flight for the same job is
    adopted rather than duplicated, and a completed run is re-used, because
    the alternative is paying for the same gigabyte twice.  ``restart``
    forces a fresh run in the same workspace; a previously *failed* run is
    retried without needing it.

    Args:
        job_id: PanDA job ID.
        requested_mode: ``"auto"``, ``"hang"`` or ``"crash"``.
        restart: Ignore an existing complete or running result.
        wait_s: Inline wait override, or ``None`` for the configured default.
        root: Analysis root override, for tests.

    Returns:
        The tool payload.
    """
    root = root if root is not None else analysis_root()
    workspace = workspace_for(job_id, root)
    budget = wait_s if wait_s is not None else inline_wait_s()

    existing = read_manifest(workspace)
    if existing is not None and not restart:
        existing = reconcile_state(workspace, existing, root)
        state = str(existing.get("state"))
        if state == STATE_COMPLETE:
            return build_response(existing, workspace, include_evidence=True, replayed=True)
        if state != STATE_FAILED:
            return await _wait_inline(workspace, root, budget)

    ok, message = preflight_atlas_environment()
    if not ok:
        return _refusal(job_id, message)
    ok, message = check_quota(root)
    if not ok:
        return _refusal(job_id, message)

    request_id = uuid.uuid4().hex[:8]
    acquired, holder = acquire_slot(root, job_id, request_id)
    if not acquired:
        return _refusal(job_id, _busy_text(holder))

    workspace.mkdir(parents=True, exist_ok=True)
    write_manifest(workspace, new_manifest(job_id, request_id, requested_mode))
    try:
        pid = spawn_worker(workspace)
    except OSError as exc:
        release_slot(root, request_id)
        return build_response(
            mark_failed(workspace, f"The core-dump analysis worker could not be started: {exc}"),
            workspace, include_evidence=False,
        )
    bind_slot(root, request_id, pid)
    claim_worker(workspace, pid)
    return await _wait_inline(workspace, root, budget)


def status_analysis(
    job_id: int,
    request_id: str | None = None,
    include_evidence: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """Report on an existing analysis.

    Args:
        job_id: PanDA job ID.
        request_id: Run to report on; when given and it does not match the
            workspace's current run, that mismatch is reported rather than
            silently answering about a different run.
        include_evidence: Whether to embed the evidence on completion.
        root: Analysis root override, for tests.

    Returns:
        The tool payload.
    """
    root = root if root is not None else analysis_root()
    workspace = workspace_for(job_id, root)
    manifest = read_manifest(workspace)
    if manifest is None:
        if workspace.is_dir():
            return _refusal(job_id, (
                f"There is a core-dump analysis workspace for job {job_id} at {workspace}, "
                "but its run record is missing or unreadable. Start the analysis again."
            ))
        return _refusal(job_id, (
            f"No core-dump analysis has been started for job {job_id}."
        ))
    if request_id and str(manifest.get("request_id")) != request_id:
        return _refusal(job_id, (
            f"Request {request_id} is not the current analysis of job {job_id}; the "
            f"workspace now holds run {manifest.get('request_id')}."
        ))
    manifest = reconcile_state(workspace, manifest, root)
    return build_response(manifest, workspace, include_evidence=include_evidence)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for core_dump_analysis.

    Returns:
        Dict with ``name``, ``description``, ``inputSchema``, ``examples``
        and ``tags`` keys.
    """
    return {
        "name": "core_dump_analysis",
        "description": (
            "Analyse the core dump of a failed PanDA job. Reconstructs the job "
            "directory from BigPanDA, runs gdb inside the matching ATLAS release "
            "container, and returns deterministic evidence: the faulting or stalled "
            "thread's backtrace, thread groupings, loaded libraries, and how long the "
            "payload had been silent when the core was written. Use when the question "
            "asks what a job was actually doing when it was killed, or follows an "
            "offer of core-dump analysis after a looping-job (pilot code 1150) "
            "diagnosis. Takes about a minute; if it takes longer the call returns a "
            "request ID to ask about later."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": "PanDA job ID (pandaid) whose core dump to analyse.",
                },
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": (
                        "'start' begins or re-uses an analysis and waits briefly for it; "
                        "'status' reports progress; 'result' returns the evidence."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": (
                        "Analysis framing. 'auto' derives it from the job's pilot error "
                        "code: 1150 is a looping-job kill and analysed as a hang."
                    ),
                },
                "request_id": {
                    "type": "string",
                    "description": "Run identifier returned by a previous 'start' call.",
                },
                "restart": {
                    "type": "boolean",
                    "description": (
                        "Re-run even when a completed analysis of this job already "
                        "exists. Off by default, since a core is expensive to fetch."
                    ),
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "examples": [
            {"job_id": 7263525363, "action": "start"},
            {"job_id": 7263525363, "action": "result", "request_id": "5f2a91c4"},
        ],
        "tags": ["atlas", "panda", "core", "gdb", "hang", "looping", "diagnosis"],
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class CoreDumpAnalysisTool:
    """MCP tool for gdb analysis of a PanDA job's core dump.

    Returns deterministic evidence only; synthesis belongs to the executor,
    which owns the provider stack and the reconciliation step.
    """

    def __init__(self) -> None:
        """Initialise with the tool definition."""
        self._def: dict[str, Any] = get_definition()

    def get_definition(self) -> dict[str, Any]:
        """Return the MCP tool definition.

        Returns:
            Tool definition dictionary.
        """
        return self._def

    async def call(self, arguments: dict[str, Any]) -> list[Any]:
        """Run the requested action and return MCP content.

        ``bamboo.tools.base`` is imported here (deferred) so the rest of this
        module stays importable without bamboo core installed.

        Args:
            arguments: Dict with required ``job_id`` and optional ``action``,
                ``mode``, ``request_id`` and ``restart``.

        Returns:
            One-element MCP content list holding the JSON-serialised payload.
        """
        from bamboo.tools.base import text_content  # deferred — see docstring

        def _reply(payload: dict[str, Any]) -> list[Any]:
            return text_content(json.dumps(payload, default=str))

        if not isinstance(arguments, dict):
            return _reply({"evidence": {"error": "arguments must be a dict", "provided": repr(arguments)}})

        try:
            job_id = int(arguments["job_id"])
        except (KeyError, TypeError, ValueError):
            return _reply({"evidence": {"error": "job_id must be an integer", "provided": str(arguments)}})

        action = str(arguments.get("action") or "start")
        if action not in ACTIONS:
            return _reply({"evidence": {
                "error": f"action must be one of {', '.join(ACTIONS)}", "provided": action,
            }})
        mode = str(arguments.get("mode") or "auto")
        if mode not in MODES:
            return _reply({"evidence": {
                "error": f"mode must be one of {', '.join(MODES)}", "provided": mode,
            }})
        request_id = str(arguments.get("request_id") or "") or None

        try:
            if action == "start":
                payload = await start_analysis(
                    job_id, requested_mode=mode, restart=bool(arguments.get("restart")),
                )
            else:
                payload = await asyncio.to_thread(
                    status_analysis, job_id, request_id, action == "result",
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("Unexpected error in core-dump analysis of job %d", job_id)
            return _reply({
                "evidence": {"job_id": job_id, "state": STATE_FAILED, "error": repr(exc)},
                "text": f"Unexpected error while analysing the core dump of job {job_id}: {exc}",
            })
        return _reply(payload)


core_dump_analysis_tool = CoreDumpAnalysisTool()

__all__ = [
    "CoreDumpAnalysisTool",
    "acquire_slot",
    "bind_slot",
    "build_analyzer_argv",
    "build_response",
    "build_worker_argv",
    "check_quota",
    "claim_worker",
    "core_dump_analysis_tool",
    "elapsed_s",
    "get_definition",
    "load_core_evidence",
    "evidence_chars",
    "mark_failed",
    "new_manifest",
    "preflight_atlas_environment",
    "find_container_runtime",
    "reset_runtime_cache",
    "read_manifest",
    "reconcile_state",
    "release_slot",
    "resolve_failure_mode",
    "spawn_worker",
    "start_analysis",
    "status_analysis",
    "update_manifest",
    "workspace_for",
    "write_manifest",
]
