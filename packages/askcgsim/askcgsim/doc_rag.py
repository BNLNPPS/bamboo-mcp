"""AskCGSim vector-similarity documentation search tool.

Thin subclass of :class:`bamboo.tools.doc_rag.PandaDocSearchTool` that
overrides the tool description so the Bamboo planner routes AskCGSim
documentation questions correctly, and sets :attr:`_default_topic` to
``"cgsim"`` so queries go to the CGSim collection by default.

All query logic, result formatting, blue/green slot resolution, and caching
behaviour are inherited unchanged from the core tool.

Configuration
-------------
``BAMBOO_CHROMA_PATH``
    Path to the ChromaDB persistent directory.  Default: ``./chroma_db``

``BAMBOO_CHROMA_COLLECTION_MAP``
    JSON object mapping topic keys to logical collection names.  The key
    ``"cgsim"`` maps to ``cgsim_docs`` by default.

``BAMBOO_CHROMA_COLLECTION``
    Scalar fallback used when the map is absent or has no ``"cgsim"`` entry.
"""
from __future__ import annotations

from typing import Any

from bamboo.tools.doc_rag import PandaDocSearchTool  # type: ignore[import-untyped]


class CgsimDocSearchTool(PandaDocSearchTool):
    """ChromaDB vector-search tool scoped to the AskCGSim documentation corpus.

    Inherits all query, caching, and formatting logic from
    :class:`~bamboo.tools.doc_rag.PandaDocSearchTool`.  Only the MCP tool
    description and the default topic differ.
    """

    _default_topic: str = "cgsim"

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for the AskCGSim documentation search.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "cgsim.doc_search",
            "description": (
                "Search the CGSim documentation for conceptual questions, "
                "how-to guidance, configuration options, or explanations of "
                "simulation behaviour.  Use when the question is about how "
                "CGSim or its underlying SimGrid framework works -- for example "
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
                    "topic": {
                        "type": "string",
                        "description": (
                            "Documentation collection to search.  "
                            "Defaults to \"cgsim\".  Override via "
                            "BAMBOO_CHROMA_COLLECTION_MAP."
                        ),
                        "default": "cgsim",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }


cgsim_doc_search_tool = CgsimDocSearchTool()

__all__ = ["CgsimDocSearchTool", "cgsim_doc_search_tool"]
