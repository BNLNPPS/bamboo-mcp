"""ePIC vector-similarity documentation search tool.

Thin subclass of :class:`bamboo.tools.doc_rag.PandaDocSearchTool` that
overrides the tool description (so the planner routes ePIC documentation
questions correctly) and the ChromaDB collection name default.

All query logic, result formatting, and caching behaviour are inherited
unchanged from the core tool.

Configuration
-------------
``BAMBOO_CHROMA_PATH``
    Path to the ChromaDB persistent directory.  Default: ``./chroma_db``

``BAMBOO_CHROMA_COLLECTION``
    ChromaDB collection name to query.  Default: ``epic_docs``

    Override this env var to use a different collection name.  The default
    is intentionally different from the core tool (``bamboo_docs``) so that
    ATLAS and ePIC collections can coexist in the same ChromaDB directory.
"""
from __future__ import annotations

import os
from typing import Any

from bamboo.tools.doc_rag import PandaDocSearchTool  # type: ignore[import-untyped]
from bamboo.tools._sqlite_compat import ensure_sqlite_compat  # type: ignore[import-untyped]
from bamboo.tools._chroma_routing import resolve_collection  # type: ignore[import-untyped]

import logging

LOG = logging.getLogger(__name__)

_DEFAULT_CHROMA_PATH = "./chroma_db"
_EPIC_DEFAULT_COLLECTION = "epic_docs"


class EpicDocSearchTool(PandaDocSearchTool):
    """ChromaDB vector-search tool scoped to the ePIC documentation corpus.

    Inherits all query, caching, and formatting logic from
    :class:`~bamboo.tools.doc_rag.PandaDocSearchTool`.  Only the MCP tool
    description and the default ChromaDB collection name differ.
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for the ePIC documentation search.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "panda_doc_search",
            "description": (
                "Search the ePIC / EIC PanDA documentation for conceptual "
                "questions, how-to guidance, configuration options, or "
                "explanations of system behaviour. Use when the question is about "
                "how something works in the ePIC experiment rather than the live "
                "status of a specific task or job. "
                "Complements panda_doc_bm25 for exact-match lookups."
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

    def _ensure_collection(self) -> str | None:
        """Initialise (or re-validate) the ChromaDB client against the ePIC collection.

        Re-reads the routing sidecar on every call to pick up blue/green slot
        swaps performed by the document-monitor agent without a server restart.
        If the resolved physical slot name has changed since the last call the
        cached collection handle is invalidated and reopened.

        Returns:
            ``None`` on success, or a human-readable error string on failure.
        """
        if not ensure_sqlite_compat():
            return (
                "System SQLite is too old for ChromaDB (need >= 3.35.0) and "
                "pysqlite3-binary is not installed.  "
                "Run: pip install pysqlite3-binary"
            )
        try:
            import chromadb  # type: ignore[import-untyped]
        except ImportError:
            return (
                "ChromaDB is not installed. "
                "Install it with: pip install -r requirements-rag.txt"
            )

        chroma_path: str = os.getenv("BAMBOO_CHROMA_PATH", _DEFAULT_CHROMA_PATH)
        logical_name: str = os.getenv(
            "BAMBOO_CHROMA_COLLECTION", _EPIC_DEFAULT_COLLECTION
        )

        if not os.path.exists(chroma_path):
            return (
                f"ChromaDB path not found: '{chroma_path}'. "
                "Set BAMBOO_CHROMA_PATH to the directory created by the "
                "ingestion script, or run the ingestion script first."
            )

        physical_name = resolve_collection(chroma_path, logical_name)

        if self._collection is not None and physical_name != self._resolved_physical:
            LOG.debug(
                "epic doc_rag: slot changed '%s' → '%s'; reopening collection",
                self._resolved_physical, physical_name,
            )
            self._collection = None
            self._client = None
            self._resolved_physical = None

        if self._collection is not None:
            return None

        try:
            self._client = chromadb.PersistentClient(path=chroma_path)
            self._collection = self._client.get_collection(name=physical_name)
            self._resolved_physical = physical_name
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._client = None
            self._collection = None
            self._resolved_physical = None
            return f"Failed to connect to ChromaDB collection '{physical_name}': {exc}"

        return None


epic_doc_search_tool = EpicDocSearchTool()

__all__ = ["EpicDocSearchTool", "epic_doc_search_tool"]
