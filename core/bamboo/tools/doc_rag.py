"""ChromaDB-backed documentation retrieval tool.

Replaces the earlier dummy implementation with a real vector-store query
against a pre-built ChromaDB persistent collection.  The ingestion pipeline
that populates the collection is managed separately and is **not** part of
this module.

Configuration (environment variables):

    BAMBOO_CHROMA_PATH       Path to the ChromaDB persistent directory.
                             Default: ``./chroma_db``
    BAMBOO_CHROMA_COLLECTION Name of the ChromaDB collection to query.
                             Default: ``bamboo_docs``

The ChromaDB client is initialised lazily on the first ``call()`` and then
cached on the tool instance.  If the ``chromadb`` package is not installed,
or the configured path does not exist, the tool returns a human-readable
error message rather than raising — callers always receive a result.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from bamboo.tools._sqlite_compat import ensure_sqlite_compat
from bamboo.tools._chroma_routing import resolve_collection
from bamboo.tools.base import text_content

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CHROMA_PATH = "./chroma_db"
_DEFAULT_CHROMA_COLLECTION = "bamboo_docs"
_SNIPPET_MAX_CHARS = 500


class PandaDocSearchTool:
    """ChromaDB-backed document search tool for the Bamboo MCP server.

    Queries a pre-built ChromaDB vector store and returns the top-k most
    relevant document chunks together with their metadata and distance scores.

    The ChromaDB client and collection handle are initialised on the first
    call and cached for subsequent calls.  Call :meth:`_reset` to force
    re-initialisation (useful in tests).

    Attributes:
        _client: Cached ChromaDB ``PersistentClient`` instance, or ``None``
            if not yet initialised.
        _collection: Cached ChromaDB ``Collection`` handle, or ``None`` if
            not yet initialised.
    """

    def __init__(self) -> None:
        """Initialise the tool with no active ChromaDB client or collection."""
        self._client: Any = None
        self._collection: Any = None
        #: Physical collection name that _collection was opened against.
        #: Re-checked on every call; reset when the sidecar reports a new slot.
        self._resolved_physical: str | None = None

    # ------------------------------------------------------------------
    # Tool protocol
    # ------------------------------------------------------------------

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool discovery definition.

        Returns:
            Dict[str, Any]: Tool definition compatible with the MCP discovery
            format used by the Bamboo server.
        """
        return {
            "name": "panda_doc_search",
            "description": (
                "Search the PanDA and Bamboo documentation for conceptual "
                "questions, how-to guidance, configuration options, or "
                "explanations of system behaviour. Use when the question is about "
                "how something works rather than the live status of a specific "
                "task or job. Complements panda_doc_bm25 for exact-match lookups."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language question or keyword query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }

    async def call(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Query the ChromaDB collection and return the top-k results.

        Args:
            arguments: Mapping with the following keys:

                - ``query`` (*str*, required): The search query.
                - ``top_k`` (*int*, optional): Number of results; default 5,
                  clamped to the range [1, 20].

        Returns:
            List[Dict[str, Any]]: A one-element MCP text content list produced
            by :func:`~bamboo.tools.base.text_content`.  The text contains a
            numbered list of document snippets with metadata and distance
            scores, or an error message if retrieval failed.
        """
        query: str = str(arguments.get("query", "")).strip()
        if not query:
            return text_content("Error: 'query' argument is required and must not be empty.")

        top_k: int = max(1, min(int(arguments.get("top_k", 5)), 20))

        # Attempt lazy initialisation; returns an error string on failure.
        init_error = self._ensure_collection()
        if init_error:
            return text_content(init_error)

        # Query the collection.
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return text_content(f"ChromaDB query failed: {exc}")

        return text_content(self._format_results(query, results, top_k))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> str | None:
        """Initialise (or re-validate) the ChromaDB client and collection.

        Called on every :meth:`call` invocation.  Re-reads the routing sidecar
        (``<BAMBOO_CHROMA_PATH>/collection_routing.json``) to resolve the
        logical collection name to the currently live physical slot.  If the
        resolved physical name has changed since the last call — because the
        ``bamboo-mcp-services`` document-monitor agent completed a blue/green
        swap — the cached collection handle is invalidated and a new one is
        opened against the new slot.  This means the server picks up a freshly
        ingested corpus without requiring a restart.

        Returns:
            ``None`` on success, or a human-readable error string if
            initialisation failed (package missing, path absent, etc.).
        """
        # --- import guard ---------------------------------------------------
        if not ensure_sqlite_compat():
            return (
                "System SQLite is too old for ChromaDB (need >= 3.35.0) and "
                "pysqlite3-binary is not installed.  "
                "Run: pip install pysqlite3-binary"
            )
        try:
            import chromadb  # type: ignore[import-untyped]  # optional dep
        except ImportError:
            return (
                "ChromaDB is not installed.  "
                "Install it with: pip install -r requirements-rag.txt"
            )

        chroma_path: str = os.getenv("BAMBOO_CHROMA_PATH", _DEFAULT_CHROMA_PATH)
        logical_name: str = os.getenv(
            "BAMBOO_CHROMA_COLLECTION", _DEFAULT_CHROMA_COLLECTION
        )

        # --- path guard -----------------------------------------------------
        if not os.path.exists(chroma_path):
            return (
                f"ChromaDB path not found: '{chroma_path}'.  "
                "Set BAMBOO_CHROMA_PATH to the directory created by the "
                "ingestion script, or run the ingestion script first."
            )

        # --- live re-resolution via routing sidecar -------------------------
        physical_name = resolve_collection(chroma_path, logical_name)

        # Invalidate the cached handle if the active slot has changed.
        if self._collection is not None and physical_name != self._resolved_physical:
            LOG.debug(
                "doc_rag: slot changed '%s' → '%s'; reopening collection",
                self._resolved_physical, physical_name,
            )
            self._collection = None
            self._client = None
            self._resolved_physical = None

        if self._collection is not None:
            return None

        # --- connect --------------------------------------------------------
        try:
            self._client = chromadb.PersistentClient(path=chroma_path)
            self._collection = self._client.get_collection(name=physical_name)
            self._resolved_physical = physical_name
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Reset so the next call retries rather than using a broken handle.
            self._client = None
            self._collection = None
            self._resolved_physical = None
            return f"Failed to connect to ChromaDB collection '{physical_name}': {exc}"

        return None

    @staticmethod
    def _format_results(
        query: str,
        results: dict[str, Any],
        top_k: int,
    ) -> str:
        """Format raw ChromaDB query results into human-readable text.

        Args:
            query: The original search query (used in the header line).
            results: Raw result mapping returned by ``Collection.query()``.
            top_k: The requested number of results (used only for the header).

        Returns:
            str: Formatted multi-line string suitable for the MCP text payload.
        """
        documents: list[str] = (results.get("documents") or [[]])[0]
        metadatas: list[dict[str, Any]] = (results.get("metadatas") or [[]])[0]
        distances: list[float] = (results.get("distances") or [[]])[0]

        if not documents:
            return (
                f"No results found for query: '{query}'.\n"
                "The collection may be empty or the query may not match any documents."
            )

        lines: list[str] = [
            f"PanDA Doc Search — top {min(len(documents), top_k)} result(s) for: '{query}'\n"
        ]

        for i, (doc, meta, dist) in enumerate(
            zip(documents, metadatas, distances), start=1
        ):
            snippet = doc[:_SNIPPET_MAX_CHARS]
            if len(doc) > _SNIPPET_MAX_CHARS:
                snippet += " …"

            source: str = ""
            if meta:
                source_val = meta.get("source") or meta.get("file") or ""
                if source_val:
                    source = f"  source : {source_val}\n"

            score_pct = max(0.0, (1.0 - dist) * 100)

            lines.append(
                f"[{i}] score={score_pct:.1f}%  distance={dist:.4f}\n"
                f"{source}"
                f"  {snippet}\n"
            )

        return "\n".join(lines)

    def _reset(self) -> None:
        """Clear the cached client, collection, and resolved physical name.

        Intended for use in tests to force re-initialisation with different
        environment variables or mock objects.

        Returns:
            None
        """
        self._client = None
        self._collection = None
        self._resolved_physical = None


panda_doc_search_tool = PandaDocSearchTool()
