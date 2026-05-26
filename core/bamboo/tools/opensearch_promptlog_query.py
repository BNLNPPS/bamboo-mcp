r"""Convenience MCP tool for querying Bamboo prompt/response logs in OpenSearch.

``opensearch_promptlog_query`` is a thin wrapper around
:class:`~bamboo.tools.opensearch_query.OpenSearchQueryTool` that pre-fills the
``bamboomcp-promptlog-*`` index pattern and provides a detailed description of
the document schema so the LLM can construct useful DSL queries without needing
to know the index name or field types.

Typical questions this tool can answer
---------------------------------------
- *"How many turns did my last session have?"*
- *"Which tools were used most frequently today?"*
- *"Show me the 5 most recent responses that used cric_query."*
- *"What is the average output token count per model this week?"*
- *"Replay session <uuid> in chronological order."*

Schema reference
----------------
Each document in ``bamboomcp-promptlog-*`` has the following fields:

.. code-block:: text

    @timestamp    (date)     UTC time of the LLM synthesis call
    session_id    (keyword)  UUID stable for one server process lifetime
    turn_number   (integer)  1-based counter within a session
    provider      (keyword)  e.g. "gemini", "openai", "anthropic", "mistral"
    model         (keyword)  e.g. "gemini-2.0-flash"
    max_tokens    (integer)  token budget passed to the LLM
    system_prompt (text)     redacted system prompt
    user_prompt   (text)     redacted synthesis prompt (question + evidence)
    response      (text)     redacted LLM response
    tools_used    (keyword)  array of MCP tool names called this turn
    input_tokens  (integer)  input token count, or null
    output_tokens (integer)  output token count, or null

Note: ``system_prompt``, ``user_prompt``, and ``response`` are pseudonymised
— personal identifiers are replaced with ``user_XXXXXXXX`` tokens before
storage.  Avoid fetching these large text fields unless they are specifically
needed; use ``source_fields`` to project only the metadata columns.
"""
from __future__ import annotations

import logging
from typing import Any

from bamboo.tools.opensearch_query import opensearch_query_tool

logger = logging.getLogger(__name__)

#: Fixed index pattern for all prompt-log queries.
PROMPTLOG_INDEX_PATTERN: str = "bamboomcp-promptlog-*"

#: Fields that are safe and cheap to fetch by default (no large text blobs).
DEFAULT_SOURCE_FIELDS: list[str] = [
    "@timestamp",
    "session_id",
    "turn_number",
    "provider",
    "model",
    "max_tokens",
    "tools_used",
    "input_tokens",
    "output_tokens",
]


class OpenSearchPromptlogQueryTool:
    r"""MCP convenience tool for querying ``bamboomcp-promptlog-*``.

    Delegates to :class:`~bamboo.tools.opensearch_query.OpenSearchQueryTool`
    with the index pattern pre-filled.  The caller supplies only the DSL query
    body; all other connection parameters are resolved from environment
    variables.

    By default, the large text fields (``system_prompt``, ``user_prompt``,
    ``response``) are **excluded** from results unless the caller explicitly
    adds them to ``source_fields``.  This keeps context-window usage low for
    metadata-only queries.
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        r"""Return the MCP tool definition for ``opensearch_promptlog_query``.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "opensearch_promptlog_query",
            "description": (
                "Query Bamboo's prompt/response log index (bamboomcp-promptlog-*) "
                "in OpenSearch.  Use this tool to analyse past Bamboo sessions: "
                "count turns, inspect tool usage patterns, compare token costs "
                "across providers/models, replay a specific session, or find "
                "responses that used a particular tool.\n\n"
                "Document schema (per turn):\n"
                "  @timestamp    (date)     UTC time of LLM call\n"
                "  session_id    (keyword)  UUID stable for one process lifetime\n"
                "  turn_number   (integer)  1-based turn counter within session\n"
                "  provider      (keyword)  gemini | openai | anthropic | mistral\n"
                "  model         (keyword)  e.g. gemini-2.0-flash\n"
                "  max_tokens    (integer)  token budget\n"
                "  tools_used    (keyword)  array of MCP tool names\n"
                "  input_tokens  (integer)  input token count (null if unavailable)\n"
                "  output_tokens (integer)  output token count (null if unavailable)\n"
                "  system_prompt (text)     redacted system prompt (large; omitted by default)\n"
                "  user_prompt   (text+keyword) redacted synthesis prompt — use "
                "user_prompt.keyword for aggregations/terms, user_prompt for "
                "full-text search\n"
                "  response      (text)     redacted LLM response (large; "
                "omitted by default)\n"
                "  NOTE: there is no user_id field.  session_id identifies a "
                "  server process lifetime; multiple users may share a deployment "
                "  and each will have their own session_id per server restart. "
                "  Queries for 'all questions today' or 'most frequent questions' "
                "  should NOT filter by session_id — omit the session filter to "
                "  return data across all sessions and all users.\n"
                "  The user_prompt field contains GDPR pseudonyms in the form "
                "  'user_XXXXXXXX' (e.g. user_b6f7494e) — these are "
                "  embedded inside the text, not a separate field. "
                "  To find turns from a specific pseudonym use a full-text match: "
                "  {\"query\":{\"match\":{\"user_prompt\":\"user_b6f7494e\"}}}. "
                "  Do NOT use term queries or filter on a 'user_id' field — "
                "  it does not exist.\n\n"
                "source_fields guidance: system_prompt, user_prompt, and response "
                "are omitted by default.  RULES: (a) For display queries (show "
                "recent turns, replay session) — do NOT put size:0 in the DSL; "
                "omit size entirely and let max_hits control the result count; add "
                "user_prompt to source_fields to show what was asked. "
                "(b) For aggregation-only queries (counts, averages, groupings) — "
                "put size:0 in the DSL AND pass source_fields=[].\n\n"
                "IMPORTANT RULES for building queries:\n"
                "1. Date math: always use OpenSearch date math for temporal filters, "
                "   e.g. 'today' = gte:now/d, 'this week' = gte:now-7d/d, "
                "   'last hour' = gte:now-1h.  NEVER use hardcoded date strings.\n"
                "2. Counting turns: to count how many turns a session has, use a "
                "   value_count aggregation with size:0.  IMPORTANT: the 'total' "
                "   field in the response envelope is the number of documents that "
                "   matched the query filter BEFORE the size limit — it is NOT the "
                "   value_count result.  Read the turn count from "
                "   aggregations.turns.value instead of from total.\n"
                "3. Finding the current session: the session_id is not known in "
                "   advance.  To find 'my last session', first query "
                "   sort:[{@timestamp:desc}],size:1 to get the most recent document, "
                "   read its session_id, then issue a second query filtered on that "
                "   session_id.\n"
                "4. keyword fields (session_id, provider, model, tools_used) require "
                "   exact 'term' queries, not 'match'.  If a term query on "
                "   session_id returns 0 results despite the session being known to "
                "   exist, the field may be mapped as text (index created before the "
                "   template was applied).  In that case try "
                "   {\"term\":{\"session_id.keyword\":\"<uuid>\"}} as a fallback.\n"
                "   text fields (user_prompt, response, system_prompt) CANNOT be "
                "   used in terms aggregations directly — you MUST use the .keyword "
                "   sub-field or results will be wrong: user_prompt.keyword, "
                "   response.keyword.  This is the most common mistake — "
                "   user_prompt aggregations without .keyword return token buckets "
                "   (individual words) instead of full questions.\n"
                "5. The id= value shown in the TUI system panel after each turn "
                "   (e.g. id='5rNAUJ4By913oBqgRMjO') is the OpenSearch document _id, "
                "   NOT the session_id.  To replay a session, use the UUID-format "
                "   session_id shown as session=\"...\" in the same panel, "
                "   e.g. session='3d07407b-65ef-400c-b4d3-47c49466f791'.\n"
                "6. tools_used contains the names of all MCP tools called during "
                "   the turn's evidence-gathering phase, including built-ins such as "
                "   opensearch_promptlog_query and opensearch_query.  A turn that "
                "   only asked a conversational question with no tool calls will "
                "   have tools_used=[].\n\n"
                "Example queries (pass as JSON strings in the 'query' argument):\n"
                r'  5 most recent turns (any session):'
                "\n"
                r'    {"query":{"match_all":{}},"sort":[{"@timestamp":"desc"}],"size":5}'
                "\n"
                r'  Turn count for a known session (use value_count agg):'
                "\n"
                r'    {"query":{"term":{"session_id":"<uuid>"}},'
                r'"aggs":{"turns":{"value_count":{"field":"turn_number"}}},"size":0}'
                "\n"
                r'  Replay a session in order (display — no size:0, add user_prompt):'
                "\n"
                r'    {"query":{"term":{"session_id":"<uuid>"}},'
                r'"sort":[{"turn_number":{"order":"asc"}}]}'
                "  + source_fields="
                "[\"@timestamp\",\"turn_number\",\"tools_used\",\"user_prompt\"]\n"
                "\n"
                r'  Tool usage counts today (date math, agg only):'
                "\n"
                r'    {"query":{"range":{"@timestamp":{"gte":"now/d"}}},'
                r'"aggs":{"tools":{"terms":{"field":"tools_used","size":20}}},"size":0}'
                "\n"
                r'  Average output tokens per model this week:'
                "\n"
                r'    {"query":{"range":{"@timestamp":{"gte":"now-7d/d"}}},'
                r'"aggs":{"by_model":{"terms":{"field":"model"},'
                r'"aggs":{"avg_out":{"avg":{"field":"output_tokens"}}}}},"size":0}'
                "\n"
                r'  Turns using cric_query:'
                "\n"
                r'    {"query":{"term":{"tools_used":"cric_query"}},'
                r'"sort":[{"@timestamp":{"order":"desc"}}]}'
                "\n"
                r'  Count turns per session today:'
                "\n"
                r'    {"query":{"range":{"@timestamp":{"gte":"now/d"}}},'
                r'"aggs":{"by_session":{"terms":{"field":"session_id","size":50},'
                r'"aggs":{"turns":{"value_count":{"field":"turn_number"}}}}},"size":0}'
                "\n"
                r'  Count how many times a specific tool was called today:'
                "\n"
                r'    {"query":{"bool":{"must":['
                r'      {"range":{"@timestamp":{"gte":"now/d"}}},'
                r'      {"term":{"tools_used":"opensearch_promptlog_query"}}]}},'
                r'"aggs":{"count":{"value_count":{"field":"turn_number"}}},"size":0}'
                "\n"
                r'  Most frequently asked questions all time'
                "  (MUST use user_prompt.keyword not user_prompt):\n"
                r'    {"aggs":{"faq":{"terms":{"field":"user_prompt.keyword",'
                r'"size":20}}},"size":0}'
                "\n"
                r'  Most frequently asked questions today:'
                "\n"
                r'    {"query":{"range":{"@timestamp":{"gte":"now/d"}}},'
                r'"aggs":{"faq":{"terms":{"field":"user_prompt.keyword",'
                r'"size":20}}},"size":0}'
                "\n"
                r'  Show recent turns including what was asked (display — no size:0):'
                "\n"
                r'    {"query":{"match_all":{}},"sort":[{"@timestamp":{"order":"desc"}}]}'
                "  + source_fields="
                "[\"@timestamp\",\"turn_number\",\"tools_used\",\"user_prompt\"]\n"
                r'  Show all rated responses today:'
                "\n"
                r'    {"query":{"bool":{"must":['
                r'      {"range":{"@timestamp":{"gte":"now/d"}}},'
                r'      {"range":{"rating":{"gte":1}}}]}},'
                r'"sort":[{"rating":{"order":"desc"}}]}'
                "\n"
                r'  Average rating per model (all time):'
                "\n"
                r'    {"query":{"range":{"rating":{"gte":1}}},'
                r'"aggs":{"by_model":{"terms":{"field":"model"},'
                r'"aggs":{"avg":{"avg":{"field":"rating"}}}}},"size":0}'
                "\n"
                "NOTE: rating is null when not yet rated — always filter "
                "with range:{rating:{gte:1}} to exclude unrated turns.\n"
                r'  Rated turns as a table (include _id for individual lookup):'
                "\n"
                r'    {"query":{"range":{"rating":{"gte":1}}},'
                r'"sort":[{"rating":{"order":"desc"}},{"@timestamp":{"order":"desc"}}]}'
                " + source_fields=[\"@timestamp\",\"turn_number\","
                "\"session_id\",\"model\",\"tools_used\","
                "\"user_prompt\",\"rating\",\"_id\"]\n"
                "NOTE: each hit includes _id (the OpenSearch document id). "
                "To show a full entry for a specific _id, use opensearch_query "
                "with {\"query\":{\"ids\":{\"values\":[\"<_id>\"]}}}.\n"
                "When asked for a table of ratings, format the result as a "
                "markdown table with columns: Doc ID (_id), Time, Turn, "
                "Session (first 8 chars only), Model, Tools, "
                "Question (truncated to 60 chars), Rating. "
                "Always include the full _id value in the Doc ID column — "
                "it is the only reliable way to fetch the full response "
                "for a specific entry via opensearch_query ids query.\n"
                "Requires ASKPANDA_OPENSEARCH to be set (same credential as "
                "harvester timeseries)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "OpenSearch DSL query body serialised as a JSON "
                            "string.  The index is always bamboomcp-promptlog-*. "
                            r"Example: "
                            r'"{\"query\":{\"match_all\":{}},\"sort\":[{\"@timestamp\":\"desc\"}]}"'
                        ),
                    },
                    "max_hits": {
                        "type": "integer",
                        "description": (
                            "Maximum number of documents to return (1–100, "
                            "default 10).  For aggregation-only queries set "
                            "this to 0 and include 'size':0 in the DSL."
                        ),
                        "default": 10,
                    },
                    "source_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Fields to include in each hit.  Defaults to all "
                            "metadata fields (excludes system_prompt, "
                            "user_prompt, response).  Pass an explicit list "
                            "to include or exclude specific fields.  Pass an "
                            "empty list to suppress _source entirely (useful "
                            "for aggregation-only queries)."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }

    async def call(self, arguments: dict[str, Any]) -> Any:
        """Execute the prompt-log query via the general opensearch_query tool.

        Injects the ``bamboomcp-promptlog-*`` index pattern and, when the
        caller has not specified ``source_fields``, applies the default
        projection that excludes the three large text fields.

        Args:
            arguments: MCP tool argument dict.  Expected keys: ``query``
                (JSON str), ``max_hits`` (int, optional), ``source_fields``
                (list, optional).

        Returns:
            One-element MCP content list with JSON-serialised result dict
            (``hits``, ``total``, ``took_ms``, ``aggregations``).
        """
        # Apply default source-field projection only when the caller hasn't
        # specified their own; an empty list is a valid explicit choice (no _source).
        if "source_fields" not in arguments:
            arguments = dict(arguments)
            arguments["source_fields"] = DEFAULT_SOURCE_FIELDS

        # Inject the fixed index pattern.
        forwarded = dict(arguments)
        forwarded["index_pattern"] = PROMPTLOG_INDEX_PATTERN

        logger.debug(
            "opensearch_promptlog_query: forwarding to opensearch_query "
            "with index=%s max_hits=%s",
            PROMPTLOG_INDEX_PATTERN,
            forwarded.get("max_hits", 10),
        )
        return await opensearch_query_tool.call(forwarded)


opensearch_promptlog_query_tool = OpenSearchPromptlogQueryTool()

__all__ = [
    "opensearch_promptlog_query_tool",
    "OpenSearchPromptlogQueryTool",
    "PROMPTLOG_INDEX_PATTERN",
    "DEFAULT_SOURCE_FIELDS",
]
