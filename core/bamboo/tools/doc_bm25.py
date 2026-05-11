"""BM25 keyword search tool for the Bamboo MCP server.

Complements the vector-similarity ``panda_doc_search`` tool with a classical
term-frequency search that excels at exact-match and enumeration queries such
as "list all error codes" or "what is BADALLOC?".

The tool loads all documents from the same ChromaDB collection used by the
vector search tool, builds a BM25 index in memory on first call, and caches
it for subsequent calls.  The cache is invalidated whenever the collection
document count changes (e.g. after re-ingestion).

Configuration (same env vars as ``doc_rag.py``):

    BAMBOO_CHROMA_PATH       Path to the ChromaDB persistent directory.
                             Default: ``./chroma_db``
    BAMBOO_CHROMA_COLLECTION Name of the ChromaDB collection to query.
                             Default: ``bamboo_docs``

Dependencies:
    chromadb   — to load the corpus (already required by doc_rag)
    rank_bm25  — lightweight BM25 implementation (add to requirements-rag.txt)
"""
from __future__ import annotations

import os
import re
from typing import Any, cast

from bamboo.tools._sqlite_compat import ensure_sqlite_compat
from bamboo.tools.base import text_content

_DEFAULT_CHROMA_PATH = "./chroma_db"
_DEFAULT_CHROMA_COLLECTION = "bamboo_docs"
_SNIPPET_MAX_CHARS = 500


def _tokenize(text: str) -> list[str]:
    """Lowercase and split text into tokens for BM25 indexing.

    Args:
        text: Raw document or query text.

    Returns:
        List of lowercase alphanumeric tokens.
    """
    return re.findall(r"[a-z0-9_]+", text.lower())


class PandaDocBM25Tool:
    """BM25 keyword search over the Bamboo documentation ChromaDB corpus.

    Loads all documents from the ChromaDB collection on first call, builds a
    BM25 index, and caches both.  The cache is refreshed automatically when
    the collection document count changes.

    Attributes:
        _docs: Cached list of document text strings.
        _ids: Cached list of document IDs corresponding to ``_docs``.
        _metadatas: Cached list of metadata dicts corresponding to ``_docs``.
        _bm25: Cached ``BM25Okapi`` index, or ``None`` if not yet built.
        _cached_count: Document count at the time the cache was last built.
    """

    def __init__(self) -> None:
        """Initialise the tool with an empty cache."""
        self._docs: list[str] = []
        self._ids: list[str] = []
        self._metadatas: list[dict[str, Any]] = []
        self._bm25: Any = None
        self._cached_count: int = -1

    # ------------------------------------------------------------------
    # Tool protocol
    # ------------------------------------------------------------------

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return the MCP tool discovery definition.

        Returns:
            Dict[str, Any]: Tool definition compatible with MCP discovery.
        """
        return {
            "name": "panda_doc_bm25",
            "description": (
                "Search the PanDA and Bamboo documentation by exact keyword match. "
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

    async def call(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a BM25 keyword search over the documentation corpus.

        Args:
            arguments: Mapping with the following keys:

                - ``query`` (*str*, required): Keyword search query.
                - ``top_k`` (*int*, optional): Number of results; default 10,
                  clamped to [1, 50].

        Returns:
            List[Dict[str, Any]]: A one-element MCP text content list with
            ranked keyword-match results, or an error message if the corpus
            could not be loaded.
        """
        query: str = str(arguments.get("query", "")).strip()
        if not query:
            return text_content("Error: 'query' argument is required and must not be empty.")

        top_k: int = max(1, min(int(arguments.get("top_k", 10)), 50))

        init_error = self._ensure_index()
        if init_error:
            return text_content(init_error)

        if not self._docs:
            return text_content(
                f"No results found for query: '{query}'.\n"
                "The collection appears to be empty."
            )

        try:
            #from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]  # optional dep
            tokens = _tokenize(query)
            scores: list[float] = self._bm25.get_scores(tokens).tolist()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return text_content(f"BM25 search failed: {exc}")

        # Rank by score descending, filter zero-score results.
        ranked = sorted(
            ((i, s) for i, s in enumerate(scores) if s > 0.0),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

        if not ranked:
            return text_content(
                f"No keyword matches found for query: '{query}'.\n"
                "Try rephrasing with specific terms (e.g. class names, error codes)."
            )

        return text_content(self._format_results(query, ranked))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_index(self) -> str | None:
        """Load corpus from ChromaDB and build (or refresh) the BM25 index.

        Reads ``BAMBOO_CHROMA_PATH`` and ``BAMBOO_CHROMA_COLLECTION`` at call
        time so environment changes take effect without restarting the process.

        Returns:
            ``None`` on success, or a human-readable error string on failure.
        """
        # --- import guards ---------------------------------------------------
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

        try:
            from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]  # optional dep
        except ImportError:
            return (
                "rank_bm25 is not installed.  "
                "Install it with: pip install -r requirements-rag.txt"
            )

        chroma_path: str = os.getenv("BAMBOO_CHROMA_PATH", _DEFAULT_CHROMA_PATH)
        collection_name: str = os.getenv(
            "BAMBOO_CHROMA_COLLECTION", _DEFAULT_CHROMA_COLLECTION
        )

        if not os.path.exists(chroma_path):
            return (
                f"ChromaDB path not found: '{chroma_path}'.  "
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
            # Fetch all documents in batches to avoid memory spikes.
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
                all_metas.extend(cast(list[dict[str, Any]], batch["metadatas"] or [{}] * len(batch["ids"])))
                offset += batch_size

            self._docs = all_docs
            self._ids = all_ids
            self._metadatas = all_metas
            tokenized = [_tokenize(d) for d in all_docs]
            self._bm25 = BM25Okapi(tokenized)
            self._cached_count = current_count
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._bm25 = None
            self._cached_count = -1
            return f"Failed to build BM25 index: {exc}"

        return None

    def _format_results(
        self,
        query: str,
        ranked: list[tuple[int, float]],
    ) -> str:
        """Format BM25 results into human-readable text.

        Args:
            query: The original search query.
            ranked: List of (doc_index, bm25_score) tuples, sorted descending.

        Returns:
            str: Formatted multi-line string for the MCP text payload.
        """
        lines: list[str] = [
            f"PanDA Doc BM25 Search — top {len(ranked)} result(s) for: '{query}'\n"
        ]
        for rank, (idx, score) in enumerate(ranked, start=1):
            doc = self._docs[idx]
            meta = self._metadatas[idx] if idx < len(self._metadatas) else {}

            snippet = doc[:_SNIPPET_MAX_CHARS]
            if len(doc) > _SNIPPET_MAX_CHARS:
                snippet += " …"

            source: str = ""
            if meta:
                source_val = meta.get("source_file") or meta.get("source") or meta.get("file") or ""
                if source_val:
                    source = f"  source : {source_val}\n"

            lines.append(
                f"[{rank}] bm25_score={score:.3f}\n"
                f"{source}"
                f"  {snippet}\n"
            )

        return "\n".join(lines)

    def _reset(self) -> None:
        """Clear the cached index and corpus.

        Intended for use in tests to force re-initialisation.

        Returns:
            None
        """
        self._docs = []
        self._ids = []
        self._metadatas = []
        self._bm25 = None
        self._cached_count = -1


panda_doc_bm25_tool = PandaDocBM25Tool()
