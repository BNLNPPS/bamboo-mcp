"""Tests for the CGSim Bamboo plugin.

Covers :mod:`cgsim.doc_rag`, :mod:`cgsim.doc_bm25`, and
:mod:`cgsim.ui_manifest`.

All ChromaDB and rank_bm25 interactions are monkeypatched so no real
dependencies are needed at test time.
"""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fake-module helpers
# ---------------------------------------------------------------------------

def _make_chroma_module(
    docs: list[str],
    ids: list[str],
    metadatas: list[dict] | None = None,
) -> types.ModuleType:
    """Build a minimal fake chromadb module returning the given docs.

    Args:
        docs: Document text strings to return from collection.get.
        ids: Corresponding document IDs.
        metadatas: Optional list of metadata dicts; defaults to empty dicts.

    Returns:
        A fake ``chromadb`` module with a ``PersistentClient`` constructor.
    """
    metas = metadatas or [{"source_file": f"doc_{i}.txt"} for i in range(len(docs))]
    mod = types.ModuleType("chromadb")
    collection = MagicMock()
    collection.count.return_value = len(docs)
    collection.get.return_value = {
        "documents": docs,
        "ids": ids,
        "metadatas": metas,
    }
    client = MagicMock()
    client.get_collection.return_value = collection
    mod.PersistentClient = MagicMock(return_value=client)  # type: ignore[attr-defined]
    return mod


class _FakeScores:
    """Minimal stand-in for a numpy array with a ``.tolist()`` method."""

    def __init__(self, scores: list[float]) -> None:
        """Initialise with a list of float scores.

        Args:
            scores: BM25 relevance scores.
        """
        self._scores = scores

    def tolist(self) -> list[float]:
        """Return scores as a plain Python list.

        Returns:
            List of float scores.
        """
        return self._scores

    def __iter__(self):
        """Support iteration over scores."""
        return iter(self._scores)


def _make_bm25_module(scores: list[float]) -> types.ModuleType:
    """Build a minimal fake rank_bm25 module returning the given scores.

    Args:
        scores: BM25 relevance scores to return from ``get_scores``.

    Returns:
        A fake ``rank_bm25`` module with a ``BM25Okapi`` constructor.
    """
    mod = types.ModuleType("rank_bm25")
    bm25 = MagicMock()
    bm25.get_scores.return_value = _FakeScores(scores)
    mod.BM25Okapi = MagicMock(return_value=bm25)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# doc_rag (vector search)
# ---------------------------------------------------------------------------

class TestCgsimDocSearchTool:
    """Tests for :class:`cgsim.doc_rag.CgsimDocSearchTool`."""

    def test_get_definition_shape(self) -> None:
        """get_definition returns a valid MCP tool definition."""
        from cgsim.doc_rag import CgsimDocSearchTool

        defn = CgsimDocSearchTool.get_definition()
        assert defn["name"] == "cgsim.doc_search"
        assert "query" in defn["inputSchema"]["properties"]
        assert "top_k" in defn["inputSchema"]["properties"]
        assert defn["inputSchema"]["required"] == ["query"]
        assert defn["inputSchema"]["additionalProperties"] is False

    def test_description_mentions_cgsim(self) -> None:
        """Tool description references CGSim and SimGrid."""
        from cgsim.doc_rag import CgsimDocSearchTool

        desc = CgsimDocSearchTool.get_definition()["description"]
        assert "CGSim" in desc
        assert "SimGrid" in desc

    def test_description_mentions_bm25_complement(self) -> None:
        """Tool description references the sibling BM25 tool by name."""
        from cgsim.doc_rag import CgsimDocSearchTool

        desc = CgsimDocSearchTool.get_definition()["description"]
        assert "cgsim.doc_bm25" in desc

    @pytest.mark.asyncio
    async def test_chromadb_not_installed_returns_error(self) -> None:
        """Returns a clear error message when chromadb is not installed."""
        from cgsim.doc_rag import CgsimDocSearchTool

        tool = CgsimDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": None}):  # type: ignore[dict-item]
            result = await tool.call({"query": "assignJob plugin method"})
        assert len(result) == 1
        assert "not installed" in result[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_missing_chroma_path_returns_error(
        self, tmp_path, monkeypatch
    ) -> None:
        """Returns a clear error when BAMBOO_CHROMA_PATH does not exist."""
        from cgsim.doc_rag import CgsimDocSearchTool

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", "/nonexistent/cgsim_path")
        chroma_mod = _make_chroma_module([], [])
        tool = CgsimDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            result = await tool.call({"query": "calibration"})
        assert "not found" in result[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_default_collection_is_cgsim_docs(
        self, tmp_path, monkeypatch
    ) -> None:
        """Collection name defaults to ``cgsim_docs`` when env var is unset."""
        from cgsim.doc_rag import CgsimDocSearchTool

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.delenv("BAMBOO_CHROMA_COLLECTION", raising=False)

        chroma_mod = _make_chroma_module([], [])
        tool = CgsimDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            await tool.call({"query": "plugin"})

        client_instance = chroma_mod.PersistentClient.return_value
        client_instance.get_collection.assert_called_once_with(name="cgsim_docs")

    @pytest.mark.asyncio
    async def test_env_override_collection_name(
        self, tmp_path, monkeypatch
    ) -> None:
        """BAMBOO_CHROMA_COLLECTION env var overrides the default."""
        from cgsim.doc_rag import CgsimDocSearchTool

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "my_custom_col")

        chroma_mod = _make_chroma_module([], [])
        tool = CgsimDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            await tool.call({"query": "anything"})

        client_instance = chroma_mod.PersistentClient.return_value
        client_instance.get_collection.assert_called_once_with(name="my_custom_col")

    def test_module_singleton_exists(self) -> None:
        """Module-level singleton ``cgsim_doc_search_tool`` is exported."""
        from cgsim.doc_rag import cgsim_doc_search_tool, CgsimDocSearchTool

        assert isinstance(cgsim_doc_search_tool, CgsimDocSearchTool)


# ---------------------------------------------------------------------------
# doc_bm25 (keyword search)
# ---------------------------------------------------------------------------

class TestCgsimDocBM25Tool:
    """Tests for :class:`cgsim.doc_bm25.CgsimDocBM25Tool`."""

    def test_get_definition_shape(self) -> None:
        """get_definition returns a valid MCP tool definition."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        defn = CgsimDocBM25Tool.get_definition()
        assert defn["name"] == "cgsim.doc_bm25"
        assert "query" in defn["inputSchema"]["properties"]
        assert "top_k" in defn["inputSchema"]["properties"]
        assert defn["inputSchema"]["required"] == ["query"]
        assert defn["inputSchema"]["additionalProperties"] is False

    def test_description_mentions_simgrid_api(self) -> None:
        """Tool description references SimGrid/CGSim API method names."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        desc = CgsimDocBM25Tool.get_definition()["description"]
        assert "assignJob" in desc
        assert "SimGrid" in desc

    def test_description_mentions_rag_complement(self) -> None:
        """Tool description references the sibling RAG tool by name."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        desc = CgsimDocBM25Tool.get_definition()["description"]
        assert "cgsim.doc_search" in desc

    @pytest.mark.asyncio
    async def test_basic_query_returns_results(
        self, tmp_path, monkeypatch
    ) -> None:
        """A standard query against a populated collection returns ranked hits."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        docs = [
            "The assignJob method must be implemented by derived plugin classes.",
            "CGSim uses SimGrid discrete-event simulation for job scheduling.",
        ]
        ids = ["doc:001", "doc:002"]
        scores = [4.8, 0.3]

        chroma_mod = _make_chroma_module(docs, ids)
        bm25_mod = _make_bm25_module(scores)

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "cgsim_test")

        tool = CgsimDocBM25Tool()
        with patch.dict(
            sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}
        ):
            result = await tool.call({"query": "assignJob plugin", "top_k": 5})

        assert len(result) == 1
        text: str = result[0]["text"]
        assert result[0]["type"] == "text"
        assert "assignJob" in text
        assert "bm25_score" in text
        assert "[1]" in text

    @pytest.mark.asyncio
    async def test_no_matches_returns_friendly_message(
        self, tmp_path, monkeypatch
    ) -> None:
        """When all BM25 scores are zero a friendly message is returned."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        chroma_mod = _make_chroma_module(["unrelated cooking content"], ["doc:001"])
        bm25_mod = _make_bm25_module([0.0])

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

        tool = CgsimDocBM25Tool()
        with patch.dict(
            sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}
        ):
            result = await tool.call({"query": "assignJob"})

        assert "no keyword matches" in result[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_chromadb_not_installed_returns_error(self) -> None:
        """Returns a clear error message when chromadb is not installed."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        tool = CgsimDocBM25Tool()
        with patch.dict(sys.modules, {"chromadb": None}):  # type: ignore[dict-item]
            result = await tool.call({"query": "anything"})
        assert "not installed" in result[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_rank_bm25_not_installed_returns_error(
        self, tmp_path, monkeypatch
    ) -> None:
        """Returns a clear error message when rank_bm25 is not installed."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        chroma_mod = _make_chroma_module(["doc"], ["id:1"])
        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

        tool = CgsimDocBM25Tool()
        with patch.dict(
            sys.modules,
            {"chromadb": chroma_mod, "rank_bm25": None},  # type: ignore[dict-item]
        ):
            result = await tool.call({"query": "anything"})
        assert "not installed" in result[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_missing_chroma_path_returns_error(self, monkeypatch) -> None:
        """Returns a clear error when BAMBOO_CHROMA_PATH does not exist."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", "/nonexistent/path")
        chroma_mod = _make_chroma_module([], [])
        bm25_mod = _make_bm25_module([])

        tool = CgsimDocBM25Tool()
        with patch.dict(
            sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}
        ):
            result = await tool.call({"query": "anything"})
        assert "not found" in result[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_default_collection_is_cgsim_docs(
        self, tmp_path, monkeypatch
    ) -> None:
        """Collection name defaults to ``cgsim_docs`` when env var is unset."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.delenv("BAMBOO_CHROMA_COLLECTION", raising=False)

        chroma_mod = _make_chroma_module([], [])
        bm25_mod = _make_bm25_module([])

        tool = CgsimDocBM25Tool()
        with patch.dict(
            sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}
        ):
            await tool.call({"query": "anything"})

        client_instance = chroma_mod.PersistentClient.return_value
        client_instance.get_collection.assert_called_once_with(name="cgsim_docs")

    @pytest.mark.asyncio
    async def test_env_override_collection_name(
        self, tmp_path, monkeypatch
    ) -> None:
        """BAMBOO_CHROMA_COLLECTION env var overrides the default."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "override_col")

        chroma_mod = _make_chroma_module([], [])
        bm25_mod = _make_bm25_module([])

        tool = CgsimDocBM25Tool()
        with patch.dict(
            sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}
        ):
            await tool.call({"query": "anything"})

        client_instance = chroma_mod.PersistentClient.return_value
        client_instance.get_collection.assert_called_once_with(name="override_col")

    @pytest.mark.asyncio
    async def test_cache_is_reused(self, tmp_path, monkeypatch) -> None:
        """The BM25 index is not rebuilt when the document count is unchanged."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        docs = ["CGSim plugin architecture", "SimGrid netzone configuration"]
        ids = ["id:1", "id:2"]
        scores = [1.0, 0.5]

        chroma_mod = _make_chroma_module(docs, ids)
        bm25_mod = _make_bm25_module(scores)
        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

        tool = CgsimDocBM25Tool()
        with patch.dict(
            sys.modules, {"chromadb": chroma_mod, "rank_bm25": bm25_mod}
        ):
            await tool.call({"query": "plugin"})
            first_bm25 = tool._bm25
            await tool.call({"query": "netzone"})
            second_bm25 = tool._bm25

        # Same object — cache was reused.
        assert first_bm25 is second_bm25

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self) -> None:
        """Returns an error message for empty query strings."""
        from cgsim.doc_bm25 import CgsimDocBM25Tool

        tool = CgsimDocBM25Tool()
        result = await tool.call({"query": ""})
        assert "required" in result[0]["text"].lower()

    def test_module_singleton_exists(self) -> None:
        """Module-level singleton ``cgsim_doc_bm25_tool`` is exported."""
        from cgsim.doc_bm25 import cgsim_doc_bm25_tool, CgsimDocBM25Tool

        assert isinstance(cgsim_doc_bm25_tool, CgsimDocBM25Tool)


# ---------------------------------------------------------------------------
# ui_manifest
# ---------------------------------------------------------------------------

class TestCgsimUiManifestTool:
    """Tests for :class:`cgsim.ui_manifest.CgsimUiManifestTool`."""

    def test_get_definition_shape(self) -> None:
        """get_definition returns the expected MCP tool definition."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        defn = CgsimUiManifestTool.get_definition()
        assert defn["name"] == "cgsim.ui_manifest"
        assert defn["inputSchema"]["additionalProperties"] is False
        assert defn["inputSchema"]["properties"] == {}

    @pytest.mark.asyncio
    async def test_call_returns_valid_json(self) -> None:
        """call() returns a single text content block containing valid JSON."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        tool = CgsimUiManifestTool()
        result = await tool.call({})
        assert len(result) == 1
        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert isinstance(payload, dict)

    @pytest.mark.asyncio
    async def test_manifest_plugin_id(self) -> None:
        """Manifest ``plugin_id`` is ``cgsim``."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        tool = CgsimUiManifestTool()
        result = await tool.call({})
        payload = json.loads(result[0]["text"])
        assert payload["plugin_id"] == "cgsim"

    @pytest.mark.asyncio
    async def test_manifest_display_name(self) -> None:
        """Manifest ``display_name`` contains 'AskCGSim'."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        tool = CgsimUiManifestTool()
        result = await tool.call({})
        payload = json.loads(result[0]["text"])
        assert "AskCGSim" in payload["display_name"]

    @pytest.mark.asyncio
    async def test_manifest_accent_is_green(self) -> None:
        """Manifest ``accent`` is ``green`` (distinct from ATLAS cyan)."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        tool = CgsimUiManifestTool()
        result = await tool.call({})
        payload = json.loads(result[0]["text"])
        assert payload["accent"] == "green"

    @pytest.mark.asyncio
    async def test_manifest_banner_is_list_of_strings(self) -> None:
        """Manifest ``banner`` is a non-empty list of strings."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        tool = CgsimUiManifestTool()
        result = await tool.call({})
        payload = json.loads(result[0]["text"])
        assert isinstance(payload["banner"], list)
        assert len(payload["banner"]) > 0
        assert all(isinstance(line, str) for line in payload["banner"])

    @pytest.mark.asyncio
    async def test_manifest_banner_contains_cgsim(self) -> None:
        """Banner text contains block-letter CGSim characters."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        tool = CgsimUiManifestTool()
        result = await tool.call({})
        payload = json.loads(result[0]["text"])
        full_banner = "\n".join(payload["banner"])
        # Block font uses box-drawing characters; check at least one is present.
        assert "█" in full_banner or "╗" in full_banner

    @pytest.mark.asyncio
    async def test_manifest_help_text_present(self) -> None:
        """Manifest ``help`` key is a non-empty string."""
        from cgsim.ui_manifest import CgsimUiManifestTool

        tool = CgsimUiManifestTool()
        result = await tool.call({})
        payload = json.loads(result[0]["text"])
        assert isinstance(payload["help"], str)
        assert len(payload["help"]) > 0

    def test_module_singleton_exists(self) -> None:
        """Module-level singleton ``cgsim_ui_manifest_tool`` is exported."""
        from cgsim.ui_manifest import cgsim_ui_manifest_tool, CgsimUiManifestTool

        assert isinstance(cgsim_ui_manifest_tool, CgsimUiManifestTool)
