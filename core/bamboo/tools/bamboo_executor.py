"""Plan executor for Bamboo — runs a validated :class:`~bamboo.tools.planner.Plan`.

This module provides the execution layer that sits between the planner and the
LLM synthesiser.  Given a ``Plan`` produced by :mod:`bamboo.tools.planner`, it:

1. Iterates ``plan.tool_calls`` in order.
2. Resolves each tool via the core ``TOOLS`` registry or the plugin entry-point
   loader.
3. Validates arguments with :func:`~bamboo.core._validate_arguments`.
4. Calls ``await tool.call(args)`` and collects ``list[MCPContent]``.
5. Unpacks JSON evidence from evidence tools.
6. Selects a synthesis system prompt based on which tools were called.
7. Synthesises a final natural-language answer via the LLM.

All functions are intentionally **pure orchestration** — no experiment-specific
logic lives here.  Synthesis prompts are kept as module-level constants so they
can be updated independently of routing logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from bamboo.llm.types import Message
from bamboo.tools.base import MCPContent, text_content
from bamboo.tools.llm_passthrough import bamboo_llm_answer_tool
from bamboo.tools.loader import find_tool_by_name
from bamboo.tools.planner import Plan
from bamboo.tracing import EVENT_PLAN, EVENT_RETRIEVAL, EVENT_SYNTHESIS, span

# ---------------------------------------------------------------------------
# In-process evidence store
#
# Populated by execute_plan() after every successful tool call so that the
# TUI /json and /inspect commands can retrieve the last evidence dict without
# re-fetching from BigPanDA.  Keys are tool names; values are the unpacked
# evidence dicts.  A separate "last_tool" key tracks which tool ran most
# recently so callers can retrieve the most relevant entry.
# ---------------------------------------------------------------------------

_last_evidence_store: dict[str, Any] = {}
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synthesis system prompt constants (moved from bamboo_answer.py)
# ---------------------------------------------------------------------------

_SYSTEM_LOG_ANALYSIS: str = (
    "You are AskPanDA for the ATLAS experiment.\n"
    "Given a user's question and a JSON evidence object containing PanDA job "
    "log analysis, write a concise diagnostic answer.\n"
    "Rules:\n"
    "- State the failure classification clearly.\n"
    "- Quote relevant log excerpts if present.\n"
    "- Suggest concrete next steps based on the failure type.\n"
    "- Do not include a Links section; links are appended automatically.\n"
    "- Keep it under ~10 bullet points.\n"
)

_SYSTEM_PILOT_SOURCE: str = (
    "You are AskPanDA for the ATLAS experiment.\n"
    "You have fetched the pilot3 source code from GitHub for the functions "
    "named in a job failure traceback.\n"
    "The evidence contains:\n"
    "- exception: the exception string that was raised.\n"
    "- traceback_frames: list of {pilot_path, func} dicts from the traceback.\n"
    "- source_snippets: dict keyed by 'path::function' containing the extracted "
    "source of each function.\n"
    "- github_urls: dict keyed by pilot path with links to the GitHub source.\n"
    "- missing_functions: functions named in the traceback but not found in source.\n"
    "- fetch_errors: any GitHub fetch failures.\n"
    "Rules:\n"
    "- Explain exactly which line in the deepest frame caused the exception and why.\n"
    "- Quote the relevant source lines from source_snippets.\n"
    "- Describe whether this is a pilot infrastructure bug or a site configuration "
    "issue (e.g. missing UID in passwd/LDAP vs. a code defect).\n"
    "- If the fix is straightforward, suggest a concrete code change or workaround.\n"
    "- Include GitHub source links from github_urls so developers can navigate directly.\n"
    "- Do not include a Links section; links are appended automatically.\n"
    "- Keep it focused — this is a developer-level diagnosis, not a user-facing summary.\n"
)

_SYSTEM_JOB: str = (
    "You are AskPanDA for the ATLAS experiment.\n"
    "Given a user's question and a JSON evidence object from BigPanDA, "
    "write a concise, helpful answer about the job status.\n"
    "Rules:\n"
    "- If evidence.not_found is true: say the job was not found and suggest "
    "checking the ID.\n"
    "- Otherwise: summarise status, site, queue, pilot error, and timing.\n"
    "- Always include the BigPanDA monitor URL as plain text (not a Markdown "
    "hyperlink), e.g.: Monitor: https://bigpanda.cern.ch/job/12345/\n"
    "- Keep it under ~8 bullet points.\n"
)

_SYSTEM_TASK: str = (
    "You are AskPanDA, an assistant for the ATLAS experiment at CERN.\n"
    "You are given a user question and JSON metadata for a PanDA task fetched from BigPanDA.\n"
    "Answer the user's specific question using only data explicitly present in the metadata.\n"
    "Rules:\n"
    "- If evidence.not_found is true or evidence.http_status==404: clearly state that the task ID\n"
    "  was not found in BigPanDA. Say the task does not exist or the ID is incorrect. Do not\n"
    "  include a monitor link.\n"
    "- If evidence indicates a non-JSON or HTTP error (but not 404): explain that BigPanDA returned\n"
    "  an unexpected response and include the monitor_url so the user can check manually.\n"
    "- TASK STATUS: ``task_status`` is the ONLY authoritative source for the overall task\n"
    "  outcome. Report it verbatim (e.g. \'finished\', \'failed\', \'running\'). The jobs endpoint\n"
    "  returns only a SAMPLE of jobs (typically failed ones), so jobs_by_status reflects that\n"
    "  sample only — NOT all jobs. A task with task_status=\'finished\' completed successfully\n"
    "  even if the sample contains only failed jobs. NEVER infer the task outcome from\n"
    "  jobs_by_status. Always report task_status first, then the job breakdown.\n"
    "- JOB COUNTS: Use dsinfo[\'nfilesfinished\'] and dsinfo[\'nfilesfailed\'] for the\n"
    "  authoritative finished/failed counts when present — these are not inflated by retries.\n"
    "  Fall back to sum(jobs_by_piloterrorcode.values()) for failed count if dsinfo is absent.\n"
    "  jobs_by_status may be inflated by retries. A single job appears in BOTH\n"
    "  jobs_by_piloterrorcode AND errs_by_count (different views) — do NOT add them together.\n"
    "- TERMINOLOGY: When reporting nfilesfinished/nfilesfailed from dsinfo, call them JOBS not\n"
    "  files (e.g. \'49,988 jobs finished, 12 jobs failed\'). These are grid job counts, not\n"
    "  dataset file counts. Use \'files\' only when describing dataset contents.\n"
    "- PANDA IDs: The evidence has a ``failed_pandaids`` field — a plain list of integer\n"
    "  PanDA job IDs for the failed jobs (e.g. [7073513639, 7073514709, ...]). When the\n"
    "  user asks for job IDs, list every value in failed_pandaids. Note it is a sample\n"
    "  (up to 20) and point to the monitor URL for the complete list.\n"
    "- Provide a thorough summary covering: overall status, dataset name, job counts\n"
    "  (finished, failed, total), failure details (error codes and root cause), computing\n"
    "  sites involved, and any other fields relevant to the question. Use bullet points.\n"
    "- Otherwise: answer the question directly using only fields present in the metadata.\n"
    "- NEVER infer, guess, or derive values not explicitly in the data. If a requested value is\n"
    "  absent, say it is not available in the metadata rather than inventing it.\n"
    "- The Job list section below the metadata lists actual PanDA job IDs. Use ONLY those IDs\n"
    "  when answering questions about pandaids/job IDs. If the section says no jobs were\n"
    "  returned, say so — never derive job IDs from dataset IDs or any other field.\n"
    "- Include the BigPanDA monitor URL as plain text at the end in non-error cases,\n"
    "  e.g.: Monitor: https://bigpanda.cern.ch/task/12345/\n"
)

_SYSTEM_RAG: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "You are given a user question and relevant excerpts retrieved from the "
    "PanDA/Bamboo documentation knowledge base.\n"
    "Rules:\n"
    "- Base your answer primarily on the retrieved documentation excerpts.\n"
    "- If the excerpts fully answer the question, do not add unreferenced claims.\n"
    "- If the excerpts are only partially relevant, supplement with your general "
    "knowledge but clearly distinguish what comes from documentation vs. general "
    "knowledge.\n"
    "- Be concise and precise. Prefer bullet points for multi-part answers.\n"
    "- Do not fabricate PanDA-specific details (task IDs, queue names, error "
    "codes) that are not in the excerpts.\n"
)

_SYSTEM_RAG_NO_CONTEXT: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "No relevant documentation excerpts were found for this question.\n"
    "Rules:\n"
    "- Do NOT answer from general knowledge or make up any PanDA-specific "
    "details such as error codes, queue names, or configuration values.\n"
    "- Tell the user that the documentation knowledge base did not contain "
    "enough information to answer this question reliably.\n"
    "- Suggest they consult the official PanDA documentation or BigPanDA "
    "monitor directly.\n"
    "- If you can point to a plausible documentation URL or resource name, "
    "do so — but do not invent specific technical values.\n"
)

_SYSTEM_GENERIC: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "You have been given the results of one or more tool calls. Synthesise "
    "a clear, concise answer to the user's question based solely on the "
    "evidence provided.\n"
    "Rules:\n"
    "- Do not infer or fabricate values not present in the evidence.\n"
    "- If the evidence shows errors or empty results, explain that clearly.\n"
    "- Include relevant monitor URLs when available.\n"
    "- Be concise and prefer bullet points for multi-part answers.\n"
)

_SYSTEM_JOBS_QUERY: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "You have queried the live PanDA jobs database and received structured results.\n"
    "The evidence contains: the SQL query that was executed, the result rows, "
    "row_count, and an error field (null means success — do NOT treat null as an error).\n"
    "Rules:\n"
    "- Answer directly and concisely from the rows and row_count in the evidence.\n"
    "- If error is null and rows are present, give the answer confidently.\n"
    "- If row_count is 0, say no matching jobs were found.\n"
    "- If the error field contains a non-null string, explain the problem clearly.\n"
    "- Do not fabricate job counts or status values not present in the rows.\n"
    "- Do not include any timestamp, freshness, or 'data as of' text — this is\n"
    "  added automatically as a footnote after your response.\n"
    "- Be concise. For count questions, lead with the number.\n"
)

_SYSTEM_CRIC_QUERY: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "You have queried the CRIC (Computing Resource Information Catalogue) "
    "database and received structured results about ATLAS computing queues.\n"
    "The evidence contains: the SQL query that was executed, the result rows, "
    "row_count, and an error field (null means success — do NOT treat null as an error).\n"
    "Schema notes: table is 'queuedata'; USE 'status' column for filtering "
    "(online/offline/test/brokeroff) — 'state' is always 'ACTIVE' for all rows; "
    "site in 'atlas_site'; copytools/acopytools are JSON arrays.\n"
    "Rules:\n"
    "- Answer directly from the rows and row_count in the evidence.\n"
    "- If error is null and rows are present, give the answer confidently.\n"
    "- If row_count is 0: check if atlas_site used exact equality (= not ILIKE).\n"
    "  ATLAS site names include suffixes (BNL-ATLAS, CERN-PROD, not BNL/CERN).\n"
    "  Suggest asking 'what sites are available?' or rephrasing with partial match.\n"
    "- If row_count is 0 and SQL used ILIKE, say no matching queues were found.\n"
    "- If the error field contains a non-null string, quote it VERBATIM.\n"
    "- FULL-LIST RULE: If the question asks to list/show ALL queues (no site or status filter)\n"
    "  AND truncated is false, you MUST enumerate EVERY queue in the rows — do NOT summarise,\n"
    "  do NOT say 'truncated', do NOT show only examples. Render all rows as a table with\n"
    "  columns: Site, Queue, Status, Type. Group consecutive rows by site (omit repeated site\n"
    "  name). State the total count (row_count) as a header line, e.g. '230 queues total:'.\n"
    "  Do NOT include any timestamp or 'data as of' text in the header — freshness\n"
    "  is shown separately as a footnote.\n"
    "- If rows contain GROUP BY aggregation columns (e.g. atlas_site + count), "
    "present as a site-count table and state the total across all groups.\n"
    "- If rows contain individual queue names for a SCOPED query (site or status filter),\n"
    "  present them grouped by site.\n"
    "- If truncated is true, note the result was capped and suggest filtering "
    "by atlas_site (e.g. 'Which queues at BNL are not online?') to get a "
    "complete list for a specific location.\n"
    "- Highlight queue status (online/offline/test/brokeroff) prominently.\n"
    "- Do not fabricate queue names, statuses, or resource values not in the rows.\n"
    "- Do not include any timestamp, freshness, or 'data as of' text — this is\n"
    "  added automatically as a footnote after your response.\n"
    "- For count questions, lead with the number.\n"
)

_SYSTEM_HARVESTER_WORKERS: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "You have retrieved live Harvester worker (pilot) statistics from the BigPanDA API.\n"
    "The evidence contains:\n"
    "- nworkers_total: total pilot count across all statuses\n"
    "- nworkers_by_status: {status: count}\n"
    "- nworkers_by_resourcetype: {resourcetype: count} — e.g. MCORE, SCORE\n"
    "- nworkers_by_jobtype: {jobtype: count} — e.g. managed, user\n"
    "- nworkers_by_site: {site: count} (useful when no site filter was applied)\n"
    "- pivot: list of {status, jobtype, resourcetype, nworkers} rows, sorted by "
    "nworkers descending. Use this to answer questions that combine any of status, "
    "jobtype, and resourcetype — e.g. 'running MCORE managed pilots', "
    "'how many SCORE user pilots are idle'. Filter the pivot rows by the relevant "
    "fields and sum nworkers.\n"
    "- total_records: number of Harvester records received from the API\n"
    "- from_dt / to_dt: the queried time window\n"
    "- site_filter: the site queried (null means all sites)\n"
    "- error: null means success\n"
    "Rules:\n"
    "- For single-dimension questions (e.g. 'how many running pilots') use the flat "
    "breakdown. For multi-dimensional questions use the pivot.\n"
    "- Always state the time window and site_filter so the user knows the scope.\n"
    "- If nworkers_total is 0, say no workers were found — do not invent numbers.\n"
    "- If error is non-null, explain the API could not be reached and suggest retrying.\n"
    "- Be concise. Lead with the number for count questions.\n"
)

_SYSTEM_SITE_HEALTH: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "You have retrieved two independent evidence sources for a site health question:\n\n"
    "1. [panda_harvester_workers] — live Harvester pilot/worker statistics.\n"
    "   Key fields: nworkers_total, nworkers_by_status (running/idle/submitted/failed), "
    "pivot ({status, jobtype, resourcetype, nworkers} rows), from_dt/to_dt, site_filter.\n\n"
    "2. [panda_jobs_query] — live job counts from the ingestion database.\n"
    "   Key fields: sql (the query executed), rows (result set), row_count, "
    "error (null means success — do NOT treat null as an error).\n\n"
    "Rules:\n"
    "- Present both results clearly, labelled as pilots and jobs respectively.\n"
    "- For pilots, lead with nworkers_by_status['running'] then total.\n"
    "- For jobs, answer directly from the rows/row_count in the evidence.\n"
    "- If either error field is non-null, explain that source failed and present "
    "the other source's data on its own.\n"
    "- Do not invent numbers from either source.\n"
    "- Keep the response concise — a short paragraph per source is enough unless "
    "the user asked for detail.\n"
)

_SYSTEM_PANDA_HEALTH: str = (
    "You are AskPanDA, an expert assistant for the PanDA workload management "
    "system and ATLAS experiment workflows at CERN.\n"
    "You have called the PanDA server liveness check (is_alive).\n"
    "The evidence contains:\n"
    "- is_alive: true if the server is alive and responding, false otherwise.\n"
    "- raw_response: the raw string returned by the PanDA MCP is_alive tool.\n"
    "- error: null on success; an error string if the MCP server could not be reached.\n"
    "Rules:\n"
    "- If error is non-null: report that the PanDA MCP server could not be reached "
    "and include the error message.\n"
    "- If is_alive is true: confirm the PanDA server is alive and responding. "
    "Include any useful detail from raw_response.\n"
    "- If is_alive is false: report that the PanDA server does not appear to be alive "
    "and include raw_response so the user can investigate.\n"
    "- Be concise — one or two sentences is enough.\n"
)

# ---------------------------------------------------------------------------
# RAG helpers (moved from bamboo_answer.py)
# ---------------------------------------------------------------------------

_NO_CONTEXT_SIGNALS: tuple[str, ...] = (
    "not installed",
    "chromadb path not found",
    "failed to connect",
    "no results found",
    "no keyword matches",
    "required and must not be empty",
)


def _extract_rag_context(result: object) -> str:
    """Return text from a retrieval tool result if it contains useful context.

    Args:
        result: Raw return value from a retrieval tool call.

    Returns:
        Extracted text string, or empty string if the result is an error or
        contains a no-context signal on its first line.
    """
    if isinstance(result, Exception) or not result:
        return ""
    if not isinstance(result, list) or not isinstance(result[0], dict):
        return ""
    text = str(result[0].get("text", ""))
    first_line = text.split("\n")[0].lower()
    if any(s in first_line for s in _NO_CONTEXT_SIGNALS):
        return ""
    return text


def _rag_hit_count(result: object, context: str) -> int:
    """Return the number of non-empty result lines, or -1 on retrieval error.

    Args:
        result: Raw return value from a retrieval tool call.
        context: Extracted context string for this result.

    Returns:
        Number of non-empty lines in the context, or -1 if the result was an
        exception.
    """
    if isinstance(result, Exception):
        return -1
    return len([ln for ln in context.splitlines() if ln.strip()])


async def _run_vector_search(question: str) -> str:
    """Run vector search inside its own tracing span.

    Args:
        question: User question to search for.

    Returns:
        Extracted context string, or empty string on failure.
    """
    from bamboo.tools.doc_rag import panda_doc_search_tool  # avoid circular at module level

    async with span(EVENT_RETRIEVAL, tool="panda_doc_search", backend="vector") as _s:
        try:
            result = await panda_doc_search_tool.call({"query": question, "top_k": 20})
        except Exception as exc:  # pylint: disable=broad-exception-caught
            result = exc  # type: ignore[assignment]
        ctx = _extract_rag_context(result)
        _s.set(hits=_rag_hit_count(result, ctx))
    return ctx


async def _run_bm25_search(question: str) -> str:
    """Run BM25 keyword search inside its own tracing span.

    Args:
        question: User question to search for.

    Returns:
        Extracted context string, or empty string on failure.
    """
    from bamboo.tools.doc_bm25 import panda_doc_bm25_tool  # avoid circular at module level

    async with span(EVENT_RETRIEVAL, tool="panda_doc_bm25", backend="bm25") as _s:
        try:
            result = await panda_doc_bm25_tool.call({"query": question, "top_k": 10})
        except Exception as exc:  # pylint: disable=broad-exception-caught
            result = exc  # type: ignore[assignment]
        ctx = _extract_rag_context(result)
        _s.set(hits=_rag_hit_count(result, ctx))
    return ctx


async def retrieve_rag_context(question: str) -> str:
    """Run vector and BM25 searches concurrently and merge results.

    Args:
        question: User question to retrieve context for.

    Returns:
        Merged context string, or empty string if both searches fail or return
        no useful content.
    """
    try:
        vec_ctx, bm25_ctx = await asyncio.gather(
            _run_vector_search(question),
            _run_bm25_search(question),
        )
        if vec_ctx and bm25_ctx:
            return f"{vec_ctx}\n\n--- Keyword search results ---\n{bm25_ctx}"
        return vec_ctx or bm25_ctx
    except Exception:  # pylint: disable=broad-exception-caught
        return ""


# ---------------------------------------------------------------------------
# Shared LLM call helpers (moved from bamboo_answer.py)
# ---------------------------------------------------------------------------


def _extract_delegated_text(delegated: Any) -> str:
    """Extract the text body from a delegated bamboo_llm_answer_tool result.

    Args:
        delegated: Raw return value from ``bamboo_llm_answer_tool.call()``.

    Returns:
        Plain text string from the first content block.
    """
    if delegated and isinstance(delegated[0], dict):
        return str(delegated[0].get("text", ""))
    return str(delegated)


# Maximum characters kept per assistant history message before truncation.
# A 400-char excerpt preserves enough for the model to resolve follow-up
# references without bloating multi-turn synthesis prompts.
_HISTORY_ASSISTANT_MAX_CHARS: int = 400


def _truncate_history(history: list[Message]) -> list[Message]:
    """Return a copy of history with long assistant messages truncated.

    User messages are kept verbatim (they're short questions).  Assistant
    messages are capped at ``_HISTORY_ASSISTANT_MAX_CHARS`` characters so
    that a long prior answer does not dominate the synthesis prompt on the
    next turn.

    Args:
        history: Prior conversation turns with ``role`` and ``content`` keys.

    Returns:
        New list with assistant content truncated where necessary.
    """
    out: list[Message] = []
    for msg in history:
        if msg.get("role") == "assistant":
            content = str(msg.get("content", ""))
            if len(content) > _HISTORY_ASSISTANT_MAX_CHARS:
                content = content[:_HISTORY_ASSISTANT_MAX_CHARS] + "…(truncated)"
            out.append({"role": "assistant", "content": content})
        else:
            out.append(msg)
    return out


async def call_llm(
    system: str,
    user: str,
    history: list[Message] | None = None,
    max_tokens: int = 2048,
) -> str:
    """Call the default LLM with a system + user prompt and return the text.

    Prior conversation turns (``history``) are inserted between the system
    prompt and the synthesised user message so the model can resolve follow-up
    questions.  Long assistant messages in history are truncated via
    :func:`_truncate_history` to prevent synthesis prompts from growing
    unbounded across multi-turn conversations.

    Args:
        system: System prompt string.
        user: Synthesised user prompt for the current turn.
        history: Optional list of prior ``{role, content}`` turns to inject
            between the system prompt and the current user message.  Must
            contain only ``"user"`` and ``"assistant"`` roles.
        max_tokens: Maximum tokens for the LLM response (default 2048).

    Returns:
        LLM response text.
    """
    messages: list[Message] = [{"role": "system", "content": system}]
    if history:
        messages.extend(_truncate_history(history))
    messages.append({"role": "user", "content": user})

    delegated = await bamboo_llm_answer_tool.call({
        "messages": messages,
        "max_tokens": max_tokens,
    })
    return _extract_delegated_text(delegated)


def unpack_tool_result(result: list[MCPContent]) -> dict[str, Any]:
    """Deserialise a JSON-wrapped MCPContent result from an internal tool.

    Internal tools (job_status, log_analysis, task_status) return a
    one-element ``list[MCPContent]`` whose ``text`` field contains the
    JSON-serialised ``{evidence, text}`` dict.  This helper unpacks that
    layer so callers can access ``result.get("evidence", ...)`` as before.

    Falls back to an empty dict if the result cannot be parsed, so callers
    always receive a dict regardless of upstream errors.

    Args:
        result: Raw return value from an internal tool ``call()`` method.

    Returns:
        Deserialised dict, or ``{}`` on parse failure.
    """
    try:
        if result and isinstance(result[0], dict):
            text = result[0].get("text", "")
            if isinstance(text, str) and text.strip().startswith("{"):
                return json.loads(text)  # type: ignore[no-any-return]
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    return {}


# ---------------------------------------------------------------------------
# Synthesis prompt selection
# ---------------------------------------------------------------------------


def _pick_synthesis_prompt(tool_names: list[str]) -> str:
    """Select the most appropriate synthesis system prompt for a set of tools.

    The priority order mirrors the original hard-wired routing logic in
    ``bamboo_answer._route()``, ensuring that specialist prompts are
    preferred over generic ones when a dedicated prompt exists.

    Args:
        tool_names: Names of the tools that were actually called during
            plan execution (in call order).

    Returns:
        System prompt string for the LLM synthesis step.
    """
    if "panda_log_analysis" in tool_names:
        return _SYSTEM_LOG_ANALYSIS
    if "pilot_source_analysis" in tool_names:
        return _SYSTEM_PILOT_SOURCE
    if "panda_job_status" in tool_names:
        return _SYSTEM_JOB
    if "panda_task_status" in tool_names:
        return _SYSTEM_TASK
    if "panda_server_health" in tool_names:
        return _SYSTEM_PANDA_HEALTH
    # Combined site-health: both harvester and jobs query in the same plan.
    if "panda_harvester_workers" in tool_names and "panda_jobs_query" in tool_names:
        return _SYSTEM_SITE_HEALTH
    if "panda_harvester_workers" in tool_names:
        return _SYSTEM_HARVESTER_WORKERS
    if "panda_jobs_query" in tool_names:
        return _SYSTEM_JOBS_QUERY
    if "cric_query" in tool_names:
        return _SYSTEM_CRIC_QUERY
    if any(t in tool_names for t in ("panda_doc_search", "panda_doc_bm25")):
        return _SYSTEM_RAG
    return _SYSTEM_GENERIC


# ---------------------------------------------------------------------------
# Plan execution
# ---------------------------------------------------------------------------


def _resolve_tool(tool_name: str, namespace: str | None, tools: dict[str, Any]) -> Any:
    """Resolve a tool object by name from the registry or entry points.

    Tries the static TOOLS registry first, then namespace-qualified entry-point
    lookup, then unqualified suffix-based lookup.

    Args:
        tool_name: Unqualified or qualified tool name to resolve.
        namespace: Optional namespace hint (e.g. ``"atlas"``).
        tools: The core TOOLS registry dict.

    Returns:
        Resolved tool object, or ``None`` if not found.
    """
    tool_obj: Any = tools.get(tool_name)
    if tool_obj is None and namespace:
        resolved = find_tool_by_name(tool_name, namespace=namespace)
        if resolved is not None:
            tool_obj = resolved.obj
    if tool_obj is None:
        resolved = find_tool_by_name(tool_name)
        if resolved is not None:
            tool_obj = resolved.obj
    return tool_obj


def _build_synthesis_prompt(
    called_tool_names: list[str],
    evidence_parts: list[str],
    question: str,
    errors: list[str],
    original_question: str | None = None,
) -> tuple[str, str]:
    """Build the system and user prompts for the synthesis LLM call.

    Selects a specialist prompt for known tool sets and falls back to a
    generic multi-tool prompt for unknown combinations.  RAG evidence is
    presented as documentation excerpts; other evidence as a merged block.

    When ``original_question`` differs from ``question`` (i.e. the user sent a
    content-free follow-up and ``question`` is the reformulated RAG query), the
    user prompt instructs the LLM to **expand** the prior answer rather than
    re-answer the original question from scratch.

    Args:
        called_tool_names: Names of tools that completed successfully.
        evidence_parts: One evidence string per successful tool call.
        question: Question used for retrieval (may be reformulated from history).
        errors: Error messages from any failed tool calls.
        original_question: The user's actual phrasing if it differs from
            ``question`` (e.g. "Tell me more please").  When provided, the
            synthesis prompt uses expansion framing instead of answer framing.

    Returns:
        Tuple of ``(system_prompt, user_prompt)`` strings.
    """
    rag_tools = {"panda_doc_search", "panda_doc_bm25"}
    plan_is_rag = any(t in rag_tools for t in called_tool_names)
    is_followup = (
        original_question is not None and original_question != question
    )

    if plan_is_rag:
        rag_context = "\n\n".join(evidence_parts)
        if rag_context:
            system = _SYSTEM_RAG
            if is_followup:
                user = (
                    f"The user asked a follow-up: {repr(original_question)}\n"
                    f"They want you to expand on the topic: {repr(question)}\n"
                    f"Using the retrieved documentation excerpts below, provide "
                    f"a more detailed explanation than before. "
                    f"Do not simply repeat what was said — go deeper, but be "
                    f"concise: aim for 200-300 words maximum.\n\n"
                    f"Retrieved documentation excerpts:\n{rag_context}\n"
                )
            else:
                user = (
                    f"User question:\n{question}\n\n"
                    f"Retrieved documentation excerpts:\n{rag_context}\n"
                )
        else:
            system = _SYSTEM_RAG_NO_CONTEXT
            user = f"User question:\n{question}\n"
    else:
        system = _pick_synthesis_prompt(called_tool_names)
        evidence_block = "\n\n".join(evidence_parts)
        user = (
            f"User question:\n{question}\n\n"
            f"Evidence from tool calls:\n{evidence_block}\n"
        )
        if errors:
            user += f"\nNote: the following tool calls failed: {'; '.join(errors)}\n"

    return system, user


# ---------------------------------------------------------------------------
# Direct formatting bypass for large CRIC full-list results
# ---------------------------------------------------------------------------

#: Minimum row count above which the CRIC full-list formatter is used instead
#: of LLM synthesis.  Below this threshold (e.g. a site-scoped query returning
#: a handful of queues) the LLM synthesises normally.
_CRIC_DIRECT_FORMAT_THRESHOLD: int = 100


def _format_cric_full_list(evidence: dict[str, Any]) -> str | None:
    """Format a full CRIC queue-list result directly, bypassing LLM synthesis.

    Called when ``cric_query`` returns a large, non-truncated set of individual
    queue rows.  Renders a plain-text table grouped by site, which is both
    lossless (no token-budget truncation) and faster than LLM synthesis.

    Returns ``None`` when the evidence does not meet the criteria for direct
    formatting (wrong shape, aggregation result, error present, etc.) so the
    caller falls through to normal LLM synthesis.

    Args:
        evidence: Unpacked evidence dict from ``cric_query``.

    Returns:
        Formatted plain-text string, or ``None`` to fall back to LLM synthesis.
    """
    # Only bypass for successful, non-truncated, non-aggregation results.
    if evidence.get("error"):
        return None
    rows: list[dict[str, Any]] = evidence.get("rows", [])
    row_count: int = evidence.get("row_count", 0)
    truncated: bool = evidence.get("truncated", False)
    columns: list[str] = evidence.get("columns", [])

    if truncated or row_count < _CRIC_DIRECT_FORMAT_THRESHOLD:
        return None

    # Aggregation results have no "queue" column — let the LLM handle those.
    if "queue" not in columns or "atlas_site" not in columns:
        return None

    # Build the table grouped by site.
    lines: list[str] = [f"{row_count} PanDA queues in CRIC:"]
    col_queue = "queue"
    col_site = "atlas_site"
    col_status = "status"
    col_type = "type"

    prev_site = ""
    for row in rows:
        site = str(row.get(col_site, ""))
        queue = str(row.get(col_queue, ""))
        status = str(row.get(col_status, ""))
        qtype = str(row.get(col_type, ""))
        site_label = site if site != prev_site else ""
        prev_site = site
        lines.append(f"  {site_label:<28}{queue:<32}{status:<12}{qtype}")

    return "\n".join(lines)


def _looks_like_fetch_cap_truncation(evidence: dict[str, Any]) -> bool:
    """Return True when cric_query evidence looks like a silently-truncated fetch.

    The NL-to-SQL pipeline uses ``fetchmany(MAX_ROWS + 1)`` where ``MAX_ROWS=50``.
    When the DB has more rows than that cap, ``truncated`` is set to ``True`` by
    :func:`_execute_query`.  However if the LLM-generated SQL already contained
    ``LIMIT 50`` (matching the cap exactly), ``truncated`` is ``False`` even though
    the full result set may be much larger.

    This heuristic detects that case: row_count equals exactly 50 (the cap), the
    result has individual queue columns (not an aggregation), and no error occurred.
    When true, the caller should re-query via ``list_all_queues`` to recover the
    full set before attempting direct formatting.

    Args:
        evidence: Evidence dict from ``_last_evidence_store["cric_query"]``.

    Returns:
        True when the evidence looks like a fetch-cap truncation.
    """
    from askpanda_atlas.cric_query_schema import MAX_ROWS  # deferred
    if evidence.get("error"):
        return False
    if evidence.get("truncated"):
        return False  # normal truncation — already flagged
    row_count = evidence.get("row_count", 0)
    if row_count != MAX_ROWS:
        return False
    columns = evidence.get("columns", [])
    return "queue" in columns and "atlas_site" in columns


def _try_cric_direct_format() -> str | None:
    """Attempt the CRIC full-list direct-format bypass.

    Reads the most recent ``cric_query`` evidence from ``_last_evidence_store``
    and tries two strategies:

    1. **Normal path**: :func:`_format_cric_full_list` qualifies the evidence
       (≥100 rows, not truncated, has queue+site columns) and formats it.
    2. **Fetch-cap recovery**: if the NL→SQL pipeline ran instead of the
       ``list_all_queues`` fast path (e.g. old deployed code), the evidence may
       have exactly ``MAX_ROWS=50`` rows and ``truncated=False`` — the Python
       ``fetchmany`` cap silently truncated without setting the flag.  When
       detected, ``list_all_queues`` is called directly to get the full set.

    The result is stored in two places so the TUI can retrieve it without going
    through the MCP pipe (bypasses the macOS 8 KB pipe-buffer limit).

    Returns:
        A short sentinel string ``"__CRIC_TABLE_READY__:<row_count>"`` when the
        direct-format path fires, or ``None`` to fall through to LLM synthesis.
    """
    _stored = _last_evidence_store.get("cric_query", {})
    # unpack_tool_result stores {"evidence": {...}} — unwrap if needed
    cric_evidence = _stored.get("evidence", _stored)

    # Strategy 1: normal path — evidence already has full row set.
    direct = _format_cric_full_list(cric_evidence)

    # Strategy 2: fetch-cap recovery.  If row_count == 50 and truncated is False
    # and the result has individual queue rows (not aggregation), the NL→SQL
    # pipeline ran with the default MAX_ROWS fetch cap and silently truncated.
    # Call list_all_queues directly to get the real full set.
    if direct is None and _looks_like_fetch_cap_truncation(cric_evidence):
        db_path = cric_evidence.get("db_path", "")
        try:
            from askpanda_atlas.cric_query_impl import list_all_queues  # deferred
            recovered = list_all_queues(db_path)
            if not recovered.get("error"):
                _last_evidence_store["cric_query"] = recovered
                cric_evidence = recovered
                direct = _format_cric_full_list(cric_evidence)
        except Exception:  # noqa: BLE001
            pass

    if direct is None:
        return None

    footnote = _db_footnote(["cric_query"])
    table_with_footnote = direct + footnote

    _last_evidence_store["_cric_direct_table"] = table_with_footnote

    cric_table_file = os.environ.get("BAMBOO_CRIC_TABLE_FILE")
    if cric_table_file:
        try:
            with open(cric_table_file, "w", encoding="utf-8") as _fh:
                _fh.write(table_with_footnote)
        except OSError:
            logger.warning("cric_query: failed to write table to %s", cric_table_file)

    row_count = cric_evidence.get("row_count", 0)
    return f"__CRIC_TABLE_READY__:{row_count}"


async def _execute_one_tool(
    tc: Any,
    called_tool_names: list[str],
    evidence_parts: list[str],
    errors: list[str],
) -> None:
    """Call a single tool from a plan and accumulate evidence or errors.

    Validates arguments, calls the tool, unpacks JSON evidence into
    ``_last_evidence_store``, and appends a compact evidence string to
    *evidence_parts*.  All failures are non-fatal — errors are appended to
    *errors* so the caller can attempt synthesis with partial evidence.

    Args:
        tc: Tool call descriptor with ``tool``, ``namespace``, and ``arguments``.
        called_tool_names: Mutable list of successfully-called tool names.
        evidence_parts: Mutable list of compact evidence strings for synthesis.
        errors: Mutable list of error strings accumulated across all tool calls.
    """
    from bamboo.core import TOOLS  # pylint: disable=import-outside-toplevel
    from bamboo.core import _validate_arguments  # pylint: disable=import-outside-toplevel

    tool_name: str = tc.tool
    args: dict[str, Any] = dict(tc.arguments)

    tool_obj = _resolve_tool(tool_name, tc.namespace, TOOLS)
    if tool_obj is None:
        errors.append(f"Unknown tool: {tool_name}")
        return

    get_def_fn = getattr(tool_obj, "get_definition", None)
    if callable(get_def_fn):
        try:
            tool_def: dict[str, Any] = get_def_fn()  # type: ignore[assignment]
        except Exception:  # pylint: disable=broad-exception-caught
            tool_def = {}
        err = _validate_arguments(tool_def, args)
        if err:
            errors.append(f"Invalid args for {tool_name}: {err}")
            return

    try:
        raw_result: list[MCPContent] = await tool_obj.call(args)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        errors.append(f"Tool {tool_name} raised: {exc!s}")
        return

    called_tool_names.append(tool_name)

    unpacked = unpack_tool_result(raw_result)
    if unpacked:
        _STORE_STRIP = {"pandaid_list"}
        _last_evidence_store[tool_name] = {
            k: v for k, v in unpacked.items() if k not in _STORE_STRIP
        }
        _last_evidence_store["last_tool"] = tool_name
        _LLM_STRIP = {"raw_payload", "pandaid_list"}
        llm_evidence = {k: v for k, v in unpacked.items() if k not in _LLM_STRIP}
        evidence_parts.append(f"[{tool_name}]\n{_compact_json(llm_evidence)}")
    else:
        raw_text = raw_result[0].get("text", "") if raw_result else ""
        if raw_text:
            evidence_parts.append(f"[{tool_name}]\n{raw_text}")


def _log_analysis_links_md() -> str:
    r"""Return the pre-built Markdown links block from the last log analysis call.

    Reads ``links_md`` from the ``panda_log_analysis`` evidence stored in
    ``_last_evidence_store``.  The value is built deterministically in
    ``log_analysis_impl.fetch_and_analyse`` from programmatic URLs, so it
    always contains real hrefs regardless of what the LLM produced.

    Returns:
        Markdown links block string (e.g. ``"\n\nLinks:\n- [BigPanDA Monitor](...)"``),
        or an empty string when no evidence is available.
    """
    stored = _last_evidence_store.get("panda_log_analysis", {})
    evidence = stored.get("evidence", stored)
    return str(evidence.get("links_md", ""))


def _strip_llm_links_section(body: str) -> str:
    """Remove any LLM-generated Links section from a synthesis response.

    The LLM may invent a ``Links:`` section with placeholder text or
    incorrect URLs.  This function strips everything from the last
    occurrence of a ``Links:`` heading to the end of the string so the
    canonical block from ``_log_analysis_links_md`` can be appended
    instead.

    Args:
        body: Raw LLM synthesis response text.

    Returns:
        Response text with any trailing Links section removed.
    """
    # Match a Links heading that appears on its own line (case-insensitive),
    # preceded by optional whitespace.  Strip from that point to end-of-string.
    stripped = re.sub(r"\n[^\n]*[Ll]inks:[^\n]*\n.*$", "", body, flags=re.DOTALL)
    return stripped.rstrip()


async def execute_plan(
    plan: Plan,
    question: str,
    history: list[Message],
    include_raw: bool = False,
    original_question: str | None = None,
) -> list[MCPContent]:
    """Execute a validated Plan and return a synthesised answer.

    Iterates ``plan.tool_calls`` in order, calls each tool, unpacks evidence,
    merges all evidence into a single synthesised LLM call.

    Unknown tools, validation failures, and individual tool call exceptions are
    handled gracefully — partial evidence from successful calls is still used
    for synthesis.  Only when *all* calls fail is a top-level error returned.

    Args:
        plan: Validated :class:`~bamboo.tools.planner.Plan` from the planner.
        question: Question string used for retrieval (may be reformulated).
        history: Prior conversation turns to inject into the LLM prompt.
        include_raw: If ``True``, include raw tool-result previews in the
            synthesised answer when errors are detected.
        original_question: The user's actual phrasing when ``question`` has
            been reformulated (e.g. for content-free follow-ups).  Passed to
            :func:`_build_synthesis_prompt` to enable expansion framing.

    Returns:
        One-element ``list[MCPContent]`` with the synthesised text answer.
    """
    evidence_parts: list[str] = []
    called_tool_names: list[str] = []
    errors: list[str] = []

    async with span(EVENT_PLAN, tool="bamboo_executor", plan=plan.model_dump()):
        pass  # Emit the plan as a trace event so the TUI /plan command can find it.

    for tc in plan.tool_calls:
        await _execute_one_tool(
            tc, called_tool_names, evidence_parts, errors
        )

    if not called_tool_names:
        error_summary = "; ".join(errors) if errors else "No tool calls in plan."
        return text_content(f"All tool calls failed: {error_summary}")

    # Direct-format bypass: for large CRIC full-list results, skip LLM synthesis.
    # Returns a short sentinel; the table is written to a temp file for the TUI.
    if called_tool_names == ["cric_query"]:
        sentinel = _try_cric_direct_format()
        if sentinel is not None:
            async with span(EVENT_SYNTHESIS, tool="bamboo_executor",
                            tools=called_tool_names, route=plan.route.value):
                pass  # emit span for tracing consistency
            return text_content(sentinel)

    system, user = _build_synthesis_prompt(
        called_tool_names, evidence_parts, question, errors,
        original_question=original_question,
    )

    async with span(EVENT_SYNTHESIS, tool="bamboo_executor",
                    tools=called_tool_names, route=plan.route.value):
        # Cap tokens at 600 for follow-up expansions; use 2048 normally.
        # For cric_query returning many individual-queue rows (direct-format
        # path missed), raise to 8192 so the LLM fallback does not truncate.
        if original_question is not None:
            synthesis_max_tokens = 600
        elif _is_large_cric_result(called_tool_names):
            synthesis_max_tokens = 8192
        else:
            synthesis_max_tokens = 2048
        body = await call_llm(system, user, history, max_tokens=synthesis_max_tokens)

    # For log analysis: strip any LLM-invented Links section and append the
    # canonical one built from programmatic URLs in log_analysis_impl.
    if "panda_log_analysis" in called_tool_names:
        body = _strip_llm_links_section(body)
        body += _log_analysis_links_md()

    return text_content(body + _db_footnote(called_tool_names))


def _is_large_cric_result(tool_names: list[str]) -> bool:
    """Return True when the last cric_query returned a large individual-queue result set.

    Used to raise the LLM synthesis token budget when the direct-format bypass
    did not fire (e.g. old deployed code) and the LLM must enumerate many rows.

    Args:
        tool_names: Names of the tools called in this plan execution.

    Returns:
        True when cric_query ran and returned >= threshold individual queue rows.
    """
    if "cric_query" not in tool_names:
        return False
    _stored = _last_evidence_store.get("cric_query", {})
    ev = _stored.get("evidence", _stored)
    if ev.get("row_count", 0) < _CRIC_DIRECT_FORMAT_THRESHOLD:
        return False
    return "queue" in ev.get("columns", [])


def _db_footnote(tool_names: list[str]) -> str:
    r"""Return a "Database last updated" footnote for DB-backed tool responses.

    Reads ``db_last_modified`` from ``_last_evidence_store`` for each tool in
    *tool_names* and returns a formatted footnote line.  Returns an empty string
    when no timestamp is available (errors, in-memory test DBs, non-DB tools).

    Args:
        tool_names: Names of the tools whose evidence to inspect.

    Returns:
        Footnote string like ``"\n\nDatabase last updated: 2026-04-07 10:31 UTC"``,
        or an empty string when no timestamp is found.
    """
    for name in tool_names:
        _stored = _last_evidence_store.get(name, {})
        evidence = _stored.get("evidence", _stored)
        ts = evidence.get("db_last_modified")
        if ts:
            return f"\n\nDatabase last updated: {ts}"
    return ""


def get_last_pilot_monitoring_evidence() -> dict[str, Any] | None:
    """Return the stored panda_log_analysis evidence if the last failure was a pilot_monitoring_error.

    Used by ``bamboo_answer._build_deterministic_plan`` to detect whether a
    follow-up question should be routed to ``pilot_source_analysis`` rather
    than the default job-status path.

    Returns:
        The ``evidence`` sub-dict from the last ``panda_log_analysis`` call
        when ``failure_type == "pilot_monitoring_error"`` and a non-empty
        ``log_excerpt`` is present; ``None`` otherwise.
    """
    stored = _last_evidence_store.get("panda_log_analysis", {})
    evidence = stored.get("evidence", stored)
    if (
        isinstance(evidence, dict)
        and evidence.get("failure_type") == "pilot_monitoring_error"
        and evidence.get("log_excerpt")
    ):
        return evidence
    return None


def _compact_json(obj: Any, limit: int = 12000) -> str:
    """Compact JSON for prompts, bounded to ``limit`` characters.

    Args:
        obj: Any JSON-serialisable object.
        limit: Maximum character count before truncation.

    Returns:
        Compact JSON string, truncated with an ellipsis if over ``limit``.
    """
    try:
        s = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except Exception:  # pylint: disable=broad-exception-caught
        s = str(obj)
    if len(s) > limit:
        return s[:limit] + "…(truncated)"
    return s


__all__ = [
    "execute_plan",
    "call_llm",
    "unpack_tool_result",
    "retrieve_rag_context",
    "_pick_synthesis_prompt",
    "get_last_pilot_monitoring_evidence",
    "_SYSTEM_LOG_ANALYSIS",
    "_SYSTEM_PILOT_SOURCE",
    "_SYSTEM_JOB",
    "_SYSTEM_TASK",
    "_SYSTEM_RAG",
    "_SYSTEM_RAG_NO_CONTEXT",
    "_SYSTEM_GENERIC",
    "_SYSTEM_JOBS_QUERY",
    "_SYSTEM_CRIC_QUERY",
    "_format_cric_full_list",
    "_CRIC_DIRECT_FORMAT_THRESHOLD",
    "_SYSTEM_HARVESTER_WORKERS",
    "_SYSTEM_SITE_HEALTH",
    "_SYSTEM_PANDA_HEALTH",
    "bamboo_last_evidence_tool",
]


class BambooLastEvidenceTool:
    """MCP tool that returns the evidence dict from the most recent tool call.

    ``execute_plan`` stores the unpacked evidence from every tool call in
    ``_last_evidence_store``.  This tool exposes that store so the TUI
    ``/json`` and ``/inspect`` commands can retrieve it without making a
    fresh HTTP request to BigPanDA.

    Two modes (controlled by the ``mode`` argument):

    ``"evidence"`` (default)
        The compact structured evidence dict — job counts, site breakdown,
        error tallies, sample job records.  This is what was sent to the LLM.

    ``"raw"``
        The verbatim BigPanDA API response stored under
        ``evidence["raw_payload"]``, if present.  Falls back to the full
        evidence dict if ``raw_payload`` is absent.
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for ``bamboo_last_evidence``.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "bamboo_last_evidence",
            "description": (
                "Return the evidence dict from the most recent panda_task_status "
                "or panda_log_analysis call.  Use mode='evidence' (default) for "
                "the compact structured summary, or mode='raw' for the verbatim "
                "BigPanDA API response."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["evidence", "raw", "table"],
                        "description": (
                            "'evidence' returns the compact LLM-facing evidence dict; "
                            "'raw' returns the verbatim BigPanDA API payload; "
                            "'table' returns the pre-formatted CRIC full-list table text."
                        ),
                    },
                    "tool": {
                        "type": "string",
                        "description": (
                            "Optional tool name to retrieve evidence for "
                            "(e.g. 'panda_task_status').  Defaults to the "
                            "most recently called tool."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        }

    async def call(self, arguments: dict[str, Any]) -> list[MCPContent]:
        """Return stored evidence from the last tool execution.

        Args:
            arguments: Dict with optional ``"mode"`` (``"evidence"`` or
                ``"raw"``) and optional ``"tool"`` name.

        Returns:
            One-element MCP content list with the JSON-serialised evidence,
            or an error message if no evidence is stored yet.
        """
        mode: str = str(arguments.get("mode") or "evidence")
        requested_tool: str | None = arguments.get("tool") or None

        if not _last_evidence_store:
            return text_content(json.dumps({
                "error": "No evidence stored yet — ask about a task or job first."
            }))

        tool_name = requested_tool or _last_evidence_store.get("last_tool")
        evidence = _last_evidence_store.get(str(tool_name), {}) if tool_name else {}

        if not evidence:
            return text_content(json.dumps({
                "error": f"No evidence stored for tool {tool_name!r}.",
                "available_tools": [k for k in _last_evidence_store if k != "last_tool"],
            }))

        if mode == "table":
            # Return the pre-formatted CRIC full-list table stored by the
            # direct-format bypass in execute_plan.  The table is keyed
            # separately so it survives independent of which tool ran last.
            table_text = _last_evidence_store.get("_cric_direct_table")
            if not table_text:
                return text_content(json.dumps({
                    "error": "No CRIC table available — ask to list all queues first.",
                }))
            return text_content(json.dumps({"table": table_text}))

        if mode == "raw":
            payload = evidence.get("raw_payload")
            result = payload if isinstance(payload, dict) else evidence
        else:
            # Return evidence without the raw_payload to keep it compact.
            result = {k: v for k, v in evidence.items() if k != "raw_payload"}

        return text_content(json.dumps({
            "tool": tool_name,
            "mode": mode,
            "evidence": result,
        }))


bamboo_last_evidence_tool = BambooLastEvidenceTool()
