"""CGSim vector-similarity documentation search tool.

Thin subclass of :class:`bamboo.tools.doc_rag.PandaDocSearchTool` that
overrides the tool description (so the Bamboo planner routes CGSim
documentation questions correctly) and the ChromaDB collection name default.

All query logic, result formatting, and caching behaviour are inherited
unchanged from the core tool.

Configuration
-------------
``BAMBOO_CHROMA_PATH``
    Path to the ChromaDB persistent directory.  Default: ``./chroma_db``

``BAMBOO_CHROMA_COLLECTION``
    ChromaDB collection name to query.  Default: ``cgsim_docs``

    Override this env var to use a different collection name.  The default
    is intentionally different from the ATLAS (``atlas_docs``) and ePIC
    (``epic_docs``) defaults so all three corpora can coexist in the same
    ChromaDB directory.
"""
from __future__ import annotations

import os
from typing import Any

from bamboo.tools.doc_rag import PandaDocSearchTool  # type: ignore[import-untyped]

_DEFAULT_CHROMA_PATH = "./chroma_db"
_CGSIM_DEFAULT_COLLECTION = "cgsim_docs"


class CgsimDocSearchTool(PandaDocSearchTool):
    """ChromaDB vector-search tool scoped to the CGSim documentation corpus.

    Inherits all query, caching, and formatting logic from
    :class:`~bamboo.tools.doc_rag.PandaDocSearchTool`.  Only the MCP tool
    description and the default ChromaDB collection name differ.
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for the CGSim documentation search.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "cgsim.doc_search",
            "description": (
                "Search the CGSim documentation for conceptual questions, "
                "how-to guidance, configuration options, or explanations of "
                "simulation behaviour.  Use when the question is about how "
                "CGSim or its underlying SimGrid framework works — for example "
                "plugin development, calibration methodology, network topology "
                "configuration, job lifecycle modelling, or the real-time "
                "monitoring dashboard.  "
                "Complements cgsim.doc_bm25 for exact-match lookups."
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
        """Initialise the ChromaDB client against the CGSim collection.

        Reads ``BAMBOO_CHROMA_PATH`` and ``BAMBOO_CHROMA_COLLECTION`` from the
        environment, defaulting the collection name to ``cgsim_docs`` rather
        than the core tool's ``bamboo_docs``.

        Returns:
            ``None`` on success, or a human-readable error string on failure.
        """
        if self._collection is not None:
            return None

        try:
            import chromadb  # type: ignore[import-untyped]
        except ImportError:
            return (
                "ChromaDB is not installed. "
                "Install it with: pip install -r requirements-rag.txt"
            )

        chroma_path: str = os.getenv("BAMBOO_CHROMA_PATH", _DEFAULT_CHROMA_PATH)
        collection_name: str = os.getenv(
            "BAMBOO_CHROMA_COLLECTION", _CGSIM_DEFAULT_COLLECTION
        )

        if not os.path.exists(chroma_path):
            return (
                f"ChromaDB path not found: '{chroma_path}'. "
                "Set BAMBOO_CHROMA_PATH to the directory created by the "
                "ingestion script, or run the ingestion script first."
            )

        try:
            self._client = chromadb.PersistentClient(path=chroma_path)
            self._collection = self._client.get_collection(name=collection_name)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._client = None
            self._collection = None
            return (
                f"Failed to connect to ChromaDB collection '{collection_name}': {exc}"
            )

        return None


cgsim_doc_search_tool = CgsimDocSearchTool()

__all__ = ["CgsimDocSearchTool", "cgsim_doc_search_tool"]
