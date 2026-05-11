"""ePIC BM25 keyword documentation search tool.

Thin subclass of :class:`bamboo.tools.doc_bm25.PandaDocBM25Tool` that
overrides the tool description (so the planner routes ePIC documentation
questions correctly) and the ChromaDB collection name default.

All BM25 index building, caching, and result formatting logic is inherited
unchanged from the core tool.

Configuration
-------------
``BAMBOO_CHROMA_PATH``
    Path to the ChromaDB persistent directory.  Default: ``./chroma_db``

``BAMBOO_CHROMA_COLLECTION``
    ChromaDB collection name to query.  Default: ``epic_docs``

    Must match the value used by :mod:`askpanda_epic.doc_rag` so both tools
    search the same corpus.
"""
from __future__ import annotations

import os
from typing import Any, cast

from bamboo.tools.doc_bm25 import PandaDocBM25Tool  # type: ignore[import-untyped]
from bamboo.tools._sqlite_compat import ensure_sqlite_compat  # type: ignore[import-untyped]

_DEFAULT_CHROMA_PATH = "./chroma_db"
_EPIC_DEFAULT_COLLECTION = "epic_docs"


class EpicDocBM25Tool(PandaDocBM25Tool):
    """BM25 keyword search tool scoped to the ePIC documentation corpus.

    Inherits all index-building, caching, and formatting logic from
    :class:`~bamboo.tools.doc_bm25.PandaDocBM25Tool`.  Only the MCP tool
    description and the default ChromaDB collection name differ.
    """

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool definition for the ePIC BM25 documentation search.

        Returns:
            Tool definition dict compatible with MCP discovery.
        """
        return {
            "name": "panda_doc_bm25",
            "description": (
                "Search the ePIC / EIC PanDA documentation by exact keyword match. "
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
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }

    def _ensure_index(self) -> str | None:
        """Load the ePIC corpus from ChromaDB and build (or refresh) the BM25 index.

        Reads ``BAMBOO_CHROMA_PATH`` and ``BAMBOO_CHROMA_COLLECTION`` at call
        time, defaulting the collection name to ``epic_docs`` rather than the
        core tool's ``bamboo_docs``.

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

        try:
            from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
        except ImportError:
            return (
                "rank_bm25 is not installed. "
                "Install it with: pip install -r requirements-rag.txt"
            )

        chroma_path: str = os.getenv("BAMBOO_CHROMA_PATH", _DEFAULT_CHROMA_PATH)
        collection_name: str = os.getenv(
            "BAMBOO_CHROMA_COLLECTION", _EPIC_DEFAULT_COLLECTION
        )

        if not os.path.exists(chroma_path):
            return (
                f"ChromaDB path not found: '{chroma_path}'. "
                "Set BAMBOO_CHROMA_PATH to the directory created by the ingestion script."
            )

        try:
            client = chromadb.PersistentClient(path=chroma_path)
            collection = client.get_collection(name=collection_name)
            current_count = collection.count()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return f"Failed to connect to ChromaDB collection '{collection_name}': {exc}"

        # Rebuild cache only when collection has changed.
        if self._bm25 is not None and current_count == self._cached_count:
            return None

        if current_count == 0:
            self._docs = []
            self._ids = []
            self._metadatas = []
            self._bm25 = BM25Okapi([[]])
            self._cached_count = 0
            return None

        try:
            batch_size = 500
            all_docs: list[str] = []
            all_ids: list[str] = []
            all_metas: list[dict[str, Any]] = []
            offset = 0
            while offset < current_count:
                batch = collection.get(
                    limit=batch_size,
                    offset=offset,
                    include=["documents", "metadatas"],
                )
                all_docs.extend(batch["documents"] or [])
                all_ids.extend(batch["ids"] or [])
                all_metas.extend(
                    cast(
                        list[dict[str, Any]],
                        batch["metadatas"] or [{}] * len(batch["ids"]),
                    )
                )
                offset += batch_size

            self._docs = all_docs
            self._ids = all_ids
            self._metadatas = all_metas
            from bamboo.tools.doc_bm25 import _tokenize  # type: ignore[import-untyped]
            self._bm25 = BM25Okapi([_tokenize(d) for d in all_docs])
            self._cached_count = current_count
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._bm25 = None
            self._cached_count = -1
            return f"Failed to build BM25 index: {exc}"

        return None


epic_doc_bm25_tool = EpicDocBM25Tool()

__all__ = ["EpicDocBM25Tool", "epic_doc_bm25_tool"]
