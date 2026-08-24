"""Tests for core-dump analysis routing — rules 1c and 1d.

Two entry points are covered, and they are deliberately tested through
different layers because they live in different ones:

* **Rule 1c** (explicit request naming a job) is a ``_build_deterministic_plan``
  rule and is tested by inspecting the plan it returns.
* **Rule 1d** (bare affirmative answering a stored offer) is a ``_route``
  branch, placed ahead of the social intercept and the topic guard, and is
  tested end-to-end through ``_route`` — testing it at the plan layer would
  pass while the shipped path still answered "You're welcome".
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import bamboo.tools.bamboo_executor as ex_mod
from bamboo.tools.bamboo_answer import (
    _build_deterministic_plan,
    _is_core_dump_affirmative,
    _is_core_dump_request,
    bamboo_answer_tool,
)

_CORE_DUMP_TOOL = "atlas.core_dump_analysis"
_JOB_ID = 7263525363


@pytest.fixture(autouse=True)
def _clear_evidence_store() -> Any:
    """Reset the executor's evidence store around every test.

    The store is module-global, so a stored offer would otherwise leak into
    unrelated tests and make rule 1d fire where it should not.

    Yields:
        None.
    """
    ex_mod._last_evidence_store.clear()
    yield
    ex_mod._last_evidence_store.clear()


def _store_offer(offer_md: str = "\n\nA core dump is present. Analyse it?") -> None:
    """Place a panda_log_analysis result carrying a core-dump offer.

    Args:
        offer_md: The offer Markdown to store; empty means no offer.
    """
    ex_mod._last_evidence_store["panda_log_analysis"] = {
        "evidence": {
            "job_id": _JOB_ID,
            "failure_type": "looping_job",
            "core_dump_offer_md": offer_md,
        },
        "text": "Job was killed as a looping job.",
    }


# ---------------------------------------------------------------------------
# Signal predicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question", [
    "analyse the core dump of job 7263525363",
    "can you run gdb on job 7263525363",
    "show me the backtrace for job 7263525363",
    "what was job 7263525363 stuck on?",
    "what was job 7263525363 actually doing?",
    "where was job 7263525363 stuck?",
])
def test_core_dump_signals_match_explicit_requests(question: str) -> None:
    """Explicit core-dump phrasings are recognised."""
    assert _is_core_dump_request(question) is True


@pytest.mark.parametrize("question", [
    "why did job 7263525363 fail?",
    "what is the status of job 7263525363",
    "show me the pilot source for job 7263525363",
    "why did job 7263525363 hang?",
])
def test_core_dump_signals_ignore_unrelated_requests(question: str) -> None:
    """Ordinary failure questions do not trigger core-dump routing.

    "Why did job X hang" is in this list on purpose: it is a diagnosis
    request, and sending it straight to a multi-gigabyte core fetch would be
    the wrong opening move. It goes to panda_log_analysis, which answers it or
    makes the offer that rule 1d picks up.
    """
    assert _is_core_dump_request(question) is False


@pytest.mark.parametrize("question", [
    "yes",
    "Yes please",
    "yes, please",
    "ok",
    "okay, go ahead",
    "sure",
    "go ahead",
    "do it",
    "please analyse it",
    "analyze it",
    "yep!",
])
def test_affirmatives_are_recognised(question: str) -> None:
    """Bare affirmatives of several shapes all match."""
    assert _is_core_dump_affirmative(question) is True


@pytest.mark.parametrize("question", [
    "yes but what about the stage-out errors",
    "no",
    "not yet",
    "yes, and also show me job 123",
    "why did it hang",
    "",
])
def test_affirmatives_reject_anything_with_content(question: str) -> None:
    """A message carrying its own question is never treated as a bare yes.

    This is the guard that keeps rule 1d from hijacking a follow-up that
    happens to begin with "yes".
    """
    assert _is_core_dump_affirmative(question) is False


# ---------------------------------------------------------------------------
# Rule 1c — explicit request
# ---------------------------------------------------------------------------


def test_rule_1c_routes_an_explicit_request_to_the_core_dump_tool() -> None:
    """A job ID plus core-dump keywords produces a core-dump plan."""
    plan = _build_deterministic_plan(
        "analyse the core dump of job 7263525363", None, _JOB_ID,
    )
    assert plan is not None
    assert [tc.tool for tc in plan.tool_calls] == [_CORE_DUMP_TOOL]
    assert plan.tool_calls[0].arguments["job_id"] == _JOB_ID
    assert plan.tool_calls[0].arguments["action"] == "start"


def test_rule_1c_uses_auto_mode() -> None:
    """An explicit request may name any job, so the mode is left to the tool.

    Contrast with rule 1d, which pins ``hang`` because the offer it answers is
    only ever made for a looping-job kill.
    """
    plan = _build_deterministic_plan(
        "analyse the core dump of job 7263525363", None, _JOB_ID,
    )
    assert plan is not None
    assert plan.tool_calls[0].arguments["mode"] == "auto"


def test_rule_1c_precedes_log_analysis() -> None:
    """A core-dump request that also matches the log-analysis pattern wins.

    "Analyse the core dump of job X" contains "analys" + a job ID, so it
    matches ``_is_log_analysis_request`` too. Without rule 1c sitting ahead of
    rule 1 this would silently re-run panda_log_analysis — the very tool whose
    output prompted the request.
    """
    from bamboo.tools.bamboo_answer import _is_log_analysis_request
    question = "analyse the core dump of job 7263525363"
    assert _is_log_analysis_request(question) is True
    plan = _build_deterministic_plan(question, None, _JOB_ID)
    assert plan is not None
    assert plan.tool_calls[0].tool == _CORE_DUMP_TOOL


def test_rule_1c_does_not_fire_without_a_job_id() -> None:
    """Core-dump keywords alone are not enough to route."""
    plan = _build_deterministic_plan("what does a core dump tell you?", None, None)
    assert plan is None or plan.tool_calls[0].tool != _CORE_DUMP_TOOL


def test_rule_1c_is_atlas_only() -> None:
    """The ePIC mirror has no core-dump tool, so the rule must not fire there.

    ``_CORE_DUMP_ANALYSIS_AVAILABLE`` is False in ``askpanda_epic``; naming the
    tool anyway would produce "Unknown tool" rather than a useful answer.
    """
    plan = _build_deterministic_plan(
        "analyse the core dump of job 7263525363", None, _JOB_ID, plugin_id="epic",
    )
    assert plan is not None
    assert plan.tool_calls[0].tool != _CORE_DUMP_TOOL


# ---------------------------------------------------------------------------
# Rule 1d — affirmative answering a stored offer
# ---------------------------------------------------------------------------


async def _route_question(
    question: str, bypass_fast_path: bool = False,
) -> tuple[Any, Any]:
    """Drive ``_route`` with execute_plan patched out.

    Args:
        question: The user message to route.
        bypass_fast_path: Value of the flag the TUI derives from ``/fastpath``.

    Returns:
        Tuple of ``(result, execute_plan_mock)``.
    """
    with patch(
        "bamboo.tools.bamboo_answer.execute_plan", new_callable=AsyncMock,
    ) as mock_exec, patch(
        "bamboo.tools.bamboo_answer._run_topic_guard", new_callable=AsyncMock,
    ) as mock_guard, patch(
        "bamboo.tools.bamboo_answer.bamboo_plan_tool.call", new_callable=AsyncMock,
    ) as mock_plan:
        mock_exec.return_value = [{"type": "text", "text": "analysed"}]
        # The guard needs an LLM; stub it to pass the question through
        # unchanged so a fall-through reaches the planner rather than raising.
        mock_guard.side_effect = lambda q, h: (q, False)
        mock_plan.return_value = [{"type": "text", "text": "planner"}]
        result = await bamboo_answer_tool._route(
            question, [], False, bypass_fast_path, False, False,
        )
    return result, mock_exec


@pytest.mark.asyncio
async def test_rule_1d_routes_a_bare_yes_to_the_core_dump_tool() -> None:
    """"Yes" after an offer starts the analysis of the offered job."""
    _store_offer()
    _, mock_exec = await _route_question("yes please")
    assert mock_exec.await_count == 1
    plan = mock_exec.await_args.args[0]
    assert plan.tool_calls[0].tool == _CORE_DUMP_TOOL
    assert plan.tool_calls[0].arguments["job_id"] == _JOB_ID


@pytest.mark.asyncio
async def test_rule_1d_pins_hang_mode() -> None:
    """The offer only ever follows pilot code 1150, so the mode is stated.

    Passing it explicitly removes a metadata round trip and the failure mode
    where that fetch fails and leaves the framing unresolved.
    """
    _store_offer()
    _, mock_exec = await _route_question("yes")
    assert mock_exec.await_args.args[0].tool_calls[0].arguments["mode"] == "hang"


@pytest.mark.asyncio
async def test_rule_1d_beats_the_ack_intercept() -> None:
    """"Ok" answering an offer must not be answered with "You're welcome".

    ``_is_ack`` matches "ok", "okay", "great", "perfect" and "sounds good".
    This is the whole reason rule 1d sits ahead of the social intercept, and
    this test is the entire guard on that ordering.
    """
    _store_offer()
    result, mock_exec = await _route_question("ok")
    assert mock_exec.await_count == 1
    assert "welcome" not in result[0]["text"].lower()


@pytest.mark.asyncio
async def test_an_affirmative_without_an_offer_is_still_an_ack() -> None:
    """With no offer stored, "ok" keeps its ordinary meaning."""
    result, mock_exec = await _route_question("ok")
    assert mock_exec.await_count == 0
    assert "welcome" in result[0]["text"].lower()


@pytest.mark.asyncio
async def test_an_empty_offer_does_not_arm_rule_1d() -> None:
    """A log analysis that made no offer leaves the affirmative unrouted.

    ``_build_core_dump_offer`` returns "" for a non-1150 failure, a missing
    core, or a zero-byte one, so the empty string is the real-world signal
    that no offer is outstanding.
    """
    _store_offer(offer_md="")
    _, mock_exec = await _route_question("yes please")
    assert mock_exec.await_count == 0


@pytest.mark.asyncio
async def test_rule_1d_ignores_a_followup_with_its_own_content() -> None:
    """A reply that asks something else is routed on its own merits."""
    _store_offer()
    _, mock_exec = await _route_question("yes but what about the stage-out errors")
    if mock_exec.await_count:
        plan = mock_exec.await_args.args[0]
        assert plan.tool_calls[0].tool != _CORE_DUMP_TOOL


def test_get_last_core_dump_offer_requires_a_job_id() -> None:
    """An offer with no usable job ID cannot be acted on, so it is not one."""
    ex_mod._last_evidence_store["panda_log_analysis"] = {
        "evidence": {"core_dump_offer_md": "Analyse it?"},
    }
    assert ex_mod.get_last_core_dump_offer() is None


# ---------------------------------------------------------------------------
# Rule 1d under /fastpath off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_1d_fires_with_the_fast_path_off() -> None:
    """The live regression: ``/fastpath off`` must not disarm offer acceptance.

    ``bypass_fast_path`` exists to hand a *question* to the LLM planner instead
    of a deterministic rule.  A bare "yes" is not such a question: the topic
    guard reformulates content-free follow-ups into a documentation query
    before any planner sees them, so gating rule 1d on the flag did not reroute
    the turn, it destroyed it — job 7272161793's offer was answered with "the
    documentation search did not return relevant results".
    """
    _store_offer()
    _, mock_exec = await _route_question("yes please", bypass_fast_path=True)
    assert mock_exec.await_count == 1
    plan = mock_exec.await_args.args[0]
    assert plan.tool_calls[0].tool == _CORE_DUMP_TOOL
    assert plan.tool_calls[0].arguments["job_id"] == _JOB_ID


@pytest.mark.asyncio
async def test_the_fast_path_flag_still_gates_rule_1c() -> None:
    """Un-gating 1d must not un-gate the routing rules it sits beside.

    Rule 1c *is* a routing rule: with the fast path off, an explicit request
    belongs to the LLM planner, which can name the tool itself.  A test that
    only covered 1d would not notice this regressing.
    """
    _store_offer()
    _, mock_exec = await _route_question(
        "please analyse the core dump in job 7263525363", bypass_fast_path=True,
    )
    assert mock_exec.await_count == 0


@pytest.mark.asyncio
async def test_an_unarmed_affirmative_is_untouched_with_the_fast_path_off() -> None:
    """With no offer stored, "yes" keeps whatever meaning it had before.

    Rule 1d now runs unconditionally, so the no-offer case carries the whole
    weight of not hijacking an affirmative that answers something else.
    """
    _, mock_exec = await _route_question("yes please", bypass_fast_path=True)
    assert mock_exec.await_count == 0
