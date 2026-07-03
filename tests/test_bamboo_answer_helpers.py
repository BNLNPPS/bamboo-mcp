"""Unit tests for module-level helper functions after the executor refactor.

After the refactor the helper functions were split across two modules:

- ``bamboo_answer`` retains: ``_extract_task_id``, ``_extract_job_id``,
  ``_is_log_analysis_request``, ``_extract_history``.
- ``bamboo_executor`` owns: ``_compact_json`` (was ``_compact``),
  ``unpack_tool_result`` (was ``_unpack_tool_result``),
  ``_extract_delegated_text``, ``_extract_rag_context``, ``_rag_hit_count``,
  ``retrieve_rag_context`` (was ``_retrieve_rag_context``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import bamboo.tools.bamboo_executor as ex_mod
from bamboo.tools.bamboo_answer import (
    _extract_job_id,
    _extract_task_id,
    _extract_id_from_history,
    _is_contextual_followup,
    _is_implicit_contextual_followup,
    _is_conceptual_question,
    _is_job_stats_question,
    _is_log_analysis_request,
)
from bamboo.tools.bamboo_executor import (
    _compact_json,
    _extract_delegated_text,
    _extract_rag_context,
    _rag_hit_count,
    retrieve_rag_context,
    unpack_tool_result,
)


class TestExtractTaskId:
    """Pattern matching for task IDs in user questions."""

    def test_plain_task_number(self) -> None:
        """Standard 'task <id>' form is matched."""
        assert _extract_task_id("What is task 12345678?") == 12345678

    def test_task_colon(self) -> None:
        """'task:<id>' form is matched."""
        assert _extract_task_id("task:99887766 status") == 99887766

    def test_task_hash(self) -> None:
        """'task#<id>' form is matched."""
        assert _extract_task_id("task#12345678") == 12345678

    def test_task_dash(self) -> None:
        """'task-<id>' form is matched."""
        assert _extract_task_id("task-56781234 failed") == 56781234

    def test_case_insensitive(self) -> None:
        """Pattern matching is case-insensitive."""
        assert _extract_task_id("TASK 12345678") == 12345678

    def test_no_match_returns_none(self) -> None:
        """Non-task questions return None."""
        assert _extract_task_id("What is PanDA?") is None

    def test_too_short_ignored(self) -> None:
        """IDs below the 4-digit minimum are not matched."""
        assert _extract_task_id("task 123") is None

    def test_too_long_ignored(self) -> None:
        """IDs above the 12-digit maximum are not matched."""
        assert _extract_task_id("task 1234567890123") is None

    def test_exactly_four_digits(self) -> None:
        """Exactly 4-digit IDs are at the lower boundary."""
        assert _extract_task_id("task 1234") == 1234

    def test_exactly_twelve_digits(self) -> None:
        """Exactly 12-digit IDs are at the upper boundary."""
        assert _extract_task_id("task 123456789012") == 123456789012

    def test_empty_string(self) -> None:
        """Empty input returns None."""
        assert _extract_task_id("") is None


class TestExtractJobId:
    """Pattern matching for job / pandaID references."""

    def test_job_space(self) -> None:
        """'job <id>' form is matched."""
        assert _extract_job_id("job 6837798305 failed") == 6837798305

    def test_job_colon(self) -> None:
        """'job:<id>' form is matched."""
        assert _extract_job_id("job:6837798305") == 6837798305

    def test_pandaid(self) -> None:
        """'pandaid <id>' form is matched."""
        assert _extract_job_id("pandaid 6837798305") == 6837798305

    def test_panda_id_spaced(self) -> None:
        """'panda id <id>' form is matched."""
        assert _extract_job_id("panda id 6837798305") == 6837798305

    def test_panda_id_hyphen(self) -> None:
        """'panda-id <id>' form is matched."""
        assert _extract_job_id("panda-id 6837798305") == 6837798305

    def test_case_insensitive(self) -> None:
        """Pattern matching is case-insensitive."""
        assert _extract_job_id("JOB 6837798305") == 6837798305

    def test_no_match_returns_none(self) -> None:
        """Non-job questions return None."""
        assert _extract_job_id("What is PanDA?") is None

    def test_task_reference_not_matched(self) -> None:
        """'task' keyword does not match the job pattern."""
        assert _extract_job_id("task 12345678") is None

    def test_empty_string(self) -> None:
        """Empty input returns None."""
        assert _extract_job_id("") is None


class TestIsLogAnalysisRequest:
    """Keyword detection for log / failure analysis intent."""

    def test_why_fail(self) -> None:
        """'why ... fail' phrasing is detected."""
        assert _is_log_analysis_request("Why did job 6837798305 fail?") is True

    def test_analyse(self) -> None:
        """'analyse' triggers log analysis detection."""
        assert _is_log_analysis_request("analyse job 6837798305") is True

    def test_analyze_american(self) -> None:
        """'analyze' (American spelling) also triggers detection."""
        assert _is_log_analysis_request("analyze job 6837798305") is True

    def test_diagnose(self) -> None:
        """'diagnose' triggers log analysis detection."""
        assert _is_log_analysis_request("diagnose job 6837798305 please") is True

    def test_log_keyword(self) -> None:
        """'log' keyword triggers detection."""
        assert _is_log_analysis_request("show log for job 6837798305") is True

    def test_plain_job_status_not_matched(self) -> None:
        """A plain status question without analysis keywords is not matched."""
        assert _is_log_analysis_request("What is job 6837798305 status?") is False

    def test_no_job_id_not_matched(self) -> None:
        """Failure keywords without a job ID are not matched."""
        assert _is_log_analysis_request("Why did the task fail?") is False

    def test_empty(self) -> None:
        """Empty input returns False."""
        assert _is_log_analysis_request("") is False


class TestIsJobStatsQuestion:
    """Fast-path routing signal detection for ``atlas.job_stats``.

    Covers the memory-leak diagnostics fields (``leak_slope``,
    ``leak_intersect``, ``leak_chi2``) and the software-environment fields
    (``lsetup_time``, ``os_version``, ``python_version``) added alongside
    ``task_container_name`` when Sasha exposed the parsed ``jobmetrics``
    sub-fields as flat top-level fields.
    """

    def test_memory_leak_phrase(self) -> None:
        """The natural phrase 'memory leak' is a signal."""
        assert _is_job_stats_question("What is the average memory leak rate at CERN today?") is True

    def test_leak_rate_phrase(self) -> None:
        """The natural phrase 'leak rate' is a signal."""
        assert _is_job_stats_question("Which site has the highest leak rate today?") is True

    def test_leak_slope_token(self) -> None:
        """The literal field token 'leak_slope' is a signal."""
        assert _is_job_stats_question("What is the average leak_slope at BNL?") is True

    def test_leak_intersect_token(self) -> None:
        """The literal field token 'leak_intersect' is a signal."""
        assert _is_job_stats_question("Show me leak_intersect for job stats at CERN") is True

    def test_leak_chi2_token(self) -> None:
        """The literal field token 'leak_chi2' is a signal."""
        assert _is_job_stats_question("What is the average leak_chi2 today?") is True

    def test_lsetup_time_token(self) -> None:
        """The literal field token 'lsetup_time' is a signal."""
        assert _is_job_stats_question("What is the average lsetup_time at IN2P3?") is True

    def test_os_version_token(self) -> None:
        """The literal field token 'os_version' is a signal."""
        assert _is_job_stats_question("Break down jobs by os_version at CERN") is True

    def test_python_version_token(self) -> None:
        """The literal field token 'python_version' is a signal."""
        assert _is_job_stats_question("Break down jobs by python_version at CERN") is True

    def test_natural_os_version_phrase_not_a_signal(self) -> None:
        """The natural phrase 'os version' (not the field token) is NOT a
        signal — deliberately excluded as too ambiguous with generic
        questions unrelated to job stats, same treatment as atlasrelease."""
        assert _is_job_stats_question("What OS version does ATLAS require?") is False

    def test_natural_python_version_phrase_not_a_signal(self) -> None:
        """The natural phrase 'python version' (not the field token) is NOT
        a signal, for the same ambiguity reason as os_version."""
        assert _is_job_stats_question("What python version does this tool use?") is False

    def test_unrelated_question_not_a_signal(self) -> None:
        """A question with no job-stats signal phrase returns False."""
        assert _is_job_stats_question("What is the weather today?") is False

    def test_python_37_site_question(self) -> None:
        """'sites ... using python 3.7' is a signal via the version regex.

        Regression test: this phrasing has no "version" word and no
        "python_version" token, so it matched _is_jobs_db_question (via
        "sites") before _is_job_stats_question ever had a chance — routing
        to the jobs/CRIC database-disambiguation prompt, or worse, to
        panda_jobs_query (which has no python_version column), instead of
        atlas.job_stats. Reported live as: "Which sites are still using
        python 3.7?" and "Which sites are still running jobs using python
        3.7?".
        """
        assert _is_job_stats_question("Which sites are still using python 3.7?") is True

    def test_python_37_running_jobs_phrase(self) -> None:
        """The 'running jobs using python 3.7' variant is also a signal."""
        assert _is_job_stats_question(
            "Which sites are still running jobs using python 3.7?"
        ) is True

    def test_python_no_space_version(self) -> None:
        """'python3.7' with no space is also a signal."""
        assert _is_job_stats_question("How many jobs used python3.7 at CERN?") is True

    def test_python_bare_major_version(self) -> None:
        """A bare major version ('python 2', 'python 3') is a signal."""
        assert _is_job_stats_question("Are any sites still on python 2?") is True

    def test_python_mention_without_version_number_not_a_signal(self) -> None:
        """A generic Python mention with no version number is NOT a signal
        via the regex (deliberately left to the LLM planner, same as the
        plural 'python versions' phrasing)."""
        assert _is_job_stats_question("How do I write a python script for bamboo?") is False

    def test_el7_shorthand_site_question(self) -> None:
        """'sites ... on EL7' is a signal via the OS version extraction.

        Regression test: this was broken the same way as the Python
        version case above — "Which sites are still on EL7?" matched
        _is_jobs_db_question (via "sites") before this function recognised
        "EL7" as an OS version mention, routing to panda_jobs_query
        (no os_version column) instead of atlas.job_stats.
        """
        assert _is_job_stats_question("Which sites are still on EL7?") is True

    def test_el9_shorthand_is_a_signal(self) -> None:
        """'EL9' shorthand is a signal."""
        assert _is_job_stats_question(
            "What is the average CPU efficiency for jobs on EL9 at CERN today?"
        ) is True

    def test_os_version_word_phrase_is_a_signal(self) -> None:
        """'os version 9.7' (explicit number) is a signal."""
        assert _is_job_stats_question(
            "Break down jobs by site for os version 9.7 today."
        ) is True

    def test_os_mention_without_version_number_not_a_signal(self) -> None:
        """A generic 'os' mention with no version number is NOT a signal."""
        assert _is_job_stats_question("What OS does the pilot run on?") is False

    def test_queue_time_phrase(self) -> None:
        """'queue time' is a signal (pre-existing)."""
        assert _is_job_stats_question("What is the average queue time at CERN?") is True

    def test_queuing_time_phrase(self) -> None:
        """'queuing time' (-ing form) is a signal.

        Regression test: this phrasing does not contain the substring
        'queue time' or 'queuetime', so it previously fell through every
        fast-path signal set to the deterministic RAG-retrieval fallback in
        _build_deterministic_plan, which never returns None to defer to the
        LLM planner. Reported live as: "What was the average queuing time
        at CERN yesterday?" answered with an RAG "excerpts do not contain
        this information" response instead of routing to atlas.job_stats.
        """
        assert _is_job_stats_question("What was the average queuing time at CERN yesterday?") is True

    def test_queueing_time_phrase(self) -> None:
        """'queueing time' (British -eing spelling) is a signal."""
        assert _is_job_stats_question("What is the average queueing time at BNL?") is True

    def test_queue_wait_time_phrase(self) -> None:
        """'queue wait time' (the exact phrase used in planner.py's own
        routing examples) is a signal."""
        assert _is_job_stats_question("What is the maximum queue wait time for failed jobs?") is True


class TestCompactJson:
    """JSON serialisation with length capping (executor helper)."""

    def test_small_dict(self) -> None:
        """Small dicts are serialised normally."""
        result = _compact_json({"key": "val"})
        assert '"key"' in result and '"val"' in result

    def test_truncated_at_limit(self) -> None:
        """Output is capped at limit + truncation suffix."""
        big = {"data": "x" * 10000}
        result = _compact_json(big, limit=100)
        assert len(result) <= 100 + len("…(truncated)")
        assert result.endswith("…(truncated)")

    def test_exact_limit_not_truncated(self) -> None:
        """Output at or below limit is never truncated."""
        assert not _compact_json({"k": "v"}, limit=10000).endswith("…(truncated)")

    def test_non_serialisable_falls_back_to_str(self) -> None:
        """Non-JSON-serialisable objects fall back to repr/str."""
        class _Weird:
            def __repr__(self) -> str:
                return "weird_repr"
        assert "weird_repr" in _compact_json(_Weird())

    def test_list_input(self) -> None:
        """Lists are serialised correctly."""
        result = _compact_json([1, 2, 3])
        assert "1" in result and "2" in result


class TestUnpackToolResult:
    """JSON unwrapping from MCPContent list results."""

    def test_valid_json_text_is_parsed(self) -> None:
        """A valid JSON text block is deserialised."""
        result = unpack_tool_result([{"type": "text", "text": '{"evidence": {"status": "done"}}'}])
        assert result == {"evidence": {"status": "done"}}

    def test_non_json_text_returns_empty(self) -> None:
        """Non-JSON text returns an empty dict."""
        assert unpack_tool_result([{"type": "text", "text": "plain text"}]) == {}

    def test_empty_list_returns_empty(self) -> None:
        """Empty result list returns an empty dict."""
        assert unpack_tool_result([]) == {}

    def test_missing_text_key_returns_empty(self) -> None:
        """Missing text key in content block returns an empty dict."""
        assert unpack_tool_result([{"type": "text"}]) == {}


class TestExtractDelegatedText:
    """Text extraction from bamboo_llm_answer_tool results."""

    def test_standard_mcp_result(self) -> None:
        """Standard MCPContent dict is unwrapped."""
        assert _extract_delegated_text([{"type": "text", "text": "hello world"}]) == "hello world"

    def test_missing_text_key_returns_empty(self) -> None:
        """Missing text key returns empty string."""
        assert _extract_delegated_text([{"type": "text"}]) == ""

    def test_non_dict_first_element_falls_back_to_str(self) -> None:
        """Non-dict first element is stringified."""
        assert "plain string" in _extract_delegated_text(["plain string"])

    def test_empty_list_falls_back_to_str(self) -> None:
        """Empty list falls back to str representation."""
        assert isinstance(_extract_delegated_text([]), str)

    def test_non_list_falls_back_to_str(self) -> None:
        """Non-list input is stringified."""
        assert "bare string" in _extract_delegated_text("bare string")  # type: ignore[arg-type]


class TestExtractRagContext:
    """Context extraction with no-context signal filtering."""

    def test_good_result_returns_text(self) -> None:
        """Useful retrieval text is returned unchanged."""
        result = [{"type": "text", "text": "PanDA is a workload manager.\nMore info here."}]
        assert "PanDA" in _extract_rag_context(result)

    def test_exception_returns_empty(self) -> None:
        """An exception object returns empty string."""
        assert _extract_rag_context(RuntimeError("fail")) == ""

    def test_not_installed_signal(self) -> None:
        """'not installed' on the first line suppresses the result."""
        assert _extract_rag_context([{"type": "text", "text": "ChromaDB is not installed."}]) == ""

    def test_no_results_found_signal(self) -> None:
        """'no results found' on the first line suppresses the result."""
        assert _extract_rag_context([{"type": "text", "text": "No results found for your query."}]) == ""

    def test_chromadb_path_not_found(self) -> None:
        """'chromadb path not found' on the first line suppresses the result."""
        assert _extract_rag_context([{"type": "text", "text": "ChromaDB path not found at /tmp/db"}]) == ""

    def test_no_keyword_matches_signal(self) -> None:
        """'no keyword matches' on the first line suppresses the result."""
        assert _extract_rag_context([{"type": "text", "text": "No keyword matches for 'foo bar'."}]) == ""

    def test_signal_only_on_second_line_not_suppressed(self) -> None:
        """Suppression signals on lines after the first are ignored."""
        result = [{"type": "text", "text": "Good context here.\nNo results found elsewhere."}]
        assert _extract_rag_context(result) != ""

    def test_empty_list_returns_empty(self) -> None:
        """Empty list returns empty string."""
        assert _extract_rag_context([]) == ""

    def test_non_list_returns_empty(self) -> None:
        """Non-list input returns empty string."""
        assert _extract_rag_context("not a list") == ""


class TestRagHitCount:
    """Hit counting from retrieval results."""

    def test_counts_non_empty_lines(self) -> None:
        """Non-empty lines are counted."""
        context = "line one\nline two\nline three"
        assert _rag_hit_count([{"type": "text", "text": context}], context) == 3

    def test_blank_lines_not_counted(self) -> None:
        """Blank lines are excluded from the count."""
        context = "line one\n\n\nline two\n"
        assert _rag_hit_count([{"type": "text", "text": context}], context) == 2

    def test_exception_returns_minus_one(self) -> None:
        """Exception result returns -1."""
        assert _rag_hit_count(RuntimeError("error"), "") == -1

    def test_empty_context_returns_zero(self) -> None:
        """Empty context returns 0."""
        assert _rag_hit_count([{"text": ""}], "") == 0


class TestRetrieveRagContext:
    """Integration tests for retrieve_rag_context with mocked search functions."""

    @pytest.mark.asyncio
    async def test_merges_both_results(self) -> None:
        """Both vector and BM25 results are merged with a separator."""
        vec_text = "Vector result content."
        bm25_text = "BM25 keyword result."
        with (
            patch.object(ex_mod, "_run_vector_search", new=AsyncMock(return_value=vec_text)),
            patch.object(ex_mod, "_run_bm25_search", new=AsyncMock(return_value=bm25_text)),
        ):
            ctx = await retrieve_rag_context("test question")
        assert vec_text in ctx
        assert bm25_text in ctx
        assert "Keyword search results" in ctx

    @pytest.mark.asyncio
    async def test_falls_back_to_vector_only(self) -> None:
        """When BM25 returns empty, only vector context is returned."""
        vec_text = "Only vector content."
        with (
            patch.object(ex_mod, "_run_vector_search", new=AsyncMock(return_value=vec_text)),
            patch.object(ex_mod, "_run_bm25_search", new=AsyncMock(return_value="")),
        ):
            ctx = await retrieve_rag_context("test question")
        assert ctx == vec_text

    @pytest.mark.asyncio
    async def test_both_fail_returns_empty(self) -> None:
        """When both searches raise, an empty string is returned gracefully."""
        with (
            patch.object(ex_mod, "_run_vector_search", new=AsyncMock(side_effect=RuntimeError("down"))),
            patch.object(ex_mod, "_run_bm25_search", new=AsyncMock(side_effect=RuntimeError("down"))),
        ):
            ctx = await retrieve_rag_context("test question")
        assert ctx == ""


# ---------------------------------------------------------------------------
# Contextual follow-up routing helpers (added with history ID extraction)
# ---------------------------------------------------------------------------


class TestIsContextualFollowup:
    """Tests for :func:`bamboo_answer._is_contextual_followup`.

    Only tests explicit pronoun/demonstrative back-references.
    Implicit short follow-ups are handled by _is_implicit_contextual_followup.
    """

    @pytest.mark.parametrize("text", [
        "How many of those jobs failed?",
        "Which of them are at BNL?",
        "What is the status of that task?",
        "Are those jobs still running?",
        "What error did it produce?",
        "How many of the jobs are finished?",
        "Tell me about the results",
        "What happened to them?",
        "What is its piloterrorcode?",
        "Can you analyse that job?",
    ])
    def test_contextual_followup_detected(self, text: str) -> None:
        """Explicit pronoun/demonstrative back-references are detected."""
        assert _is_contextual_followup(text), f"Expected match for: {text!r}"

    @pytest.mark.parametrize("text", [
        "What is PanDA?",
        "How does brokerage work?",
        "How many jobs finished?",   # no back-reference — handled by implicit path
        "Which sites are used?",     # no back-reference — handled by implicit path
        "hello",
        "thanks",
        "",
        "Tell me more",
    ])
    def test_non_contextual_not_detected(self, text: str) -> None:
        """Questions without explicit back-references are not matched."""
        assert not _is_contextual_followup(text), f"Expected no match for: {text!r}"


class TestIsImplicitContextualFollowup:
    """Tests for :func:`bamboo_answer._is_implicit_contextual_followup`."""

    @pytest.mark.parametrize("text", [
        "How many jobs finished?",
        "How many jobs are running?",
        "Which sites are used?",
        "How many are still running?",
        "Any jobs still transferring?",
        "How many failed?",
        "Any errors?",
    ])
    def test_implicit_followup_detected(self, text: str) -> None:
        """Short questions with domain status words are detected."""
        assert _is_implicit_contextual_followup(text), f"Expected match for: {text!r}"

    @pytest.mark.parametrize("text", [
        # Social / general — no domain word
        "What is PanDA?",
        "How does brokerage work?",
        "hello",
        "thanks",
        "",
        "Tell me more",
        # Long questions — above word limit even with domain words
        "Explain the JEDI architecture in detail and how it relates to task scheduling",
        "What are the main components of the PanDA workload management system?",
        "How does the pilot framework interact with the workload management system?",
        # Fresh pilot questions — must NOT inherit task ID even though short
        # and containing domain words like "running"
        "How many pilots are running at BNL right now",
        "How many pilots are running at MWT2?",
        "How many pilots are idle?",
        "How many pilots are running right now?",
        # Fresh site-scoped job questions
        "How many jobs failed at AGLT2?",
        "What are the pilot and job failure rates at BNL?",
    ])
    def test_non_implicit_not_detected(self, text: str) -> None:
        """Social messages, long doc questions, and fresh site/pilot questions are not matched."""
        assert not _is_implicit_contextual_followup(text), f"Expected no match for: {text!r}"


class TestExtractIdFromHistory:
    """Tests for :func:`bamboo_answer._extract_id_from_history`."""

    def test_extracts_task_id_from_user_turn(self) -> None:
        """A task ID in a prior user turn is found."""
        history = [
            {"role": "user", "content": "Summarize task 49375514"},
            {"role": "assistant", "content": "Task 49375514 has 84 jobs."},
        ]
        task_id, job_id = _extract_id_from_history(history)
        assert task_id == 49375514
        assert job_id is None

    def test_extracts_task_id_from_assistant_turn(self) -> None:
        """A task ID mentioned only in an assistant reply is still found."""
        history = [
            {"role": "user", "content": "What about that task?"},
            {"role": "assistant", "content": "Task 49375514 is currently running."},
        ]
        task_id, _ = _extract_id_from_history(history)
        assert task_id == 49375514

    def test_extracts_job_id_from_history(self) -> None:
        """A job ID in history is extracted correctly."""
        history = [
            {"role": "user", "content": "Analyse job 7061545370"},
            {"role": "assistant", "content": "Job 7061545370 failed with pilot error 1008."},
        ]
        _, job_id = _extract_id_from_history(history)
        assert job_id == 7061545370

    def test_most_recent_id_wins(self) -> None:
        """When multiple IDs appear in history, the most recent is returned."""
        history = [
            {"role": "user", "content": "Check task 11111111"},
            {"role": "assistant", "content": "Task 11111111 is done."},
            {"role": "user", "content": "Now check task 22222222"},
            {"role": "assistant", "content": "Task 22222222 is running."},
        ]
        task_id, _ = _extract_id_from_history(history)
        assert task_id == 22222222

    def test_empty_history_returns_none(self) -> None:
        """Empty history yields (None, None)."""
        assert _extract_id_from_history([]) == (None, None)

    def test_history_with_no_ids_returns_none(self) -> None:
        """History containing no IDs yields (None, None)."""
        history = [
            {"role": "user", "content": "What is PanDA?"},
            {"role": "assistant", "content": "PanDA is a workload manager."},
        ]
        assert _extract_id_from_history(history) == (None, None)


class TestContextualFollowupRouting:
    """Integration tests: contextual follow-ups route to the correct tool."""

    @pytest.mark.asyncio
    async def test_contextual_followup_routes_to_task_status(self) -> None:
        """'How many of those jobs failed?' with task history → panda_task_status."""
        import bamboo.tools.bamboo_answer as ba_mod
        from bamboo.tools.bamboo_answer import BambooAnswerTool
        from bamboo.tools.topic_guard import GuardResult

        guard_mock = AsyncMock(return_value=GuardResult(
            allowed=True, reason="keyword_allow", llm_used=False
        ))
        execute_mock = AsyncMock(return_value=[{"type": "text", "text": "0 jobs failed."}])
        tool = BambooAnswerTool()

        history = [
            {"role": "user", "content": "What are the panda jobs in task 49375514?"},
            {"role": "assistant", "content": "Task 49375514 has 84 jobs."},
        ]

        with (
            patch.object(ba_mod, "check_topic", guard_mock),
            patch.object(ba_mod, "execute_plan", execute_mock),
        ):
            await tool.call({
                "question": "How many of those jobs failed?",
                "messages": [
                    *history,
                    {"role": "user", "content": "How many of those jobs failed?"},
                ],
            })

        execute_mock.assert_awaited_once()
        plan = execute_mock.call_args[0][0]
        assert plan.tool_calls[0].tool == "panda_task_status"
        assert plan.tool_calls[0].arguments["task_id"] == 49375514

    @pytest.mark.asyncio
    async def test_genuine_doc_question_still_routes_to_rag(self) -> None:
        """A question with no back-reference and no ID defers to the LLM
        planner, which is itself capable of choosing RAG tools.

        Since the "always build a deterministic RAG plan" fallback in
        _build_deterministic_plan was removed (see test_bamboo_answer_rag.py
        for the rationale), this now asserts that _route() defers to
        bamboo_plan_tool rather than calling execute_plan directly.
        """
        import bamboo.tools.bamboo_answer as ba_mod
        from bamboo.tools.bamboo_answer import BambooAnswerTool
        from bamboo.tools.topic_guard import GuardResult

        guard_mock = AsyncMock(return_value=GuardResult(
            allowed=True, reason="keyword_allow", llm_used=False
        ))
        plan_mock = AsyncMock(return_value=[{"type": "text", "text": "PanDA info."}])
        tool = BambooAnswerTool()

        with (
            patch.object(ba_mod, "check_topic", guard_mock),
            patch.object(ba_mod, "bamboo_plan_tool") as mock_plan_tool,
        ):
            mock_plan_tool.call = plan_mock
            await tool.call({"question": "How does brokerage work?"})

        plan_mock.assert_awaited_once()
        plan_args = plan_mock.call_args[0][0]
        assert plan_args["question"] == "How does brokerage work?"


# ---------------------------------------------------------------------------
# Tests for pilot source analysis routing
# ---------------------------------------------------------------------------

class TestIsPilotSourceRequest:
    """Tests for :func:`bamboo_answer._is_pilot_source_request`."""

    from bamboo.tools.bamboo_answer import _is_pilot_source_request as _fn

    @pytest.mark.parametrize("text", [
        "Why did the pilot code raise that exception? Can it be fixed?",
        "Show me the pilot source code that failed",
        "Why did the pilot raise that?",
        "Can this be fixed?",
        "How to fix this?",
        "Show me the source code",
        "Deep dive into the pilot exception",
        "What is wrong with the pilot code?",
        "list_processes_and_threads function",
        "getpwuid error in the pilot",
        "psutils module",
        "more details on the exception",
        "patch the pilot",
        "workaround for this?",
    ])
    def test_positive_signals(self, text: str) -> None:
        from bamboo.tools.bamboo_answer import _is_pilot_source_request
        assert _is_pilot_source_request(text), f"Expected match for: {text!r}"

    @pytest.mark.parametrize("text", [
        "Analyse the failure of job 7099503721",
        "Why did job 7099503721 fail?",
        "What is the status of job 7099503721?",
        "How many jobs failed at BNL?",
        "Is PanDA alive?",
        "What is the ATLAS release for task 49752363?",
        "Tell me about stage-in timeouts",
    ])
    def test_negative_signals(self, text: str) -> None:
        from bamboo.tools.bamboo_answer import _is_pilot_source_request
        assert not _is_pilot_source_request(text), f"Expected no match for: {text!r}"


class TestPilotSourceAnalysisFastPath:
    """Integration tests: pilot_source_analysis fast-path routing."""

    @pytest.mark.asyncio
    async def test_routes_to_pilot_source_when_prior_monitoring_error(self) -> None:
        """After pilot_monitoring_error, a source-question routes to pilot_source_analysis.

        The question intentionally contains 'why' + job ID so it would normally
        match _is_log_analysis_request (rule 1).  Rule 1b must win because
        pilot-source signals are present and stored evidence exists.
        """
        import bamboo.tools.bamboo_answer as ba_mod
        from bamboo.tools.bamboo_answer import BambooAnswerTool
        from bamboo.tools.topic_guard import GuardResult

        fake_evidence = {
            "failure_type": "pilot_monitoring_error",
            "log_excerpt": "WARNING | Exception caught: 'getpwuid(): uid not found: 6435'\n"
                           "KeyError: 'getpwuid(): uid not found: 6435'",
            "piloterrordiag": "Exception caught: 'getpwuid(): uid not found: 6435'",
        }

        guard_mock = AsyncMock(return_value=GuardResult(
            allowed=True, reason="keyword_allow", llm_used=False
        ))
        execute_mock = AsyncMock(return_value=[{"type": "text", "text": "source analysis done"}])
        tool = BambooAnswerTool()

        with (
            patch.object(ba_mod, "check_topic", guard_mock),
            patch.object(ba_mod, "execute_plan", execute_mock),
            patch.object(ba_mod, "get_last_pilot_monitoring_evidence",
                         return_value=fake_evidence),
        ):
            await tool.call({
                "question": "Why did the pilot code raise that exception? "
                            "Can it be fixed? job 7099503721",
            })

        execute_mock.assert_awaited_once()
        plan = execute_mock.call_args[0][0]
        assert plan.tool_calls[0].tool == "pilot_source_analysis"
        args = plan.tool_calls[0].arguments
        assert args["job_id"] == 7099503721
        assert "getpwuid" in args["log_excerpt"]

    @pytest.mark.asyncio
    async def test_does_not_route_to_pilot_source_without_prior_evidence(self) -> None:
        """Without prior pilot_monitoring_error evidence, falls through to panda_job_status."""
        import bamboo.tools.bamboo_answer as ba_mod
        from bamboo.tools.bamboo_answer import BambooAnswerTool
        from bamboo.tools.topic_guard import GuardResult

        guard_mock = AsyncMock(return_value=GuardResult(
            allowed=True, reason="keyword_allow", llm_used=False
        ))
        execute_mock = AsyncMock(return_value=[{"type": "text", "text": "job status"}])
        tool = BambooAnswerTool()

        with (
            patch.object(ba_mod, "check_topic", guard_mock),
            patch.object(ba_mod, "execute_plan", execute_mock),
            patch.object(ba_mod, "get_last_pilot_monitoring_evidence",
                         return_value=None),
        ):
            await tool.call({
                "question": "Can this be fixed? job 7099503721",
            })

        execute_mock.assert_awaited_once()
        plan = execute_mock.call_args[0][0]
        # Should fall through to panda_job_status, not pilot_source_analysis
        assert plan.tool_calls[0].tool != "pilot_source_analysis"

    @pytest.mark.asyncio
    async def test_log_analysis_question_still_routes_to_log_analysis(self) -> None:
        """An initial diagnosis question still routes to panda_log_analysis, not pilot_source."""
        import bamboo.tools.bamboo_answer as ba_mod
        from bamboo.tools.bamboo_answer import BambooAnswerTool
        from bamboo.tools.topic_guard import GuardResult

        guard_mock = AsyncMock(return_value=GuardResult(
            allowed=True, reason="keyword_allow", llm_used=False
        ))
        execute_mock = AsyncMock(return_value=[{"type": "text", "text": "log analysis done"}])
        tool = BambooAnswerTool()

        # Even with prior pilot_monitoring_error evidence, a pure diagnosis question
        # (no pilot-source signals) must still use panda_log_analysis (rule 1).
        fake_evidence = {
            "failure_type": "pilot_monitoring_error",
            "log_excerpt": "WARNING | getpwuid error",
            "piloterrordiag": "getpwuid error",
        }

        with (
            patch.object(ba_mod, "check_topic", guard_mock),
            patch.object(ba_mod, "execute_plan", execute_mock),
            patch.object(ba_mod, "get_last_pilot_monitoring_evidence",
                         return_value=fake_evidence),
        ):
            await tool.call({
                "question": "Analyse the failure of job 7099503721",
            })

        execute_mock.assert_awaited_once()
        plan = execute_mock.call_args[0][0]
        assert plan.tool_calls[0].tool == "panda_log_analysis"


class TestIsConceptualQuestion:
    """Detection of definitional / conceptual questions.

    These questions ask what something *means* or *is*.  Any job or task ID
    in the question is incidental context from a prior turn and must not
    trigger an operational tool call.
    """

    def test_what_does_it_mean(self) -> None:
        """'what does it mean that X is Y' is detected as conceptual."""
        assert _is_conceptual_question(
            "what does it mean that a job is looping?"
        ) is True

    def test_what_does_it_mean_with_job_id(self) -> None:
        """Incidental job ID does not prevent conceptual detection."""
        assert _is_conceptual_question(
            "what does it mean that job 7103770630 is looping?"
        ) is True

    def test_what_is_a(self) -> None:
        """'what is a X' is detected as conceptual."""
        assert _is_conceptual_question("what is a looping job?") is True

    def test_what_is_an(self) -> None:
        """'what is an X' is detected as conceptual."""
        assert _is_conceptual_question("what is an input_missing error?") is True

    def test_whats_a(self) -> None:
        """Contracted 'what's a X' is detected as conceptual."""
        assert _is_conceptual_question("what's a stagein_timeout?") is True

    def test_what_does_x_mean(self) -> None:
        """'what does X mean' is detected as conceptual."""
        assert _is_conceptual_question("what does stagein_timeout mean?") is True

    def test_what_do_x_mean(self) -> None:
        """'what do X mean' (plural) is detected as conceptual."""
        assert _is_conceptual_question("what do looping jobs mean?") is True

    def test_can_you_explain_what(self) -> None:
        """'can you explain what X is' is detected as conceptual."""
        assert _is_conceptual_question(
            "can you explain what pilot error 1305 is?"
        ) is True

    def test_can_you_define(self) -> None:
        """'can you define what a looping job is' is detected as conceptual."""
        assert _is_conceptual_question(
            "can you define what a looping job is?"
        ) is True

    def test_operational_status_not_matched(self) -> None:
        """'what is the status of job X' is operational, not conceptual."""
        assert _is_conceptual_question(
            "what is the status of job 7103770630?"
        ) is False

    def test_analyse_not_matched(self) -> None:
        """An analysis request is not conceptual."""
        assert _is_conceptual_question(
            "analyse the failure of job 7103770630"
        ) is False

    def test_why_fail_not_matched(self) -> None:
        """'why did job X fail' is operational, not conceptual."""
        assert _is_conceptual_question(
            "why did job 7103770630 fail?"
        ) is False

    def test_show_logs_not_matched(self) -> None:
        """'show me the logs for job X' is operational, not conceptual."""
        assert _is_conceptual_question(
            "show me the logs for job 7103770630"
        ) is False

    def test_empty(self) -> None:
        """Empty input returns False."""
        assert _is_conceptual_question("") is False


class TestConceptualQuestionRouting:
    """Integration tests: conceptual follow-up questions skip job-status routing."""

    @pytest.mark.asyncio
    async def test_conceptual_followup_with_job_id_skips_job_status(self) -> None:
        """'what does it mean that job X is looping?' routes to LLM, not job status.

        Regression test: the question contains job ID 7103770630 from a prior
        analysis turn.  The deterministic router must recognise the conceptual
        phrasing and fall through to the LLM planner instead of calling
        panda_job_status with stale evidence.
        """
        import bamboo.tools.bamboo_answer as ba_mod
        from bamboo.tools.bamboo_answer import BambooAnswerTool
        from bamboo.tools.topic_guard import GuardResult

        guard_mock = AsyncMock(return_value=GuardResult(
            allowed=True, reason="keyword_allow", llm_used=False
        ))
        execute_mock = AsyncMock(return_value=[{"type": "text", "text": "should not be called"}])
        plan_mock = AsyncMock(return_value=[{"type": "text", "text": "explanation"}])
        tool = BambooAnswerTool()

        with (
            patch.object(ba_mod, "check_topic", guard_mock),
            patch.object(ba_mod, "execute_plan", execute_mock),
            patch.object(ba_mod, "bamboo_plan_tool") as mock_plan_tool,
            patch.object(ba_mod, "get_last_pilot_monitoring_evidence",
                         return_value=None),
        ):
            mock_plan_tool.call = plan_mock
            await tool.call({
                "question": (
                    "what does it mean that job 7103770630 is looping?"
                ),
            })

        # The deterministic fast-path must never call execute_plan directly
        # for this question — that would mean panda_job_status (or some
        # other FAST_PATH tool) ran with the stale job_id as evidence.
        execute_mock.assert_not_called()
        plan_mock.assert_awaited_once()
        plan_args = plan_mock.call_args[0][0]
        assert plan_args["question"] == "what does it mean that job 7103770630 is looping?"

    @pytest.mark.asyncio
    async def test_operational_job_id_question_still_routes_to_job_status(self) -> None:
        """'what is the status of job X' still routes to panda_job_status.

        Ensures the conceptual guard does not block legitimate status queries
        that happen to contain 'what is'.
        """
        import bamboo.tools.bamboo_answer as ba_mod
        from bamboo.tools.bamboo_answer import BambooAnswerTool
        from bamboo.tools.topic_guard import GuardResult

        guard_mock = AsyncMock(return_value=GuardResult(
            allowed=True, reason="keyword_allow", llm_used=False
        ))
        execute_mock = AsyncMock(return_value=[{"type": "text", "text": "status"}])
        tool = BambooAnswerTool()

        with (
            patch.object(ba_mod, "check_topic", guard_mock),
            patch.object(ba_mod, "execute_plan", execute_mock),
            patch.object(ba_mod, "get_last_pilot_monitoring_evidence",
                         return_value=None),
        ):
            await tool.call({
                "question": "what is the status of job 7103770630?",
            })

        execute_mock.assert_awaited_once()
        plan = execute_mock.call_args[0][0]
        assert plan.tool_calls[0].tool == "panda_job_status", (
            "An operational status question must still route to panda_job_status. "
            f"Got tool={plan.tool_calls[0].tool!r}"
        )
