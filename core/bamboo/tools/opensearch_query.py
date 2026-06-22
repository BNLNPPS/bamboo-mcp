r"""General-purpose OpenSearch read-query tool for Bamboo MCP.

Exposes a single MCP tool — ``opensearch_query`` — that allows the LLM to
execute an arbitrary OpenSearch DSL query against any index pattern on the
CERN OpenSearch cluster, subject to a configurable allow-list.

The tool is intentionally low-level: the LLM constructs the DSL query dict
and this module handles authentication, allow-list validation, execution, and
result formatting.  Higher-level convenience tools (e.g.
``opensearch_promptlog_query``) should delegate to this one rather than
reimplementing the connection plumbing.

Security model
--------------
Requests are constrained at three layers:

1. **Index-pattern allow-list** — ``index_pattern`` must match one of the
   glob patterns listed in ``BAMBOO_OPENSEARCH_ALLOWED_INDICES`` (comma-
   separated; default: ``atlas_harvesterworkers-*,bamboomcp-promptlog-*``).
   Any request for an unlisted pattern is rejected before a connection is
   opened.

2. **Row cap** — ``max_hits`` is accepted from the caller but silently clamped
   to ``MAX_HITS_HARD_CAP`` (100) so a runaway query cannot flood the context
   window.

3. **Read-only credentials** — the tool uses ``ASKPANDA_OPENSEARCH``, the same
   read-only password used by the harvester timeseries tool.  It has no access
   to the write password (``BAMBOO_OPENSEARCH_PROMPTLOG``).

Environment variables
---------------------
``ASKPANDA_OPENSEARCH``
    HTTP Basic-auth **password** for read access.  **Required.**
    Shared with the harvester timeseries tool.

``BAMBOO_OPENSEARCH_ALLOWED_INDICES``
    Comma-separated list of index-pattern globs that may be queried.
    Default: ``atlas_harvesterworkers-*,bamboomcp-promptlog-*``

All other connection parameters (host, user, CA, TLS verification) are read
from the shared variables documented in
:mod:`bamboo.llm.opensearch_client`.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hard upper bound on the number of hits returned per query.
#: Callers may request fewer; they may never request more.
MAX_HITS_HARD_CAP: int = 100

#: Default number of hits when the caller does not specify ``max_hits``.
DEFAULT_MAX_HITS: int = 10

#: Default index patterns when ``BAMBOO_OPENSEARCH_ALLOWED_INDICES`` is unset.
_DEFAULT_ALLOWED_PATTERNS: str = (
    "atlas_harvesterworkers-*,bamboomcp-promptlog-*,atlas_panda_job_stats-*"
)


# ---------------------------------------------------------------------------
# Allow-list helpers
# ---------------------------------------------------------------------------


def _get_allowed_patterns() -> list[str]:
    """Return the list of allowed index-pattern globs.

    Reads ``BAMBOO_OPENSEARCH_ALLOWED_INDICES`` from the environment.  Each
    entry is stripped of surrounding whitespace; empty entries are discarded.

    Returns:
        Non-empty list of glob strings that index patterns may be matched
        against.
    """
    raw = os.environ.get(
        "BAMBOO_OPENSEARCH_ALLOWED_INDICES", _DEFAULT_ALLOWED_PATTERNS
    )
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_index_allowed(index_pattern: str) -> bool:
    """Return True when *index_pattern* matches at least one allowed glob.

    Uses :func:`fnmatch.fnmatch` so that ``bamboomcp-promptlog-*`` correctly
    matches both ``bamboomcp-promptlog-*`` (exact) and ``bamboomcp-*`` (wider
    glob requested by caller, also allowed if listed).

    Args:
        index_pattern: The index pattern string supplied by the caller.

    Returns:
        True if the pattern is permitted, False otherwise.
    """
    for allowed in _get_allowed_patterns():
        if fnmatch.fnmatch(index_pattern, allowed):
            return True
        # Also allow an exact match where the caller supplies a concrete index
        # name that falls inside an allowed wildcard pattern.
        if fnmatch.fnmatch(index_pattern, allowed):
            return True
    return False


# ---------------------------------------------------------------------------
# Core query (synchronous — runs inside asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_query(
    index_pattern: str,
    query: dict[str, Any],
    max_hits: int,
    source_fields: list[str] | None,
) -> dict[str, Any]:
    """Execute an OpenSearch DSL query synchronously.

    Intended to be called via :func:`asyncio.to_thread` so the blocking
    network I/O never stalls the event loop.  A fresh client is created for
    each call (same pattern as
    :func:`~askpanda_atlas.harvester_timeseries_impl.fetch_timeseries`).

    Args:
        index_pattern: OpenSearch index pattern, e.g. ``bamboomcp-promptlog-*``.
        query: Full OpenSearch DSL query dict (the ``body`` parameter of
            :meth:`opensearchpy.OpenSearch.search`).
        max_hits: Maximum number of hits to return; already clamped to
            :data:`MAX_HITS_HARD_CAP` by the caller.
        source_fields: Optional list of field names to project.  When
            ``None`` all fields are returned; when an empty list is supplied
            ``_source`` is disabled entirely.

    Returns:
        Dict with keys:

        - ``hits`` — list of ``_source`` dicts (one per document).
        - ``total`` — total number of matching documents (integer, not the
          OpenSearch object).
        - ``took_ms`` — server-side query time in milliseconds.
        - ``aggregations`` — aggregation result dict, or ``{}`` when the
          query contained no aggregations.

    Raises:
        RuntimeError: If ``ASKPANDA_OPENSEARCH`` is not set.
        ImportError: If ``opensearch-py`` is not installed.
        Exception: Propagated from the OpenSearch client on HTTP errors.
    """
    from bamboo.llm.opensearch_client import create_os_client  # local import

    password = os.environ.get("ASKPANDA_OPENSEARCH", "")
    if not password:
        raise RuntimeError(
            "ASKPANDA_OPENSEARCH is not set.  "
            "Set it to your OpenSearch read password to enable queries."
        )

    client = create_os_client(password)

    # Inject size into the query body so we honour max_hits.
    body = dict(query)
    body.setdefault("size", max_hits)
    body["size"] = min(int(body["size"]), max_hits)

    search_kwargs: dict[str, Any] = {
        "index": index_pattern,
        "body": body,
    }
    if source_fields is not None:
        search_kwargs["_source"] = source_fields if source_fields else False

    resp = client.search(**search_kwargs)

    hits_raw = resp.get("hits", {})
    total_raw = hits_raw.get("total", {})
    total: int = (
        total_raw.get("value", 0)
        if isinstance(total_raw, dict)
        else int(total_raw)
    )

    # Merge _source fields with _id so callers can reference the document
    # for follow-up operations (e.g. rating updates, individual entry lookup).
    hits: list[dict[str, Any]] = []
    for h in hits_raw.get("hits", []):
        entry = dict(h.get("_source", {}))
        if "_id" in h:
            entry["_id"] = h["_id"]
        hits.append(entry)

    return {
        "hits": hits,
        "total": total,
        "took_ms": resp.get("took", 0),
        "aggregations": resp.get("aggregations", {}),
    }


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


class OpenSearchQueryTool:
    r"""MCP tool for executing read-only DSL queries against OpenSearch.

    The LLM provides a complete OpenSearch DSL query as a JSON string along
    with the target index pattern.  This tool validates the request against an
    allow-list, executes it via a read-only credential, and returns structured
    results.

    Example call (natural-language question → LLM constructs the DSL):

    .. code-block:: json

        {
            "index_pattern": "bamboomcp-promptlog-*",
            "query": (
                "{\"query\":{\"term\":{\"provider\":\"gemini\"}},\"sort\":[{\"@timestamp\":\"desc\"}]}"
            ),
            "max_hits": 5,
            "source_fields": ["@timestamp", "provider", "model", "tools_used"]
        }

    The tool never exposes write credentials and will reject requests for
    index patterns not in the allow-list (``BAMBOO_OPENSEARCH_ALLOWED_INDICES``).
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        r"""Return the MCP tool definition for ``opensearch_query``.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "opensearch_query",
            "description": (
                "Execute a read-only OpenSearch DSL query against any allowed "
                "index on the CERN OpenSearch cluster "
                "(os-atlas.cern.ch).  Use this tool to search, filter, sort, "
                "and aggregate documents from indices such as "
                "bamboomcp-promptlog-* (Bamboo prompt/response logs) or "
                "atlas_harvesterworkers-* (Harvester pilot timeseries). "
                "The 'query' argument must be a valid OpenSearch DSL query "
                "serialised as a JSON string.  Results include hits (up to "
                "max_hits documents), the total match count, and any "
                "aggregation results. "
                "Requires ASKPANDA_OPENSEARCH to be set."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "index_pattern": {
                        "type": "string",
                        "description": (
                            "OpenSearch index pattern to query, e.g. "
                            "'bamboomcp-promptlog-*' or "
                            "'atlas_harvesterworkers-*'.  "
                            "Must match an entry in BAMBOO_OPENSEARCH_ALLOWED_INDICES."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Complete OpenSearch DSL query body serialised as "
                            "a JSON string.  Must be a JSON object.  The "
                            "'size' field is honoured but clamped to max_hits. "
                            r"Example: "
                            r'"{\"query\":{\"match_all\":{}},\"sort\":[{\"@timestamp\":\"desc\"}]}"'
                        ),
                    },
                    "max_hits": {
                        "type": "integer",
                        "description": (
                            f"Maximum number of documents to return "
                            f"(1–{MAX_HITS_HARD_CAP}, default {DEFAULT_MAX_HITS}).  "
                            f"Silently clamped to {MAX_HITS_HARD_CAP}."
                        ),
                        "default": DEFAULT_MAX_HITS,
                    },
                    "source_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of document fields to return.  "
                            "Omit to return all fields.  Use this to avoid "
                            "fetching large text fields (e.g. system_prompt, "
                            "user_prompt, response) when only metadata is "
                            "needed."
                        ),
                    },
                },
                "required": ["index_pattern", "query"],
                "additionalProperties": False,
            },
        }

    async def call(self, arguments: dict[str, Any]) -> Any:
        """Execute the OpenSearch query and return structured results.

        Validates the index pattern against the allow-list, parses the DSL
        JSON, clamps ``max_hits``, then runs the blocking query in a thread
        via :func:`asyncio.to_thread`.

        Args:
            arguments: MCP tool argument dict.  Expected keys:
                ``index_pattern`` (str), ``query`` (JSON str),
                ``max_hits`` (int, optional), ``source_fields`` (list, optional).

        Returns:
            One-element MCP content list with a JSON-serialised result dict
            containing ``hits``, ``total``, ``took_ms``, and ``aggregations``.
            On error, a JSON dict with a single ``error`` key is returned so
            the LLM can surface a meaningful message rather than seeing a tool
            exception.
        """
        from bamboo.tools.base import text_content  # local import avoids cycle

        index_pattern: str = arguments.get("index_pattern", "").strip()
        query_str: str = arguments.get("query", "").strip()
        max_hits: int = min(
            int(arguments.get("max_hits", DEFAULT_MAX_HITS)),
            MAX_HITS_HARD_CAP,
        )
        source_fields: list[str] | None = arguments.get("source_fields")

        # --- Allow-list check ---
        if not _is_index_allowed(index_pattern):
            allowed = _get_allowed_patterns()
            msg = (
                f"Index pattern '{index_pattern}' is not in the allow-list.  "
                f"Allowed patterns: {allowed}.  "
                f"To add a new pattern set BAMBOO_OPENSEARCH_ALLOWED_INDICES."
            )
            logger.warning("opensearch_query: %s", msg)
            return text_content(json.dumps({"error": msg}))

        # --- Parse DSL JSON ---
        try:
            query_body: dict[str, Any] = json.loads(query_str)
        except json.JSONDecodeError as exc:
            msg = f"'query' is not valid JSON: {exc}"
            logger.warning("opensearch_query: %s", msg)
            return text_content(json.dumps({"error": msg}))
        if not isinstance(query_body, dict):
            msg = "'query' must be a JSON object, not an array or scalar."
            return text_content(json.dumps({"error": msg}))

        # --- Execute (blocking I/O in a thread) ---
        try:
            result = await asyncio.to_thread(
                _run_query,
                index_pattern,
                query_body,
                max_hits,
                source_fields,
            )
        except RuntimeError as exc:
            logger.warning("opensearch_query: %s", exc)
            return text_content(json.dumps({"error": str(exc)}))
        except ImportError:
            msg = (
                "opensearch-py is not installed.  "
                "Install it with: pip install opensearch-py"
            )
            logger.warning("opensearch_query: %s", msg)
            return text_content(json.dumps({"error": msg}))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("opensearch_query: unexpected error: %s", exc)
            return text_content(json.dumps({"error": str(exc)}))

        logger.debug(
            "opensearch_query: index=%s hits=%d total=%d took_ms=%d",
            index_pattern,
            len(result["hits"]),
            result["total"],
            result["took_ms"],
        )
        return text_content(json.dumps(result))


opensearch_query_tool = OpenSearchQueryTool()

__all__ = [
    "opensearch_query_tool",
    "OpenSearchQueryTool",
    "MAX_HITS_HARD_CAP",
    "DEFAULT_MAX_HITS",
    "_is_index_allowed",
    "_get_allowed_patterns",
    "_run_query",
]
