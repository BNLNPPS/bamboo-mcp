"""Tests for interfaces.shared.superuser_guard.

Covers:
- _has_py_filename: path-style and bare filename detection
- _has_inspection_intent: verb + keyword combinations
- is_superuser_question: full guard logic including tool registration check
- BAMBOO_SUPERUSER_PATTERNS env var injection
- BAMBOO_SUPERUSER_TOOLS env var injection
"""
from __future__ import annotations

import importlib
import sys
import types
from typing import Any, List

import pytest


# ---------------------------------------------------------------------------
# Helpers to reload the module with a fresh env (patterns are compiled once
# at import time, so we need to reimport to pick up monkeypatched env vars).
# ---------------------------------------------------------------------------

def _reload_guard() -> types.ModuleType:
    """Force-reload the superuser_guard module so env vars take effect."""
    mod_name = "interfaces.shared.superuser_guard"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


def _guard_is_superuser_question(guard: types.ModuleType, question: str, tool_names: List[str]) -> bool:
    """Call is_superuser_question on a dynamically reloaded guard module.

    Typed wrapper so pyright does not complain about unknown attributes on
    ``types.ModuleType``.

    Args:
        guard: Reloaded ``superuser_guard`` module.
        question: User question string.
        tool_names: Registered tool name list.

    Returns:
        Result of ``guard.is_superuser_question``.
    """
    fn: Any = getattr(guard, "is_superuser_question")
    return bool(fn(question, tool_names))


def _guard_tool_names(guard: types.ModuleType) -> frozenset[str]:
    """Return SUPERUSER_TOOL_NAMES from a dynamically reloaded guard module.

    Args:
        guard: Reloaded ``superuser_guard`` module.

    Returns:
        The ``SUPERUSER_TOOL_NAMES`` frozenset.
    """
    return frozenset(getattr(guard, "SUPERUSER_TOOL_NAMES"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REGISTERED_TOOLS: List[str] = ["code_query", "bamboo_answer", "panda_job_status"]
NO_SUPERUSER_TOOLS: List[str] = ["bamboo_answer", "panda_job_status", "panda_task_status"]


# ---------------------------------------------------------------------------
# _has_py_filename
# ---------------------------------------------------------------------------

def test_has_py_filename_slash_path() -> None:
    """pilot/util/processes.py triggers the path pattern."""
    from interfaces.shared.superuser_guard import _has_py_filename
    assert _has_py_filename("look at pilot/util/processes.py")


def test_has_py_filename_bare() -> None:
    """A bare filename like pilot.py triggers the pattern."""
    from interfaces.shared.superuser_guard import _has_py_filename
    assert _has_py_filename("look at pilot.py and explain how it works")


def test_has_py_filename_deep_path() -> None:
    """core/bamboo/tools/bamboo_executor.py triggers the path pattern."""
    from interfaces.shared.superuser_guard import _has_py_filename
    assert _has_py_filename("explain core/bamboo/tools/bamboo_executor.py")


def test_has_py_filename_no_match() -> None:
    """A question with no .py mention does not trigger."""
    from interfaces.shared.superuser_guard import _has_py_filename
    assert not _has_py_filename("why did task 12345 fail?")


def test_has_py_filename_partial_extension() -> None:
    """'.pyx' or '.pyc' do not match the .py word boundary."""
    from interfaces.shared.superuser_guard import _has_py_filename
    # .pyc is a compiled file — not a source path users would type
    # but we don't want false positives for typos
    # The pattern matches word-boundary so 'foo.pyc' should NOT match \b.py\b
    # because 'c' follows immediately — depends on regex engine word boundary
    # This is a documentation test: we accept either outcome but verify consistency.
    result = _has_py_filename("foo.pyc")
    # .pyc does NOT end at a word boundary after .py in the strict sense,
    # but the regex \b[\w][\w/]*\.py\b matches .py followed by end-of-word.
    # 'foo.pyc' — the 'c' is a word char so \b is NOT at .py→c boundary.
    assert result is False


# ---------------------------------------------------------------------------
# _has_inspection_intent
# ---------------------------------------------------------------------------

def test_inspection_intent_look_at_pilot() -> None:
    """'look at' + 'pilot' triggers intent detection."""
    from interfaces.shared.superuser_guard import _has_inspection_intent
    assert _has_inspection_intent("look at the pilot source and tell me if there's a bug")


def test_inspection_intent_explain_code() -> None:
    """'explain' + 'code' triggers intent detection."""
    from interfaces.shared.superuser_guard import _has_inspection_intent
    assert _has_inspection_intent("explain the code in this module")


def test_inspection_intent_review_bamboo() -> None:
    """'review' + 'bamboo' triggers intent detection."""
    from interfaces.shared.superuser_guard import _has_inspection_intent
    assert _has_inspection_intent("review the bamboo source for this issue")


def test_inspection_intent_no_verb() -> None:
    """'pilot' alone without an inspection verb does not trigger."""
    from interfaces.shared.superuser_guard import _has_inspection_intent
    assert not _has_inspection_intent("why is the pilot failing on this site?")


def test_inspection_intent_no_repo_keyword() -> None:
    """An inspection verb alone without a repo keyword does not trigger."""
    from interfaces.shared.superuser_guard import _has_inspection_intent
    assert not _has_inspection_intent("look at the queue status")


# ---------------------------------------------------------------------------
# is_superuser_question — full guard
# ---------------------------------------------------------------------------

def test_guard_blocks_slash_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path-style .py reference is blocked when a superuser tool is present."""
    from interfaces.shared.superuser_guard import is_superuser_question
    assert is_superuser_question(
        "Look at pilot/util/loopingjob.py and explain the algorithm",
        REGISTERED_TOOLS,
    )


def test_guard_blocks_bare_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare 'pilot.py' reference is blocked when a superuser tool is present."""
    from interfaces.shared.superuser_guard import is_superuser_question
    assert is_superuser_question(
        "Look at pilot.py and explain how it works",
        REGISTERED_TOOLS,
    )


def test_guard_blocks_inspection_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inspection verb + repo keyword is blocked when a superuser tool is present."""
    from interfaces.shared.superuser_guard import is_superuser_question
    assert is_superuser_question(
        "review the pilot source for potential race conditions",
        REGISTERED_TOOLS,
    )


def test_guard_passes_normal_question() -> None:
    """A normal job status question is not blocked."""
    from interfaces.shared.superuser_guard import is_superuser_question
    assert not is_superuser_question(
        "why did task 12345 fail?",
        REGISTERED_TOOLS,
    )


def test_guard_passes_when_no_superuser_tool_registered() -> None:
    """Guard never fires when no superuser tool is on the server."""
    from interfaces.shared.superuser_guard import is_superuser_question
    assert not is_superuser_question(
        "look at pilot/util/processes.py",
        NO_SUPERUSER_TOOLS,
    )


def test_guard_passes_empty_tool_list() -> None:
    """Guard never fires against an empty tool list (not yet connected)."""
    from interfaces.shared.superuser_guard import is_superuser_question
    assert not is_superuser_question("look at pilot/util/processes.py", [])


def test_guard_case_insensitive() -> None:
    """Detection is case-insensitive."""
    from interfaces.shared.superuser_guard import is_superuser_question
    assert is_superuser_question(
        "LOOK AT PILOT/UTIL/PROCESSES.PY AND EXPLAIN IT",
        REGISTERED_TOOLS,
    )


# ---------------------------------------------------------------------------
# BAMBOO_SUPERUSER_PATTERNS env var
# ---------------------------------------------------------------------------

def test_custom_pattern_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """BAMBOO_SUPERUSER_PATTERNS adds extra routing signals."""
    monkeypatch.setenv("BAMBOO_SUPERUSER_PATTERNS", r"bamboo/.*\.py")
    guard = _reload_guard()
    assert _guard_is_superuser_question(
        guard,
        "look at bamboo/core.py please",
        ["bamboo_code_query"] + list(_guard_tool_names(guard)),
    )


def test_invalid_custom_pattern_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid regex in BAMBOO_SUPERUSER_PATTERNS is skipped without crashing."""
    monkeypatch.setenv("BAMBOO_SUPERUSER_PATTERNS", r"[invalid(regex,valid\.py")
    guard = _reload_guard()
    # Should not raise; normal questions still work
    assert not _guard_is_superuser_question(guard, "why did my job fail?", NO_SUPERUSER_TOOLS)


# ---------------------------------------------------------------------------
# BAMBOO_SUPERUSER_TOOLS env var
# ---------------------------------------------------------------------------

def test_extra_tool_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """BAMBOO_SUPERUSER_TOOLS extends the gated tool set."""
    monkeypatch.setenv("BAMBOO_SUPERUSER_TOOLS", "bamboo_code_query")
    guard = _reload_guard()
    assert "bamboo_code_query" in _guard_tool_names(guard)


def test_extra_tool_triggers_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A question matching a signal is blocked when the extra tool is registered."""
    monkeypatch.setenv("BAMBOO_SUPERUSER_TOOLS", "bamboo_code_query")
    guard = _reload_guard()
    assert _guard_is_superuser_question(
        guard,
        "review the bamboo source code",
        ["bamboo_code_query"],
    )


def test_default_tools_always_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tools are present even when BAMBOO_SUPERUSER_TOOLS is set."""
    monkeypatch.setenv("BAMBOO_SUPERUSER_TOOLS", "some_other_tool")
    guard = _reload_guard()
    names = _guard_tool_names(guard)
    assert "code_query" in names
    assert "atlas.code_query" in names
    assert "some_other_tool" in names
