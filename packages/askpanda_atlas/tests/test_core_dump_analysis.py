"""Tests for the core-dump analysis state machine and tool shell.

No network, no gdb, no real subprocess.  An autouse fixture makes bare
``requests`` calls raise, and every spawn point is monkeypatched, so a path
that reaches the real HTTP layer or actually forks fails loudly rather than
hanging a test run.

The worker is driven by writing manifests directly, which is the same thing
the real worker does: the manifest file *is* the state store, so a fake that
writes one is not a simplification of the interface but the interface itself.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from askpanda_atlas import core_dump_analysis_impl as impl  # type: ignore[import]
from askpanda_atlas import _core_dump_worker as worker  # type: ignore[import]

JOB_ID = 7263525363


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


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any unmocked process launch fail loudly.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    import subprocess

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"unexpected subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the inline poll interval so waits do not dominate the suite.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setattr(impl, "POLL_INTERVAL_S", 0.01)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """Return an isolated analysis root.

    Args:
        tmp_path: pytest temporary directory.

    Returns:
        The root path.
    """
    path = tmp_path / "core-analysis"
    path.mkdir()
    return path


@pytest.fixture()
def workspace(root: Path) -> Path:
    """Return a workspace with a queued manifest already written.

    Args:
        root: Analysis root.

    Returns:
        The workspace path.
    """
    space = impl.workspace_for(JOB_ID, root)
    space.mkdir(parents=True)
    impl.write_manifest(space, impl.new_manifest(JOB_ID, "abcd1234", "auto"))
    return space


@pytest.fixture()
def atlas_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ATLAS environment preflight pass.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setattr(impl, "preflight_atlas_environment", lambda environ=None: (True, ""))


def _evidence_payload(observation: str = "the payload had been silent") -> dict[str, Any]:
    """Build a minimal analyzer evidence artifact.

    Args:
        observation: A deterministic observation to carry.

    Returns:
        The ``--json`` payload shape.
    """
    return {
        "schema_version": 1,
        "tool_version": "0.3.0",
        "evidence": {
            "mode": "hang",
            "core_file": {"path": "core.20115", "size": 1246000000},
            "observations": [observation],
            "primary_thread": {"backtrace": "XrdCl::Stream::Send()"},
        },
        "analysis": None,
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    """Manifest reading, writing and updating."""

    def test_round_trip_preserves_every_field(self, workspace: Path) -> None:
        """A written manifest reads back identically apart from the timestamp."""
        written = impl.read_manifest(workspace)
        assert written is not None
        assert written["job_id"] == JOB_ID
        assert written["state"] == impl.STATE_QUEUED
        assert written["manifest_version"] == impl.MANIFEST_VERSION

    def test_write_leaves_no_temporary_file_behind(self, workspace: Path) -> None:
        """The atomic rename must not leave the .tmp file in place."""
        impl.update_manifest(workspace, progress="working")
        assert not list(workspace.glob("*.tmp"))

    def test_update_preserves_unknown_keys(self, workspace: Path) -> None:
        """A manifest written by a newer version must not lose fields on rewrite."""
        impl.update_manifest(workspace, some_future_field={"nested": 1})
        impl.update_manifest(workspace, progress="later")
        manifest = impl.read_manifest(workspace)
        assert manifest is not None
        assert manifest["some_future_field"] == {"nested": 1}

    def test_truncated_manifest_reads_as_unknown_rather_than_raising(self, workspace: Path) -> None:
        """A half-written run record is a reason to report unknown, not to raise."""
        impl.manifest_path(workspace).write_text('{"job_id": 726352', encoding="utf-8")
        assert impl.read_manifest(workspace) is None

    def test_manifest_that_is_not_an_object_reads_as_unknown(self, workspace: Path) -> None:
        """A JSON array is valid JSON but not a run record."""
        impl.manifest_path(workspace).write_text("[1, 2, 3]", encoding="utf-8")
        assert impl.read_manifest(workspace) is None

    def test_absent_manifest_reads_as_unknown(self, root: Path) -> None:
        """A workspace that was never started has no manifest."""
        assert impl.read_manifest(impl.workspace_for(JOB_ID, root)) is None

    def test_mark_failed_sets_a_finish_time(self, workspace: Path) -> None:
        """A terminal failure stops the elapsed clock."""
        manifest = impl.mark_failed(workspace, "no core dump")
        assert manifest["state"] == impl.STATE_FAILED
        assert manifest["error"] == "no core dump"
        assert manifest["finished_utc"]

    def test_elapsed_is_zero_when_the_start_time_is_unreadable(self) -> None:
        """A manifest with no usable created_utc must not raise."""
        assert impl.elapsed_s({"created_utc": "not a timestamp"}) == 0.0

    def test_claim_worker_advances_a_queued_run(self, workspace: Path) -> None:
        """The normal case: the worker has not written anything yet."""
        manifest = impl.claim_worker(workspace, 4242)
        assert manifest["worker_pid"] == 4242
        assert manifest["state"] == impl.STATE_PREPARING

    @pytest.mark.parametrize(
        "state",
        [impl.STATE_DOWNLOADING, impl.STATE_ANALYZING, impl.STATE_COMPLETE, impl.STATE_FAILED],
    )
    def test_claim_worker_never_drags_a_run_backwards(self, workspace: Path, state: str) -> None:
        """The tool and the worker write the same file; claiming must not undo progress.

        A run dragged back to ``preparing`` after it has finished would never
        move forward again, because the process that would have advanced it has
        already exited.
        """
        impl.update_manifest(workspace, state=state)
        manifest = impl.claim_worker(workspace, 4242)
        assert manifest["state"] == state
        assert manifest["worker_pid"] == 4242


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class TestSlotLock:
    """The single-slot busy lock."""

    def test_first_acquire_succeeds(self, root: Path) -> None:
        """An unused slot is free."""
        acquired, holder = impl.acquire_slot(root, JOB_ID, "run1")
        assert acquired is True
        assert holder == {}

    def test_second_acquire_is_refused_while_the_holder_lives(self, root: Path) -> None:
        """The slot names this process, which is alive, so nothing else may start."""
        impl.acquire_slot(root, JOB_ID, "run1")
        acquired, holder = impl.acquire_slot(root, 999, "run2")
        assert acquired is False
        assert holder["request_id"] == "run1"
        assert holder["job_id"] == JOB_ID

    def test_a_dead_holder_is_taken_over(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A worker killed by an OOM or a reboot must not wedge the tool forever."""
        impl.acquire_slot(root, JOB_ID, "run1")
        monkeypatch.setattr(impl, "pid_alive", lambda pid: False)
        acquired, _holder = impl.acquire_slot(root, 999, "run2")
        assert acquired is True

    def test_release_frees_the_slot(self, root: Path) -> None:
        """After release the next request may start."""
        impl.acquire_slot(root, JOB_ID, "run1")
        impl.release_slot(root, "run1")
        acquired, _holder = impl.acquire_slot(root, 999, "run2")
        assert acquired is True

    def test_release_by_a_different_run_is_ignored(self, root: Path) -> None:
        """A late release from an abandoned run must not free somebody else's slot."""
        impl.acquire_slot(root, JOB_ID, "run1")
        impl.release_slot(root, "stale-run")
        acquired, holder = impl.acquire_slot(root, 999, "run2")
        assert acquired is False
        assert holder["request_id"] == "run1"

    def test_the_lock_file_is_never_removed(self, root: Path) -> None:
        """Releasing empties the slot in place; nothing in this package deletes a path."""
        impl.acquire_slot(root, JOB_ID, "run1")
        impl.release_slot(root, "run1")
        assert (root / impl.LOCK_NAME).is_file()

    def test_bind_transfers_ownership_to_the_worker(self, root: Path) -> None:
        """Once bound, the slot frees itself when the worker dies."""
        impl.acquire_slot(root, JOB_ID, "run1")
        impl.bind_slot(root, "run1", 424242)
        holder = json.loads((root / impl.LOCK_NAME).read_text(encoding="utf-8"))
        assert holder["pid"] == 424242

    def test_bind_from_a_different_run_is_ignored(self, root: Path) -> None:
        """A run that does not own the slot cannot repoint it at its own worker."""
        impl.acquire_slot(root, JOB_ID, "run1")
        impl.bind_slot(root, "other", 424242)
        holder = json.loads((root / impl.LOCK_NAME).read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()

    def test_a_corrupt_lock_file_is_treated_as_free(self, root: Path) -> None:
        """An unparsable slot must not make the tool permanently unusable."""
        (root / impl.LOCK_NAME).write_text("{not json", encoding="utf-8")
        acquired, _holder = impl.acquire_slot(root, JOB_ID, "run1")
        assert acquired is True


# ---------------------------------------------------------------------------
# ATLAS preflight
# ---------------------------------------------------------------------------


def _alrb(tmp_path: Path) -> Path:
    """Build a minimally complete ATLASLocalRootBase tree.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the fabricated ATLASLocalRootBase.
    """
    base = tmp_path / "cvmfs" / "ATLASLocalRootBase"
    (base / "user").mkdir(parents=True)
    (base / "user" / "atlasLocalSetup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    return base


def _no_runtime_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every container-runtime avenue miss.

    The login-shell probe is stubbed rather than allowed to run: a test that
    shells out inherits the CI host's profile scripts and would pass or fail
    on a property of the runner.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    impl.reset_runtime_cache()
    monkeypatch.setattr(impl.shutil, "which", lambda name: None)
    monkeypatch.setattr(impl, "_probe_login_shell_runtime", lambda: None)


class TestAtlasPreflight:
    """Environment checks that run before any download."""

    def test_missing_cvmfs_names_cvmfs(self, tmp_path: Path) -> None:
        """The first failure must name the mount, not the setup script."""
        ok, message = impl.preflight_atlas_environment(
            {"ATLAS_LOCAL_ROOT_BASE": str(tmp_path / "absent")}
        )
        assert ok is False
        assert "CVMFS" in message

    def test_missing_setup_script_names_the_script(self, tmp_path: Path) -> None:
        """A present but incomplete repository is a distinct failure."""
        base = tmp_path / "alrb"
        base.mkdir()
        ok, message = impl.preflight_atlas_environment({"ATLAS_LOCAL_ROOT_BASE": str(base)})
        assert ok is False
        assert "atlasLocalSetup.sh" in message

    def test_missing_runtime_names_every_avenue_tried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The container runtime is checked separately from the repository.

        The refusal must name all three avenues, because a reader who sees only
        "not on PATH" fixes the wrong PATH — that is the defect this replaced.
        """
        base = _alrb(tmp_path)
        _no_runtime_anywhere(monkeypatch)
        ok, message = impl.preflight_atlas_environment({"ATLAS_LOCAL_ROOT_BASE": str(base)})
        assert ok is False
        assert "apptainer" in message
        assert "login shell" in message
        assert "ALRB" in message

    def test_refusal_never_offers_a_local_gdb_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mismatched release resolves symbols wrongly; the refusal must say so."""
        base = _alrb(tmp_path)
        _no_runtime_anywhere(monkeypatch)
        _ok, message = impl.preflight_atlas_environment({"ATLAS_LOCAL_ROOT_BASE": str(base)})
        assert "will not fall back" in message

    def test_a_complete_environment_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All four checks satisfied."""
        base = _alrb(tmp_path)
        monkeypatch.setattr(impl.shutil, "which", lambda name: "/usr/bin/apptainer")
        assert impl.preflight_atlas_environment({"ATLAS_LOCAL_ROOT_BASE": str(base)}) == (True, "")


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


class TestFailureMode:
    """Resolution of the analysis framing."""

    def test_looping_job_code_resolves_to_hang(self) -> None:
        """1150 is the pilot's looping-job kill, which is the hang case."""
        mode, source = impl.resolve_failure_mode("auto", {"job": {"piloterrorcode": 1150}})
        assert mode == "hang"
        assert "1150" in source

    def test_other_pilot_codes_resolve_to_crash(self) -> None:
        """A payload failure is framed as a crash."""
        mode, source = impl.resolve_failure_mode("auto", {"job": {"piloterrorcode": 1305}})
        assert mode == "crash"
        assert "1305" in source

    def test_absent_metadata_resolves_to_crash(self) -> None:
        """No metadata means no evidence of looping, which is not evidence of hanging."""
        mode, _source = impl.resolve_failure_mode("auto", None)
        assert mode == "crash"

    def test_unparsable_pilot_code_does_not_raise(self) -> None:
        """A malformed metadata field must degrade, not fail the run."""
        mode, _source = impl.resolve_failure_mode("auto", {"job": {"piloterrorcode": "n/a"}})
        assert mode == "crash"

    @pytest.mark.parametrize("requested", ["hang", "crash"])
    def test_an_explicit_mode_overrides_the_pilot_code(self, requested: str) -> None:
        """A job whose code lies about what happened can still be analysed correctly."""
        mode, source = impl.resolve_failure_mode(requested, {"job": {"piloterrorcode": 1150}})
        assert mode == requested
        assert source == "requested explicitly"


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------


class TestAnalyzerArgv:
    """The analyzer command line, asserted element by element."""

    def _argv(self, tmp_path: Path) -> list[str]:
        """Build an argv against a temporary workspace.

        Args:
            tmp_path: pytest temporary directory.

        Returns:
            The argument vector.
        """
        job_dir = tmp_path / "job"
        return impl.build_analyzer_argv(
            job_dir / "core.20115", job_dir, tmp_path, "hang", container_timeout=600
        )

    def test_no_llm_is_always_passed(self, tmp_path: Path) -> None:
        """Synthesis belongs to the executor; the tool must never trigger an LLM call."""
        assert "--no-llm" in self._argv(tmp_path)

    def test_exe_is_never_passed(self, tmp_path: Path) -> None:
        """The analyzer resolves the ELF interpreter itself; overriding it risks
        substituting a same-named system binary and producing wrong symbols."""
        assert "--exe" not in self._argv(tmp_path)

    def test_quiet_is_never_passed(self, tmp_path: Path) -> None:
        """The analyzer's stderr progress is the only view of a running analysis."""
        argv = self._argv(tmp_path)
        assert "-q" not in argv and "--quiet" not in argv

    def test_execution_is_the_atlas_container(self, tmp_path: Path) -> None:
        """There is no local-gdb path."""
        argv = self._argv(tmp_path)
        assert argv[argv.index("--execution") + 1] == "atlas-container"

    def test_mode_is_resolved_not_auto(self, tmp_path: Path) -> None:
        """The analyzer's own --mode auto infers from the signal, a different question."""
        argv = self._argv(tmp_path)
        assert argv[argv.index("--mode") + 1] == "hang"

    def test_release_setup_defaults_into_the_job_directory(self, tmp_path: Path) -> None:
        """The acquisition layer guarantees my_release_setup.sh is there."""
        argv = self._argv(tmp_path)
        assert argv[argv.index("--release-setup") + 1].endswith("job/my_release_setup.sh")

    def test_artifacts_land_in_the_workspace_not_the_job_directory(self, tmp_path: Path) -> None:
        """Keeping outputs out of job/ leaves the reconstructed tree exactly as fetched."""
        argv = self._argv(tmp_path)
        assert argv[argv.index("--json") + 1] == str(tmp_path / impl.EVIDENCE_NAME)
        assert argv[argv.index("--raw-gdb") + 1] == str(tmp_path / impl.GDB_RAW_NAME)

    def test_container_timeout_is_below_the_analyzer_default(self, tmp_path: Path) -> None:
        """1800 s is far past any interactive caller's patience."""
        argv = self._argv(tmp_path)
        assert int(argv[argv.index("--container-timeout") + 1]) == 600

    def test_worker_argv_names_the_module_not_a_path(self, tmp_path: Path) -> None:
        """Spawning by module name is what keeps the worker out of the import graph."""
        argv = impl.build_worker_argv(tmp_path)
        assert argv[1:3] == ["-m", impl.WORKER_MODULE]


# ---------------------------------------------------------------------------
# State reconciliation
# ---------------------------------------------------------------------------


class TestReconcileState:
    """Correcting a manifest that no longer matches reality."""

    def test_a_terminal_state_is_left_alone(self, workspace: Path, root: Path) -> None:
        """Nothing reopens a finished run."""
        manifest = impl.mark_failed(workspace, "no core dump")
        assert impl.reconcile_state(workspace, manifest, root)["error"] == "no core dump"

    def test_a_dead_worker_becomes_a_failure(
        self, workspace: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that vanished without recording anything must not look like progress."""
        manifest = impl.update_manifest(workspace, state=impl.STATE_ANALYZING, worker_pid=999999)
        monkeypatch.setattr(impl, "pid_alive", lambda pid: False)
        updated = impl.reconcile_state(workspace, manifest, root)
        assert updated["state"] == impl.STATE_FAILED
        assert "without recording a result" in updated["error"]

    def test_a_dead_worker_releases_the_slot(
        self, workspace: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise one crashed run blocks every later one."""
        impl.acquire_slot(root, JOB_ID, "abcd1234")
        manifest = impl.update_manifest(workspace, state=impl.STATE_ANALYZING, worker_pid=999999)
        monkeypatch.setattr(impl, "pid_alive", lambda pid: False)
        impl.reconcile_state(workspace, manifest, root)
        acquired, _holder = impl.acquire_slot(root, 999, "next")
        assert acquired is True

    def test_a_live_but_wedged_worker_times_out(
        self, workspace: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hard deadline catches what a pid check cannot."""
        manifest = impl.update_manifest(
            workspace, state=impl.STATE_ANALYZING, worker_pid=os.getpid(),
            created_utc="2020-01-01T00:00:00Z",
        )
        monkeypatch.setattr(impl, "pid_alive", lambda pid: True)
        updated = impl.reconcile_state(workspace, manifest, root)
        assert updated["state"] == impl.STATE_FAILED
        assert "deadline" in updated["error"]

    def test_a_live_worker_within_its_deadline_is_untouched(
        self, workspace: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tool never signals or rewrites a healthy run."""
        manifest = impl.update_manifest(
            workspace, state=impl.STATE_DOWNLOADING, worker_pid=os.getpid()
        )
        monkeypatch.setattr(impl, "pid_alive", lambda pid: True)
        assert impl.reconcile_state(workspace, manifest, root)["state"] == impl.STATE_DOWNLOADING


# ---------------------------------------------------------------------------
# Evidence loading
# ---------------------------------------------------------------------------


class TestLoadEvidence:
    """Reading and bounding the analyzer's artifact."""

    def test_evidence_is_loaded_with_its_versions(self, workspace: Path) -> None:
        """Both version fields travel with the evidence so step 5 can key on them."""
        (workspace / impl.EVIDENCE_NAME).write_text(json.dumps(_evidence_payload()), encoding="utf-8")
        loaded = impl.load_core_evidence(workspace)
        assert loaded is not None
        assert loaded["analyzer_version"] == "0.3.0"
        assert loaded["core_evidence_schema_version"] == 1
        assert loaded["core_evidence"]["observations"] == ["the payload had been silent"]

    def test_an_absent_artifact_loads_as_none(self, workspace: Path) -> None:
        """A missing evidence file is reported, not raised."""
        assert impl.load_core_evidence(workspace) is None

    def test_an_unparsable_artifact_loads_as_none(self, workspace: Path) -> None:
        """A truncated write must not take the tool call down."""
        (workspace / impl.EVIDENCE_NAME).write_text('{"evidence": {', encoding="utf-8")
        assert impl.load_core_evidence(workspace) is None

    def test_an_artifact_without_an_evidence_object_loads_as_none(self, workspace: Path) -> None:
        """The wrapper is not the evidence."""
        (workspace / impl.EVIDENCE_NAME).write_text('{"schema_version": 1}', encoding="utf-8")
        assert impl.load_core_evidence(workspace) is None

    def test_oversized_evidence_is_shrunk_before_it_is_returned(self, workspace: Path) -> None:
        """Bounding here means the MCP payload matches the prompt budget already."""
        payload = _evidence_payload()
        payload["evidence"]["primary_thread"] = {"backtrace": "frame\n" * 40000}
        (workspace / impl.EVIDENCE_NAME).write_text(json.dumps(payload), encoding="utf-8")
        loaded = impl.load_core_evidence(workspace, limit=20_000)
        assert loaded is not None
        assert len(json.dumps(loaded["core_evidence"])) < len(json.dumps(payload["evidence"]))


# ---------------------------------------------------------------------------
# Response construction
# ---------------------------------------------------------------------------


class TestBuildResponse:
    """The tool's return payload for each state."""

    def test_a_running_run_reports_its_handle(self, workspace: Path) -> None:
        """The user needs the request ID to ask for the result later."""
        manifest = impl.update_manifest(
            workspace, state=impl.STATE_DOWNLOADING, progress="fetching core dump"
        )
        payload = impl.build_response(manifest, workspace, include_evidence=False)
        assert payload["evidence"]["state"] == impl.STATE_DOWNLOADING
        assert "abcd1234" in payload["text"]
        assert "fetching core dump" in payload["text"]

    def test_a_failed_run_surfaces_the_message_verbatim(self, workspace: Path) -> None:
        """JobPrepError messages are written to be shown as-is, not wrapped."""
        message = (
            "Job 7263525363 has no usable core dump in its job log, so there is "
            "nothing to analyse."
        )
        manifest = impl.mark_failed(workspace, message)
        payload = impl.build_response(manifest, workspace, include_evidence=True)
        assert payload["text"] == message
        assert payload["evidence"]["error"] == message

    def test_a_complete_run_embeds_the_evidence(self, workspace: Path) -> None:
        """The whole point of the result action."""
        (workspace / impl.EVIDENCE_NAME).write_text(json.dumps(_evidence_payload()), encoding="utf-8")
        manifest = impl.update_manifest(workspace, state=impl.STATE_COMPLETE, finished_utc=impl.utc_now())
        payload = impl.build_response(manifest, workspace, include_evidence=True)
        assert "core_evidence" in payload["evidence"]

    def test_status_of_a_complete_run_omits_the_evidence(self, workspace: Path) -> None:
        """A progress check must not drag a 50 kB payload through the transport."""
        (workspace / impl.EVIDENCE_NAME).write_text(json.dumps(_evidence_payload()), encoding="utf-8")
        manifest = impl.update_manifest(workspace, state=impl.STATE_COMPLETE)
        payload = impl.build_response(manifest, workspace, include_evidence=False)
        assert "core_evidence" not in payload["evidence"]

    def test_a_complete_run_whose_artifact_vanished_becomes_a_failure(self, workspace: Path) -> None:
        """Reporting success with no evidence would be the worst possible answer."""
        manifest = impl.update_manifest(workspace, state=impl.STATE_COMPLETE)
        payload = impl.build_response(manifest, workspace, include_evidence=True)
        assert payload["evidence"]["state"] == impl.STATE_FAILED
        assert "missing or unreadable" in payload["text"]

    def test_acquisition_warnings_are_always_carried(self, workspace: Path) -> None:
        """A log that could not be fetched changes what an absence of evidence means."""
        manifest = impl.update_manifest(
            workspace, state=impl.STATE_COMPLETE,
            warnings=["workDir/tmp.stdout.83d9: HTTP 404"],
        )
        (workspace / impl.EVIDENCE_NAME).write_text(json.dumps(_evidence_payload()), encoding="utf-8")
        payload = impl.build_response(manifest, workspace, include_evidence=True)
        assert payload["evidence"]["acquisition"]["warnings"] == ["workDir/tmp.stdout.83d9: HTTP 404"]
        assert "1 acquisition warning" in payload["text"]

    def test_the_skipped_list_is_sampled_but_counted_in_full(self, workspace: Path) -> None:
        """The audit trail stays complete on disk; only the sample travels."""
        skipped = [[f"workDir/usr/file{index}.log", "under workDir/usr"] for index in range(40)]
        manifest = impl.update_manifest(workspace, state=impl.STATE_DOWNLOADING, skipped=skipped)
        payload = impl.build_response(manifest, workspace, include_evidence=False)
        acquisition = payload["evidence"]["acquisition"]
        assert acquisition["skipped_count"] == 40
        assert len(acquisition["skipped_sample"]) == impl.SKIPPED_SAMPLE_SIZE
        # read_manifest returns None for an unreadable or half-written record,
        # so bind and assert rather than subscripting the union: an unreadable
        # manifest should fail here as a missing manifest, not as a TypeError
        # four frames down.
        stored = impl.read_manifest(workspace)
        assert stored is not None
        assert len(stored["skipped"]) == 40


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class TestStartAction:
    """The start action, including its inline wait."""

    def _spawn_that_completes(
        self, monkeypatch: pytest.MonkeyPatch, root: Path, state: str = impl.STATE_COMPLETE
    ) -> None:
        """Replace the spawn with a fake worker that finishes immediately.

        Args:
            monkeypatch: pytest monkeypatch fixture.
            root: Analysis root.
            state: Terminal state the fake worker records.
        """
        def _fake_spawn(workspace: Path) -> int:
            (workspace / impl.EVIDENCE_NAME).write_text(
                json.dumps(_evidence_payload()), encoding="utf-8"
            )
            manifest = impl.update_manifest(
                workspace, state=state, worker_pid=os.getpid(),
                finished_utc=impl.utc_now(), core={"relative_path": "core.20115", "size_bytes": 1246000000},
            )
            # The real worker releases the slot in its finally block; a fake
            # that does not would make every follow-up start report "busy".
            impl.release_slot(root, str(manifest.get("request_id") or ""))
            return os.getpid()

        monkeypatch.setattr(impl, "spawn_worker", _fake_spawn)

    def test_a_run_that_finishes_inline_never_shows_a_handle(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The common case: about a minute, answered in the same turn."""
        self._spawn_that_completes(monkeypatch, root)
        payload = asyncio.run(impl.start_analysis(JOB_ID, wait_s=1.0, root=root))
        assert payload["evidence"]["state"] == impl.STATE_COMPLETE
        assert "core_evidence" in payload["evidence"]
        assert "request ID" not in payload["text"]

    def test_a_slow_run_returns_a_handle(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exceeding the wait budget hands back a request ID rather than blocking."""
        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: os.getpid())
        payload = asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        assert payload["evidence"]["state"] not in impl.TERMINAL_STATES
        assert payload["evidence"]["request_id"]
        assert "request ID" in payload["text"]

    def test_a_run_that_completes_during_the_wait_is_picked_up(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The poll loop, not just the state at spawn time, decides the answer."""
        calls = {"n": 0}
        real_read = impl.read_manifest

        def _read(workspace: Path) -> dict[str, Any] | None:
            calls["n"] += 1
            if calls["n"] == 3:
                impl.update_manifest(
                    workspace, state=impl.STATE_COMPLETE, finished_utc=impl.utc_now()
                )
                (workspace / impl.EVIDENCE_NAME).write_text(
                    json.dumps(_evidence_payload()), encoding="utf-8"
                )
            return real_read(workspace)

        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: os.getpid())
        monkeypatch.setattr(impl, "read_manifest", _read)
        payload = asyncio.run(impl.start_analysis(JOB_ID, wait_s=5.0, root=root))
        assert payload["evidence"]["state"] == impl.STATE_COMPLETE

    def test_a_completed_run_is_re_used_rather_than_repeated(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-fetching a gigabyte to answer the same question twice is not acceptable."""
        self._spawn_that_completes(monkeypatch, root)
        first = asyncio.run(impl.start_analysis(JOB_ID, wait_s=1.0, root=root))
        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: pytest.fail("respawned"))
        second = asyncio.run(impl.start_analysis(JOB_ID, wait_s=1.0, root=root))
        assert second["evidence"]["request_id"] == first["evidence"]["request_id"]

    def test_restart_forces_a_new_run_over_a_completed_one(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch for a workspace whose artifacts are suspect."""
        self._spawn_that_completes(monkeypatch, root)
        first = asyncio.run(impl.start_analysis(JOB_ID, wait_s=1.0, root=root))
        second = asyncio.run(impl.start_analysis(JOB_ID, wait_s=1.0, restart=True, root=root))
        assert second["evidence"]["request_id"] != first["evidence"]["request_id"]

    def test_a_failed_run_is_retried_without_restart(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'Try again' should not need a flag."""
        self._spawn_that_completes(monkeypatch, root, state=impl.STATE_FAILED)
        first = asyncio.run(impl.start_analysis(JOB_ID, wait_s=1.0, root=root))
        self._spawn_that_completes(monkeypatch, root)
        second = asyncio.run(impl.start_analysis(JOB_ID, wait_s=1.0, root=root))
        assert second["evidence"]["state"] == impl.STATE_COMPLETE
        assert second["evidence"]["request_id"] != first["evidence"]["request_id"]

    def test_a_second_start_for_a_running_job_adopts_it(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two questions about the same job must not start two downloads."""
        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: os.getpid())
        first = asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: pytest.fail("respawned"))
        second = asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        assert second["evidence"]["request_id"] == first["evidence"]["request_id"]

    def test_a_start_for_a_different_job_is_refused_while_one_runs(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One core and one container at a time."""
        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: os.getpid())
        asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        payload = asyncio.run(impl.start_analysis(999, wait_s=0.02, root=root))
        assert payload["evidence"]["state"] == impl.STATE_FAILED
        assert "Only one" in payload["text"]
        assert str(JOB_ID) in payload["text"]

    def test_a_failing_environment_refuses_before_any_workspace_is_made(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A box without CVMFS must not download a gigabyte to discover that."""
        monkeypatch.setattr(
            impl, "preflight_atlas_environment", lambda environ=None: (False, "no CVMFS here")
        )
        payload = asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        assert payload["text"] == "no CVMFS here"
        assert not impl.workspace_for(JOB_ID, root).exists()

    def test_a_full_workspace_refuses_before_any_workspace_is_made(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing reaps, so the quota is what stops /tmp filling."""
        monkeypatch.setattr(impl, "quota_bytes", lambda: 1)
        (root / "ballast").write_bytes(b"0" * 4096)
        payload = asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        assert "ceiling" in payload["text"]
        assert not impl.workspace_for(JOB_ID, root).exists()

    def test_a_spawn_failure_releases_the_slot(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slot held by a worker that never existed would block everything after it."""
        def _boom(workspace: Path) -> int:
            raise OSError("fork failed")

        monkeypatch.setattr(impl, "spawn_worker", _boom)
        payload = asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        assert payload["evidence"]["state"] == impl.STATE_FAILED
        assert "could not be started" in payload["text"]
        acquired, _holder = impl.acquire_slot(root, 999, "next")
        assert acquired is True

    def test_the_requested_mode_is_recorded_for_the_worker(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tool has no metadata yet, so it records the request and the worker resolves it."""
        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: os.getpid())
        asyncio.run(impl.start_analysis(JOB_ID, requested_mode="crash", wait_s=0.02, root=root))
        manifest = impl.read_manifest(impl.workspace_for(JOB_ID, root))
        assert manifest is not None
        assert manifest["requested_mode"] == "crash"


class TestStatusAction:
    """The status and result actions."""

    def test_an_unstarted_job_says_so(self, root: Path) -> None:
        """Not started is a different answer from failed."""
        payload = impl.status_analysis(JOB_ID, root=root)
        assert "No core-dump analysis has been started" in payload["text"]

    def test_a_workspace_without_a_manifest_is_distinguished(self, root: Path) -> None:
        """A directory with no run record is a broken run, not an absent one."""
        impl.workspace_for(JOB_ID, root).mkdir(parents=True)
        payload = impl.status_analysis(JOB_ID, root=root)
        assert "run record is missing or unreadable" in payload["text"]

    def test_a_stale_request_id_is_reported_rather_than_answered(
        self, workspace: Path, root: Path
    ) -> None:
        """Answering about a different run under the asked-for ID would be a lie."""
        payload = impl.status_analysis(JOB_ID, request_id="oldrun", root=root)
        assert "is not the current analysis" in payload["text"]

    def test_the_matching_request_id_is_accepted(self, workspace: Path, root: Path) -> None:
        """The handle handed out by start must work."""
        payload = impl.status_analysis(JOB_ID, request_id="abcd1234", root=root)
        assert payload["evidence"]["request_id"] == "abcd1234"

    def test_result_embeds_the_evidence_and_status_does_not(
        self, workspace: Path, root: Path
    ) -> None:
        """The two actions differ in exactly one respect."""
        (workspace / impl.EVIDENCE_NAME).write_text(json.dumps(_evidence_payload()), encoding="utf-8")
        impl.update_manifest(workspace, state=impl.STATE_COMPLETE)
        assert "core_evidence" in impl.status_analysis(
            JOB_ID, include_evidence=True, root=root
        )["evidence"]
        assert "core_evidence" not in impl.status_analysis(
            JOB_ID, include_evidence=False, root=root
        )["evidence"]


# ---------------------------------------------------------------------------
# Nothing is deleted
# ---------------------------------------------------------------------------


class TestNothingIsDeleted:
    """The prohibition that makes a resumed retry possible."""

    def test_a_failed_run_keeps_every_artifact(self, workspace: Path, root: Path) -> None:
        """A .part file is resume input, and worker.log is the only record of why."""
        job_dir = workspace / impl.JOB_DIR_NAME
        job_dir.mkdir()
        (job_dir / "core.20115.part").write_bytes(b"partial")
        (workspace / impl.WORKER_LOG_NAME).write_text("attempt 1\n", encoding="utf-8")
        impl.mark_failed(workspace, "the transfer failed")
        assert (job_dir / "core.20115.part").is_file()
        assert (workspace / impl.WORKER_LOG_NAME).is_file()
        assert impl.manifest_path(workspace).is_file()

    def test_a_restart_re_uses_the_same_directory(
        self, root: Path, atlas_ok: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resume only works when the retry lands where the .part file is."""
        monkeypatch.setattr(impl, "spawn_worker", lambda workspace: os.getpid())
        asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        space = impl.workspace_for(JOB_ID, root)
        (space / "core.20115.part").write_bytes(b"partial")
        impl.mark_failed(space, "the transfer failed")
        asyncio.run(impl.start_analysis(JOB_ID, wait_s=0.02, root=root))
        assert (space / "core.20115.part").is_file()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class TestWorker:
    """The detached worker's own transitions."""

    def test_an_unusable_workspace_exits_distinctly(self, tmp_path: Path) -> None:
        """No manifest means nowhere to record a reason, so the exit code carries it."""
        assert worker.run(tmp_path) == worker.EXIT_NO_WORKSPACE

    def test_a_job_prep_error_is_recorded_verbatim(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The message is written for the user; wrapping it would spoil it."""
        message = "Job 7263525363 has no my_release_setup.sh in its job log."

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise worker.JobPrepError(message)

        monkeypatch.setattr(worker, "_prepare", _raise)
        assert worker.run(workspace) == 1
        manifest = impl.read_manifest(workspace)
        assert manifest is not None
        assert manifest["error"] == message

    def test_a_non_zero_analyzer_exit_is_a_failure_with_the_log_tail(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reader needs to see what gdb actually said."""
        (workspace / impl.WORKER_LOG_NAME).write_text(
            "setting up the release\ngdb: cannot open core\n", encoding="utf-8"
        )
        monkeypatch.setattr(worker, "_prepare", lambda *a, **k: _FakePrepared(workspace))
        monkeypatch.setattr(worker, "_run_analyzer", lambda *a, **k: (1, "gdb analysis failed (exit status 1)."))
        assert worker.run(workspace) == 1
        manifest = impl.read_manifest(workspace)
        assert manifest is not None
        assert manifest["analyzer_exit_code"] == 1

    def test_a_clean_exit_with_no_evidence_is_a_failure(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Success with no artifact is its own bug, not a success."""
        monkeypatch.setattr(worker, "_prepare", lambda *a, **k: _FakePrepared(workspace))
        monkeypatch.setattr(worker, "_run_analyzer", lambda *a, **k: (0, ""))
        assert worker.run(workspace) == 1
        manifest = impl.read_manifest(workspace)
        assert manifest is not None
        assert "wrote no evidence file" in manifest["error"]

    def test_a_stale_evidence_file_is_not_reported_as_this_run(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the failure mode that re-using the workspace introduces."""
        evidence = workspace / impl.EVIDENCE_NAME
        evidence.write_text(json.dumps(_evidence_payload("stale")), encoding="utf-8")
        os.utime(evidence, (1_600_000_000, 1_600_000_000))
        monkeypatch.setattr(worker, "_prepare", lambda *a, **k: _FakePrepared(workspace))
        monkeypatch.setattr(worker, "_run_analyzer", lambda *a, **k: (0, ""))
        assert worker.run(workspace) == 1
        manifest = impl.read_manifest(workspace)
        assert manifest is not None
        assert "did not rewrite its evidence" in manifest["error"]

    def test_a_successful_run_reaches_complete(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The happy path, end to end, with the subprocess boundary faked."""
        def _analyze(space: Path, prepared: Any, mode: str) -> tuple[int, str]:
            (space / impl.EVIDENCE_NAME).write_text(
                json.dumps(_evidence_payload()), encoding="utf-8"
            )
            return 0, ""

        monkeypatch.setattr(worker, "_prepare", lambda *a, **k: _FakePrepared(workspace))
        monkeypatch.setattr(worker, "_run_analyzer", _analyze)
        assert worker.run(workspace) == 0
        manifest = impl.read_manifest(workspace)
        assert manifest is not None
        assert manifest["state"] == impl.STATE_COMPLETE
        assert manifest["finished_utc"]

    def test_the_slot_is_released_on_every_exit_path(
        self, workspace: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that fails must not leave the tool unusable."""
        impl.acquire_slot(root, JOB_ID, "abcd1234")

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise worker.JobPrepError("no core dump")

        monkeypatch.setattr(worker, "_prepare", _raise)
        worker.run(workspace)
        acquired, _holder = impl.acquire_slot(root, 999, "next")
        assert acquired is True

    def test_an_unexpected_exception_is_recorded_rather_than_lost(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that dies silently is the one case status cannot explain."""
        def _raise(*args: Any, **kwargs: Any) -> None:
            raise ValueError("something unforeseen")

        monkeypatch.setattr(worker, "_prepare", _raise)
        assert worker.run(workspace) == 1
        manifest = impl.read_manifest(workspace)
        assert manifest is not None
        assert "failed unexpectedly" in manifest["error"]

    def test_the_log_tail_drops_blank_lines(self, workspace: Path) -> None:
        """A tail padded with blanks tells the reader nothing."""
        (workspace / impl.WORKER_LOG_NAME).write_text(
            "first\n\n\nsecond\n\n", encoding="utf-8"
        )
        assert worker.worker_log_tail(workspace, lines=2) == "first\nsecond"

    def test_the_log_tail_of_an_absent_log_is_empty(self, workspace: Path) -> None:
        """A failure before the log exists must not raise inside the failure handler."""
        assert worker.worker_log_tail(workspace) == ""


class _FakePrepared:
    """Stand-in for :class:`~askpanda_atlas._job_prep.PreparedJob`.

    Attributes:
        core_path: Path the analyzer would be pointed at.
        plan: Minimal plan object.
    """

    def __init__(self, workspace: Path) -> None:
        """Initialise against a workspace.

        Args:
            workspace: Workspace directory.
        """
        self.core_path: Path = workspace / impl.JOB_DIR_NAME / "core.20115"
        self.fetched: list[str] = ["payload.stdout"]
        self.created_empty: list[str] = ["payload.stderr"]
        self.warnings: list[str] = []
        self.bytes_downloaded: int = 774_000
        self.plan: Any = type("Plan", (), {"core": None, "skipped": []})()


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


class TestToolDefinition:
    """The MCP definition and argument handling."""

    def test_the_definition_declares_every_action(self) -> None:
        """The planner reads these enums."""
        schema = impl.get_definition()["inputSchema"]
        assert schema["properties"]["action"]["enum"] == list(impl.ACTIONS)
        assert schema["properties"]["mode"]["enum"] == list(impl.MODES)

    def test_only_job_id_is_required(self) -> None:
        """'Analyse the core dump of job N' must work with nothing else supplied."""
        assert impl.get_definition()["inputSchema"]["required"] == ["job_id"]

    def test_additional_properties_are_rejected(self) -> None:
        """A typo in an argument name should fail loudly at the schema."""
        assert impl.get_definition()["inputSchema"]["additionalProperties"] is False

    def test_a_non_integer_job_id_is_rejected(self) -> None:
        """The argument is used to build URLs and paths."""
        result = asyncio.run(impl.core_dump_analysis_tool.call({"job_id": "not a job"}))
        assert "job_id must be an integer" in json.loads(result[0]["text"])["evidence"]["error"]

    def test_a_missing_job_id_is_rejected(self) -> None:
        """No default job exists."""
        result = asyncio.run(impl.core_dump_analysis_tool.call({}))
        assert "job_id must be an integer" in json.loads(result[0]["text"])["evidence"]["error"]

    def test_non_dict_arguments_are_rejected(self) -> None:
        """The MCP contract says a dict; anything else is a caller bug."""
        result = asyncio.run(impl.core_dump_analysis_tool.call(["job_id"]))  # type: ignore[arg-type]
        assert "must be a dict" in json.loads(result[0]["text"])["evidence"]["error"]

    def test_an_unknown_action_is_rejected(self) -> None:
        """Silently defaulting to start could launch a gigabyte download."""
        result = asyncio.run(impl.core_dump_analysis_tool.call({"job_id": JOB_ID, "action": "abort"}))
        assert "action must be one of" in json.loads(result[0]["text"])["evidence"]["error"]

    def test_an_unknown_mode_is_rejected(self) -> None:
        """An unrecognised mode would change which files are fetched."""
        result = asyncio.run(impl.core_dump_analysis_tool.call({"job_id": JOB_ID, "mode": "loop"}))
        assert "mode must be one of" in json.loads(result[0]["text"])["evidence"]["error"]

    def test_status_of_an_unstarted_job_returns_cleanly(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The status action must not require the analysis root to exist."""
        monkeypatch.setattr(impl, "analysis_root", lambda: root)
        result = asyncio.run(
            impl.core_dump_analysis_tool.call({"job_id": JOB_ID, "action": "status"})
        )
        payload = json.loads(result[0]["text"])
        assert "No core-dump analysis has been started" in payload["text"]

    def test_an_unexpected_error_is_returned_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An MCP tool that raises takes the whole turn down."""
        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(impl, "status_analysis", _boom)
        result = asyncio.run(
            impl.core_dump_analysis_tool.call({"job_id": JOB_ID, "action": "status"})
        )
        assert "disk on fire" in json.loads(result[0]["text"])["text"]


class TestToolShell:
    """The delegating shell module."""

    def test_the_shell_re_exports_the_tool(self) -> None:
        """The entry point resolves through this module."""
        from askpanda_atlas import core_dump_analysis  # type: ignore[import]

        assert core_dump_analysis.core_dump_analysis_tool is impl.core_dump_analysis_tool

    def test_the_shell_has_no_fallback_implementation(self) -> None:
        """A degraded path would accept requests it could not serve."""
        assert not (
            Path(impl.__file__).parent / "_fallback_core_dump_analysis.py"
        ).exists()


class TestWorkerFailureMessage:
    """What the user reads when the analyzer exits non-zero."""

    def test_the_error_line_leads_the_message(self, tmp_path: Path) -> None:
        """The reason must not scroll off the top of a twenty-line tail.

        Job 7272161793 surfaced as ALRB's message-of-the-day and command menu
        with `Error: unable to source setupfile /srv/my_release_setup.sh`
        already out of the tail window.
        """
        from askpanda_atlas import _core_dump_worker as worker

        log = tmp_path / impl.WORKER_LOG_NAME
        log.write_text(
            "Error: unable to source setupfile /srv/my_release_setup.sh\n"
            + "lsetup root  ROOT data processing framework\n" * 40,
            encoding="utf-8",
        )
        assert worker._error_headline(tmp_path) == (
            "Error: unable to source setupfile /srv/my_release_setup.sh"
        )

    def test_a_log_without_an_error_line_yields_nothing(self, tmp_path: Path) -> None:
        """No match falls back to the tail alone — the pre-existing behaviour."""
        from askpanda_atlas import _core_dump_worker as worker

        (tmp_path / impl.WORKER_LOG_NAME).write_text("all fine\n", encoding="utf-8")
        assert worker._error_headline(tmp_path) == ""

    def test_an_unreadable_log_yields_nothing(self, tmp_path: Path) -> None:
        """A missing worker.log must not raise inside a failure path."""
        from askpanda_atlas import _core_dump_worker as worker

        assert worker._error_headline(tmp_path / "absent") == ""
