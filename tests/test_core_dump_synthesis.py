"""Tests for core-dump synthesis in bamboo_executor.

The core-dump tool is the only one whose evidence does not go through
``_build_synthesis_prompt``: the analyzer owns its prompt pair and returns
JSON, which must pass through ``reconcile_llm_analysis`` before anyone reads
it.  These tests pin the parts of that path that fail silently if broken —
a non-terminal run answered with invented findings, evidence unwrapped one
level too far, or reconciliation skipped.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bamboo.tools.bamboo_executor as ex_mod
from bamboo.tools.bamboo_executor import (
    _CORE_DUMP_TOOL,
    _append_acquisition_warnings,
    _render_core_dump_markdown,
    _synthesise_core_dump,
)
from bamboo.tools.planner import Plan, PlanRoute, ReusePolicy, ToolCall

_JOB_ID = 7263525363


@pytest.fixture(autouse=True)
def _clear_evidence_store() -> Any:
    """Reset the module-global evidence store around every test.

    Yields:
        None.
    """
    ex_mod._last_evidence_store.clear()
    yield
    ex_mod._last_evidence_store.clear()


def _plan() -> Plan:
    """Build a minimal plan naming the core-dump tool.

    Returns:
        A validated Plan.
    """
    return Plan(
        route=PlanRoute.FAST_PATH,
        confidence=1.0,
        tool_calls=[ToolCall(
            tool=_CORE_DUMP_TOOL,
            arguments={"job_id": _JOB_ID, "action": "start", "mode": "hang"},
        )],
        reuse_policy=ReusePolicy(),
        explain="test",
    )


def _store(evidence: dict[str, Any], text: str = "progress line") -> None:
    """Store a core-dump payload exactly as ``_execute_one_tool`` would.

    One wrapping layer, not two: ``unpack_tool_result`` produces
    ``{"evidence": ..., "text": ...}`` and that is what lands in the store.

    Args:
        evidence: The tool's evidence dict.
        text: The tool's own summary line.
    """
    ex_mod._last_evidence_store[_CORE_DUMP_TOOL] = {
        "evidence": evidence, "text": text,
    }


def _complete_evidence(**extra: Any) -> dict[str, Any]:
    """Build a complete-state evidence dict.

    Args:
        **extra: Keys merged over the defaults.

    Returns:
        The evidence dict.
    """
    evidence: dict[str, Any] = {
        "job_id": _JOB_ID,
        "state": "complete",
        "failure_mode": "hang",
        "monitor_url": f"https://bigpanda.cern.ch/job?pandaid={_JOB_ID}",
        "analyzer_version": "0.3.0",
        "acquisition": {
            "bytes_downloaded": 1024,
            "fetched": ["core.18277", "payload.stdout"],
            "created_empty": [],
            "skipped_count": 0,
            "skipped_sample": [],
            "warnings": [],
        },
        "core_evidence": {"mode": "hang", "threads": []},
    }
    evidence.update(extra)
    return evidence


# ---------------------------------------------------------------------------
# Non-terminal bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "preparing", "downloading", "analyzing"])
async def test_a_running_analysis_returns_its_own_progress_line(state: str) -> None:
    """A run in flight is reported verbatim, with no LLM call.

    There is nothing to reason about yet. An LLM call here could only
    paraphrase a status message, and at worst would invent findings for an
    analysis that has not produced any.
    """
    _store({"job_id": _JOB_ID, "state": state}, text="still running, ID abc123")
    with patch.object(ex_mod, "call_llm", new_callable=AsyncMock) as mock_llm:
        result = await _synthesise_core_dump(_plan(), [_CORE_DUMP_TOOL])
    assert result == "still running, ID abc123"
    assert mock_llm.await_count == 0


@pytest.mark.asyncio
async def test_a_failed_analysis_returns_its_own_error_line() -> None:
    """A failure is deterministic text and must not be re-narrated."""
    _store(
        {"job_id": _JOB_ID, "state": "failed", "error": "worker exited"},
        text="The analysis failed: worker exited",
    )
    with patch.object(ex_mod, "call_llm", new_callable=AsyncMock) as mock_llm:
        result = await _synthesise_core_dump(_plan(), [_CORE_DUMP_TOOL])
    assert result == "The analysis failed: worker exited"
    assert mock_llm.await_count == 0


@pytest.mark.asyncio
async def test_complete_without_core_evidence_falls_through() -> None:
    """A complete run with no evidence defers to the generic path."""
    _store(_complete_evidence(core_evidence=None))
    result = await _synthesise_core_dump(_plan(), [_CORE_DUMP_TOOL])
    assert result is None


# ---------------------------------------------------------------------------
# Evidence unwrapping
# ---------------------------------------------------------------------------


def test_evidence_is_unwrapped_exactly_one_level() -> None:
    """One ``.get("evidence")`` reaches the dict, not two.

    The double unwrap documented for ``bamboo_last_evidence`` applies to that
    tool's own response, which wraps this store entry a second time. Applying
    it here yields None and silently disables synthesis, which has bitten this
    codebase twice.
    """
    _store(_complete_evidence())
    evidence = ex_mod._core_dump_evidence()
    assert evidence["state"] == "complete"
    assert evidence["job_id"] == _JOB_ID


def test_missing_store_entry_yields_an_empty_dict() -> None:
    """No stored call is not an error."""
    assert ex_mod._core_dump_evidence() == {}


# ---------------------------------------------------------------------------
# Reconciliation and warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_is_always_called_on_the_model_output() -> None:
    """Reconciliation is not optional.

    It is what stops the model reading EventLoop completion markers as
    evidence that a looping job exited normally. If this test is ever
    loosened, that protection is gone.
    """
    _store(_complete_evidence())
    analysis = {"verdict": "the job hung", "classification": "hang"}
    fake_module = MagicMock()
    fake_module.build_system_prompt.return_value = "sys"
    fake_module.build_user_prompt.return_value = "usr"
    fake_module.core_evidence_from_dict.return_value = MagicMock()
    fake_module.extract_json_object.return_value = dict(analysis)
    fake_module.reconcile_llm_analysis.return_value = {
        **analysis, "verdict": "reconciled verdict",
    }

    with patch.dict(
        "sys.modules", {"askpanda_atlas._core_dump_analyzer": fake_module},
    ), patch.object(ex_mod, "call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = json.dumps(analysis)
        result = await _synthesise_core_dump(_plan(), [_CORE_DUMP_TOOL])

    assert fake_module.reconcile_llm_analysis.call_count == 1
    # _synthesise_core_dump returns str | None, where None means "fall through
    # to generic synthesis". Assert the happy path explicitly: a None here is a
    # silently disabled synthesis path, which is exactly the failure this test
    # exists to catch.
    assert result is not None
    assert "reconciled verdict" in result


@pytest.mark.asyncio
async def test_the_analyzer_mode_drives_the_system_prompt() -> None:
    """``failure_mode`` selects the crash or hang prompt, not a default."""
    _store(_complete_evidence(failure_mode="crash"))
    fake_module = MagicMock()
    fake_module.extract_json_object.return_value = {"verdict": "v"}
    fake_module.reconcile_llm_analysis.side_effect = lambda _e, a: a

    with patch.dict(
        "sys.modules", {"askpanda_atlas._core_dump_analyzer": fake_module},
    ), patch.object(ex_mod, "call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "{}"
        await _synthesise_core_dump(_plan(), [_CORE_DUMP_TOOL])

    fake_module.build_system_prompt.assert_called_once_with("crash")


def test_acquisition_warnings_are_appended_to_limitations() -> None:
    """Decision D3's payload lands in limitations, deterministically."""
    evidence = _complete_evidence()
    evidence["acquisition"]["warnings"] = ["workDir listing was truncated"]
    analysis = _append_acquisition_warnings({"limitations": ["no symbols"]}, evidence)
    assert analysis["limitations"] == ["no symbols", "workDir listing was truncated"]


def test_warnings_survive_a_missing_limitations_key() -> None:
    """A model that omitted limitations still gets the warnings."""
    evidence = _complete_evidence()
    evidence["acquisition"]["warnings"] = ["core was truncated"]
    analysis = _append_acquisition_warnings({}, evidence)
    assert analysis["limitations"] == ["core was truncated"]


def test_no_warnings_leaves_limitations_untouched() -> None:
    """The common case adds nothing."""
    analysis = _append_acquisition_warnings({"limitations": ["a"]}, _complete_evidence())
    assert analysis["limitations"] == ["a"]


@pytest.mark.asyncio
async def test_warnings_are_appended_after_reconciliation() -> None:
    """Ordering matters: reconciliation may rewrite the limitations list.

    Appending first would let ``reconcile_llm_analysis`` filter an acquisition
    warning back out, since it drops list entries that read as claims of
    normal job success.
    """
    evidence = _complete_evidence()
    evidence["acquisition"]["warnings"] = ["a truncated fetch"]
    _store(evidence)

    seen: dict[str, Any] = {}
    fake_module = MagicMock()
    fake_module.extract_json_object.return_value = {"verdict": "v"}

    def _reconcile(_evidence: Any, analysis: dict[str, Any]) -> dict[str, Any]:
        seen["limitations_at_reconcile"] = list(analysis.get("limitations", []))
        analysis["limitations"] = ["from reconciliation"]
        return analysis

    fake_module.reconcile_llm_analysis.side_effect = _reconcile

    with patch.dict(
        "sys.modules", {"askpanda_atlas._core_dump_analyzer": fake_module},
    ), patch.object(ex_mod, "call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "{}"
        result = await _synthesise_core_dump(_plan(), [_CORE_DUMP_TOOL])

    assert seen["limitations_at_reconcile"] == []
    assert result is not None
    assert "from reconciliation" in result
    assert "a truncated fetch" in result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_includes_the_headline_fields() -> None:
    """Verdict, classification and cause all reach the output."""
    body = _render_core_dump_markdown(
        {
            "verdict": "The payload stalled in a remote file close.",
            "classification": "hang",
            "confidence": "high",
            "confidence_reason": "symbols resolved",
            "likely_cause": "An xrootd shutdown hang.",
            "culprit_component": "xrootd",
            "supporting_evidence": ["thread 3 in XrdCl::File::Close"],
            "next_steps": ["retry at another site"],
        },
        _complete_evidence(),
    )
    assert "The payload stalled in a remote file close." in body
    assert "`hang`" in body
    assert "high" in body
    assert "xrootd" in body
    assert "- thread 3 in XrdCl::File::Close" in body
    assert "- retry at another site" in body


def test_render_omits_absent_and_unknown_fields() -> None:
    """An "unknown" culprit is noise and is dropped."""
    body = _render_core_dump_markdown(
        {"verdict": "v", "classification": "undetermined", "culprit_component": "unknown"},
        _complete_evidence(),
    )
    assert "responsible component" not in body
    assert "Supporting evidence" not in body
    assert "Limitations" not in body


def test_render_links_the_monitor_url() -> None:
    """The BigPanDA link is built from evidence, never from the model."""
    body = _render_core_dump_markdown({"verdict": "v"}, _complete_evidence())
    assert f"https://bigpanda.cern.ch/job?pandaid={_JOB_ID}" in body


# ---------------------------------------------------------------------------
# Presentation-key handling
# ---------------------------------------------------------------------------


def test_core_dump_offer_is_hidden_from_the_synthesis_llm() -> None:
    """The offer is appended programmatically, so the LLM must not see it.

    Showing a ready-made offer string to the model reliably beats an
    instruction not to reproduce it — that is what happened to
    ``code_analysis_offer_md`` and produced a duplicated offer.
    """
    cleaned = ex_mod._strip_presentation_keys({
        "evidence": {"job_id": 1, "core_dump_offer_md": "Analyse it?"},
        "text": "t",
    })
    assert "core_dump_offer_md" not in cleaned["evidence"]
    assert cleaned["evidence"]["job_id"] == 1


def test_core_dump_offer_is_read_back_from_the_store() -> None:
    """The store keeps the key so the post-synthesis append can find it."""
    ex_mod._last_evidence_store["panda_log_analysis"] = {
        "evidence": {"job_id": _JOB_ID, "core_dump_offer_md": "\n\nAnalyse it?"},
    }
    assert ex_mod._log_analysis_core_dump_offer_md() == "\n\nAnalyse it?"
