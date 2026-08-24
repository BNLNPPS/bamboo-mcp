"""Tests for PandaDocBM25Tool.

All ChromaDB and rank_bm25 interactions are monkeypatched so no real
dependencies are needed at test time.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from bamboo.tools.doc_bm25 import PandaDocBM25Tool, _tokenize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chroma_module(docs: list[str], ids: list[str]) -> types.ModuleType:
    """Build a minimal fake chromadb module returning the given docs."""
    mod = types.ModuleType("chromadb")
    collection = MagicMock()
    collection.count.return_value = len(docs)
    collection.get.return_value = {
        "documents": docs,
        "ids": ids,
        "metadatas": [{"source_file": f"doc_{i}.txt"} for i in range(len(docs))],
    }
    client = MagicMock()
    client.get_collection.return_value = collection
    mod.PersistentClient = MagicMock(return_value=client)  # type: ignore[attr-defined]
    return mod


class _FakeScores:
    """Minimal stand-in for a numpy array with a .tolist() method."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def tolist(self) -> list[float]:
        """Return scores as a plain Python list."""
        return self._scores

    def __iter__(self):
        """Support iteration over scores."""
        return iter(self._scores)


def _make_bm25_module(scores: list[float]) -> types.ModuleType:
    """Build a minimal fake rank_bm25 module returning the given scores."""
    mod = types.ModuleType("rank_bm25")
    bm25 = MagicMock()
    bm25.get_scores.return_value = _FakeScores(scores)
    mod.BM25Okapi = MagicMock(return_value=bm25)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Unit tests — _tokenize
# ---------------------------------------------------------------------------

def test_tokenize_basic():
    """Tokenizer lowercases and splits on non-alphanumeric characters."""
    tokens = _tokenize("Hello World! BADALLOC=1223")
    assert tokens == ["hello", "world", "badalloc", "1223"]


def test_tokenize_empty():
    """Tokenizer returns empty list for empty input."""
    assert _tokenize("") == []


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_returns_ranked_results(tmp_path, monkeypatch):
    """A successful BM25 query returns ranked results with scores and snippets."""
    docs = [
        "BADALLOC = 1223 Transform failed due to bad alloc",
        "PanDA is the workload management system for ATLAS",
    ]
    ids = ["doc:001", "doc:002"]
    scores = [5.2, 0.1]

    chroma_mod = _make_chroma_module(docs, ids)
    bm25_mod = _make_bm25_module(scores)

    monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
    monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "test_col")

    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}):
        result = await tool.call({"query": "BADALLOC error code", "top_k": 5})

    assert len(result) == 1
    text: str = result[0]["text"]
    assert result[0]["type"] == "text"
    assert "BADALLOC" in text
    assert "bm25_score" in text
    assert "[1]" in text


@pytest.mark.asyncio
async def test_no_matches_returns_friendly_message(tmp_path, monkeypatch):
    """When all BM25 scores are zero, a friendly message is returned."""
    docs = ["Some unrelated document about cooking"]
    ids = ["doc:001"]
    scores = [0.0]

    chroma_mod = _make_chroma_module(docs, ids)
    bm25_mod = _make_bm25_module(scores)

    monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}):
        result = await tool.call({"query": "pilot error codes"})

    assert "no keyword matches" in result[0]["text"].lower()


@pytest.mark.asyncio
async def test_chromadb_not_installed_returns_error():
    """Returns a clear error when chromadb is not installed."""
    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": None}):  # type: ignore[dict-item]
        result = await tool.call({"query": "anything"})
    assert "not installed" in result[0]["text"].lower()


@pytest.mark.asyncio
async def test_rank_bm25_not_installed_returns_error(tmp_path, monkeypatch):
    """Returns a clear error when rank_bm25 is not installed."""
    chroma_mod = _make_chroma_module(["doc"], ["id:1"])
    monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": None}):  # type: ignore[dict-item]
        result = await tool.call({"query": "anything"})
    assert "not installed" in result[0]["text"].lower()


@pytest.mark.asyncio
async def test_missing_path_returns_error(monkeypatch):
    """Returns a clear error when BAMBOO_CHROMA_PATH does not exist."""
    monkeypatch.setenv("BAMBOO_CHROMA_PATH", "/nonexistent/path")
    chroma_mod = _make_chroma_module([], [])
    bm25_mod = _make_bm25_module([])
    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}):
        result = await tool.call({"query": "anything"})
    assert "not found" in result[0]["text"].lower()


@pytest.mark.asyncio
async def test_empty_query_returns_error():
    """Returns an error message for empty query strings."""
    tool = PandaDocBM25Tool()
    result = await tool.call({"query": ""})
    assert "required" in result[0]["text"].lower()


@pytest.mark.asyncio
async def test_cache_is_reused(tmp_path, monkeypatch):
    """The BM25 index is only rebuilt when the document count changes."""
    docs = ["doc one about panda", "doc two about atlas"]
    ids = ["id:1", "id:2"]
    scores = [1.0, 0.5]

    chroma_mod = _make_chroma_module(docs, ids)
    bm25_mod = _make_bm25_module(scores)
    monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}):
        await tool.call({"query": "panda"})
        first_bm25 = tool._bm25
        await tool.call({"query": "atlas"})
        second_bm25 = tool._bm25

    # Same object — cache was reused.
    assert first_bm25 is second_bm25


def test_get_definition_shape():
    """get_definition returns the expected MCP structure."""
    defn = PandaDocBM25Tool.get_definition()
    assert defn["name"] == "panda_doc_bm25"
    assert "query" in defn["inputSchema"]["properties"]
    assert "top_k" in defn["inputSchema"]["properties"]
    assert defn["inputSchema"]["required"] == ["query"]


def test_reset_clears_state(tmp_path, monkeypatch):
    """_reset() clears all cached state."""
    tool = PandaDocBM25Tool()
    tool._docs = ["something"]
    tool._cached_count = 1
    tool._reset()
    assert tool._docs == []
    assert tool._bm25 is None
    assert tool._cached_count == -1


# ---------------------------------------------------------------------------
# topic parameter tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_topic_argument_opens_correct_collection(tmp_path, monkeypatch):
    """The 'topic' argument controls which collection the BM25 index is built from."""
    monkeypatch.setenv(
        "BAMBOO_CHROMA_COLLECTION_MAP",
        '{"rucio": "rucio_docs", "panda": "panda_docs"}',
    )
    monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

    docs = ["Rucio rule lifecycle explanation."]
    ids = ["doc:rucio:001"]
    scores = [3.5]

    chroma_mod = _make_chroma_module(docs, ids)
    bm25_mod = _make_bm25_module(scores)

    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}):
        await tool.call({"query": "rucio rule", "topic": "rucio"})

    chroma_mod.PersistentClient.return_value.get_collection.assert_called_once_with(
        name="rucio_docs"
    )


@pytest.mark.asyncio
async def test_default_topic_used_when_topic_absent(tmp_path, monkeypatch):
    """When 'topic' is absent, _default_topic ('panda') is used."""
    monkeypatch.delenv("BAMBOO_CHROMA_COLLECTION_MAP", raising=False)
    monkeypatch.delenv("BAMBOO_CHROMA_COLLECTION", raising=False)
    monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

    chroma_mod = _make_chroma_module(["panda doc"], ["id:1"])
    bm25_mod = _make_bm25_module([2.0])

    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}):
        await tool.call({"query": "BADALLOC"})

    chroma_mod.PersistentClient.return_value.get_collection.assert_called_once_with(
        name="panda_docs"
    )


@pytest.mark.asyncio
async def test_topic_change_invalidates_bm25_cache(tmp_path, monkeypatch):
    """Switching topic mid-session forces a BM25 index rebuild."""
    monkeypatch.setenv(
        "BAMBOO_CHROMA_COLLECTION_MAP",
        '{"panda": "panda_docs", "rucio": "rucio_docs"}',
    )
    monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

    chroma_mod = _make_chroma_module(["a doc"], ["id:1"])
    bm25_mod = _make_bm25_module([1.0])

    tool = PandaDocBM25Tool()
    with patch.dict(sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}):
        await tool.call({"query": "first", "topic": "panda"})
        # Switch topic — should clear cache and reopen collection.
        await tool.call({"query": "second", "topic": "rucio"})

    # Two separate get_collection calls because topic changed.
    assert chroma_mod.PersistentClient.return_value.get_collection.call_count == 2


def test_get_definition_includes_topic_property():
    """get_definition exposes 'topic' in the inputSchema properties."""
    defn = PandaDocBM25Tool.get_definition()
    props = defn["inputSchema"]["properties"]
    assert "topic" in props
    assert props["topic"]["default"] == "panda"
