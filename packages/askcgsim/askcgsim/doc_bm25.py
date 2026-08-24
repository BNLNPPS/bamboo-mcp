"""AskCGSim BM25 keyword documentation search tool.

Thin subclass of :class:`bamboo.tools.doc_bm25.PandaDocBM25Tool` that
overrides the tool description so the Bamboo planner routes AskCGSim
documentation questions correctly, and sets :attr:`_default_topic` to
``"cgsim"`` so queries go to the CGSim collection by default.

All BM25 index building, caching, and result formatting logic is inherited
unchanged from the core tool.

Configuration
-------------
``BAMBOO_CHROMA_PATH``
    Path to the ChromaDB persistent directory.  Default: ``./chroma_db``

``BAMBOO_CHROMA_COLLECTION_MAP``
    JSON object mapping topic keys to logical collection names.  The key
    ``"cgsim"`` maps to ``cgsim_docs`` by default.

``BAMBOO_CHROMA_COLLECTION``
    Scalar fallback used when the map is absent or has no ``"cgsim"`` entry.
    Must match the value used by :mod:`askcgsim.doc_rag` so both tools search
    the same corpus.
"""
from __future__ import annotations

from typing import Any

from bamboo.tools.doc_bm25 import PandaDocBM25Tool  # type: ignore[import-untyped]


class CgsimDocBM25Tool(PandaDocBM25Tool):
    """BM25 keyword search tool scoped to the AskCGSim documentation corpus.

    Inherits all index-building, caching, and formatting logic from
    :class:`~bamboo.tools.doc_bm25.PandaDocBM25Tool`.  Only the MCP tool
    description and the default topic differ.
    """

    _default_topic: str = "cgsim"

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for the AskCGSim BM25 documentation search.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "cgsim.doc_bm25",
            "description": (
                "Search the CGSim documentation by exact keyword match. "
                "Prefer over cgsim.doc_search when the question contains "
                "specific terms such as SimGrid API names, plugin method names "
                "(assignJob, getResourceInformation, onJobEnd, onSimulationEnd), "
                "configuration file keys, calibration parameter names, "
                "or CGSim CLI flags where an exact match matters more than "
                "semantic similarity."
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


cgsim_doc_bm25_tool = CgsimDocBM25Tool()

__all__ = ["CgsimDocBM25Tool", "cgsim_doc_bm25_tool"]
