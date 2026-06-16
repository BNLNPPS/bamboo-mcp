"""ATLAS BM25 keyword documentation search tool.

Thin subclass of :class:`bamboo.tools.doc_bm25.PandaDocBM25Tool` that
overrides the tool description so the planner routes ATLAS documentation
questions correctly, and sets :attr:`_default_topic` to ``"atlas"`` so
queries go to the ATLAS collection by default.

All BM25 index building, caching, and result formatting logic is inherited
unchanged from the core tool.

Configuration
-------------
``BAMBOO_CHROMA_PATH``
    Path to the ChromaDB persistent directory.  Default: ``./chroma_db``

``BAMBOO_CHROMA_COLLECTION_MAP``
    JSON object mapping topic keys to logical collection names.  The key
    ``"atlas"`` maps to ``atlas_docs`` by default.

``BAMBOO_CHROMA_COLLECTION``
    Scalar fallback used when the map is absent or has no ``"atlas"`` entry.
    Must match the value used by :mod:`askpanda_atlas.doc_rag` so both tools
    search the same corpus.
"""
from __future__ import annotations

from typing import Any

from bamboo.tools.doc_bm25 import PandaDocBM25Tool  # type: ignore[import-untyped]


class AtlasDocBM25Tool(PandaDocBM25Tool):
    """BM25 keyword search tool scoped to the ATLAS documentation corpus.

    Inherits all index-building, caching, and formatting logic from
    :class:`~bamboo.tools.doc_bm25.PandaDocBM25Tool`.  Only the MCP tool
    description and the default topic differ.
    """

    _default_topic: str = "atlas"

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for the ATLAS BM25 documentation search.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "panda_doc_bm25",
            "description": (
                "Search the ATLAS PanDA documentation by exact keyword match. "
                "Prefer over panda_doc_search when the question contains specific "
                "terms such as error codes, parameter names, class names, or "
                "command names where an exact match matters more than semantic "
                "similarity."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 10).",
                        "default": 10,
                    },
                    "topic": {
                        "type": "string",
                        "description": (
                            "Documentation collection to search.  One of: "
                            '"panda", "atlas" (default), "bamboo", '
                            '"rucio", "root", "epic", "cgsim".  '
                            "Controls which ChromaDB collection is queried via "
                            "BAMBOO_CHROMA_COLLECTION_MAP."
                        ),
                        "default": "atlas",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }


atlas_doc_bm25_tool = AtlasDocBM25Tool()

__all__ = ["AtlasDocBM25Tool", "atlas_doc_bm25_tool"]
