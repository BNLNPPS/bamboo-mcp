"""Tests for container-runtime detection in the core-dump preflight.

These cover the defect that motivated the module: ``preflight_atlas_environment``
asked ``shutil.which("apptainer")`` in the MCP server process, but the analyzer
starts the release container from ``bash -lc``, under a login shell whose PATH
the server never had.  On a host where CVMFS was healthy and the analysis would
have succeeded, the tool refused with "apptainer is not on PATH".

Every test that exercises detection stubs the login-shell probe.  Letting it
run would make the result depend on the CI runner's ``/etc/profile.d``, which
is exactly the host-specific property under test.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import askpanda_atlas.core_dump_analysis_impl as impl


@pytest.fixture(autouse=True)
def _clear_runtime_cache() -> Any:
    """Reset the memoised login-shell probe around every test.

    The cache is module-global and caches negatives, so one test's stub would
    otherwise decide the next test's answer.

    Yields:
        None.
    """
    impl.reset_runtime_cache()
    yield
    impl.reset_runtime_cache()


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


def _executable(path: Path) -> Path:
    """Create an executable stub file.

    Args:
        path: Destination path; parents are created.

    Returns:
        The same path, now an executable file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class TestFindContainerRuntime:
    """Avenue selection and precedence."""

    def test_server_path_is_used_when_it_hits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The free avenue is tried before any subprocess."""
        monkeypatch.setattr(impl.shutil, "which", lambda name: f"/usr/bin/{name}")

        def _explode() -> str | None:
            raise AssertionError("login-shell probe must not run when PATH hits")

        monkeypatch.setattr(impl, "_probe_login_shell_runtime", _explode)
        location, source = impl.find_container_runtime({}, None)
        assert location == "/usr/bin/apptainer"
        assert "PATH" in source

    def test_login_shell_rescues_a_narrow_server_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression case: absent from the daemon's PATH, present in a login shell."""
        monkeypatch.setattr(impl.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            impl, "_probe_login_shell_runtime", lambda: "/opt/apptainer/bin/apptainer"
        )
        location, source = impl.find_container_runtime({}, None)
        assert location == "/opt/apptainer/bin/apptainer"
        assert "login shell" in source

    def test_alrb_bundled_apptainer_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ALRB ships its own runtime; a host with none of its own is still fine."""
        base = _alrb(tmp_path)
        (base.parent / "containers" / "sw" / "apptainer").mkdir(parents=True)
        monkeypatch.setattr(impl.shutil, "which", lambda name: None)
        monkeypatch.setattr(impl, "_probe_login_shell_runtime", lambda: None)
        location, source = impl.find_container_runtime({}, base)
        assert location is not None
        assert location.endswith("containers/sw/apptainer")
        assert "ALRB" in source

    def test_singularity_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Older EL7 deployments resolve to singularity; either satisfies ALRB."""
        monkeypatch.setattr(
            impl.shutil,
            "which",
            lambda name: "/usr/bin/singularity" if name == "singularity" else None,
        )
        monkeypatch.setattr(impl, "_probe_login_shell_runtime", lambda: None)
        location, _source = impl.find_container_runtime({}, None)
        assert location == "/usr/bin/singularity"

    def test_override_wins_over_every_avenue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit path is an operator decision and outranks discovery."""
        binary = _executable(tmp_path / "custom" / "apptainer")
        monkeypatch.setattr(impl.shutil, "which", lambda name: "/usr/bin/apptainer")
        location, source = impl.find_container_runtime(
            {"BAMBOO_CORE_DUMP_APPTAINER": str(binary)}, None
        )
        assert location == str(binary)
        assert source == "BAMBOO_CORE_DUMP_APPTAINER"

    def test_unusable_override_falls_through_rather_than_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale override must not veto a runtime that is genuinely present."""
        monkeypatch.setattr(impl.shutil, "which", lambda name: "/usr/bin/apptainer")
        location, source = impl.find_container_runtime(
            {"BAMBOO_CORE_DUMP_APPTAINER": str(tmp_path / "gone")}, None
        )
        assert location == "/usr/bin/apptainer"
        assert "PATH" in source

    def test_all_avenues_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No runtime is a real outcome and must be reported as one."""
        monkeypatch.setattr(impl.shutil, "which", lambda name: None)
        monkeypatch.setattr(impl, "_probe_login_shell_runtime", lambda: None)
        assert impl.find_container_runtime({}, None) == (None, "")


class TestLoginShellProbe:
    """The probe itself — failure containment and caching."""

    def test_absolute_path_is_taken_from_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resolved path is accepted when it is executable."""
        monkeypatch.setattr(
            impl.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "/bin/sh\n", ""),
        )
        assert impl._probe_login_shell_runtime() == "/bin/sh"

    def test_a_bare_name_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``command -v`` can print a shell function name, which cannot be exec'd."""
        monkeypatch.setattr(
            impl.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "apptainer\n", ""),
        )
        assert impl._probe_login_shell_runtime() is None

    def test_a_hung_profile_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A diagnostic probe must never propagate into the tool."""

        def _timeout(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="bash", timeout=impl.RUNTIME_PROBE_TIMEOUT_S)

        monkeypatch.setattr(impl.subprocess, "run", _timeout)
        assert impl._probe_login_shell_runtime() is None

    def test_a_host_without_bash_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError from a missing shell is contained on the same terms."""

        def _missing(*_a: Any, **_k: Any) -> Any:
            raise FileNotFoundError("bash")

        monkeypatch.setattr(impl.subprocess, "run", _missing)
        assert impl._probe_login_shell_runtime() is None

    def test_the_negative_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One subprocess per process, not one per ``start``."""
        calls: list[int] = []

        def _counted(*a: Any, **_k: Any) -> Any:
            calls.append(1)
            return subprocess.CompletedProcess(a[0], 1, "", "")

        monkeypatch.setattr(impl.subprocess, "run", _counted)
        assert impl._probe_login_shell_runtime() is None
        assert impl._probe_login_shell_runtime() is None
        assert len(calls) == 1

    def test_reset_forces_a_reprobe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host that gains a runtime can be picked up without a restart."""
        results = ["", "/usr/bin/apptainer\n"]

        def _sequenced(*a: Any, **_k: Any) -> Any:
            return subprocess.CompletedProcess(a[0], 0, results.pop(0), "")

        monkeypatch.setattr(impl.subprocess, "run", _sequenced)
        monkeypatch.setattr(impl.os, "access", lambda *_a, **_k: True)
        assert impl._probe_login_shell_runtime() is None
        impl.reset_runtime_cache()
        assert impl._probe_login_shell_runtime() == "/usr/bin/apptainer"


class TestPreflightRuntimeIntegration:
    """How detection reaches the refusal text and the escape hatch."""

    def test_login_shell_runtime_makes_preflight_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact aipanda033 case: CVMFS fine, daemon PATH narrow, login shell fine."""
        base = _alrb(tmp_path)
        monkeypatch.setattr(impl.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            impl, "_probe_login_shell_runtime", lambda: "/usr/bin/apptainer"
        )
        assert impl.preflight_atlas_environment(
            {"ATLAS_LOCAL_ROOT_BASE": str(base)}
        ) == (True, "")

    def test_skip_flag_bypasses_only_the_runtime_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch must not also disable the CVMFS checks."""
        base = _alrb(tmp_path)
        monkeypatch.setattr(impl.shutil, "which", lambda name: None)
        monkeypatch.setattr(impl, "_probe_login_shell_runtime", lambda: None)
        env = {
            "ATLAS_LOCAL_ROOT_BASE": str(base),
            "BAMBOO_CORE_DUMP_SKIP_RUNTIME_CHECK": "1",
        }
        assert impl.preflight_atlas_environment(env) == (True, "")

        env["ATLAS_LOCAL_ROOT_BASE"] = str(tmp_path / "absent")
        ok, message = impl.preflight_atlas_environment(env)
        assert ok is False
        assert "CVMFS" in message

    def test_refusal_names_the_two_escape_hatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal the operator cannot act on is a support ticket."""
        base = _alrb(tmp_path)
        monkeypatch.setattr(impl.shutil, "which", lambda name: None)
        monkeypatch.setattr(impl, "_probe_login_shell_runtime", lambda: None)
        _ok, message = impl.preflight_atlas_environment(
            {"ATLAS_LOCAL_ROOT_BASE": str(base)}
        )
        assert "BAMBOO_CORE_DUMP_APPTAINER" in message
        assert "BAMBOO_CORE_DUMP_SKIP_RUNTIME_CHECK" in message
