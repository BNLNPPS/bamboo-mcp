"""ePIC vector-similarity documentation search tool.

Thin subclass of :class:`bamboo.tools.doc_rag.PandaDocSearchTool` that
overrides the tool description so the planner routes ePIC documentation
questions correctly, and sets :attr:`_default_topic` to ``"epic"`` so
queries go to the ePIC collection by default.

All query logic, result formatting, blue/green slot resolution, and caching
behaviour are inherited unchanged from the core tool.

Configuration
-------------
``BAMBOO_CHROMA_PATH``
    Path to the ChromaDB persistent directory.  Default: ``./chroma_db``

``BAMBOO_CHROMA_COLLECTION_MAP``
    JSON object mapping topic keys to logical collection names.  The key
    ``"epic"`` maps to ``epic_docs`` by default.

``BAMBOO_CHROMA_COLLECTION``
    Scalar fallback used when the map is absent or has no ``"epic"`` entry.
"""
from __future__ import annotations

from typing import Any

from bamboo.tools.doc_rag import PandaDocSearchTool  # type: ignore[import-untyped]


class EpicDocSearchTool(PandaDocSearchTool):
    """ChromaDB vector-search tool scoped to the ePIC documentation corpus.

    Inherits all query, caching, and formatting logic from
    :class:`~bamboo.tools.doc_rag.PandaDocSearchTool`.  Only the MCP tool
    description and the default topic differ.
    """

    _default_topic: str = "epic"

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
                    "topic": {
                        "type": "string",
                        "description": (
                            "Documentation collection to search.  One of: "
                            '"panda", "atlas", "bamboo", '
                            '"rucio", "root", "epic" (default), "cgsim".  '
                            "Controls which ChromaDB collection is queried via "
                            "BAMBOO_CHROMA_COLLECTION_MAP."
                        ),
                        "default": "epic",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }


epic_doc_search_tool = EpicDocSearchTool()

__all__ = ["EpicDocSearchTool", "epic_doc_search_tool"]
