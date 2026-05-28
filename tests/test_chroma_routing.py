"""Tests for the ChromaDB blue/green collection name resolver.

Covers :func:`bamboo.tools._chroma_routing.resolve_collection` and the live
re-resolution behaviour of :class:`bamboo.tools.doc_rag.PandaDocSearchTool`.
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bamboo.tools._chroma_routing import resolve_collection, ROUTING_SIDECAR
from bamboo.tools.doc_rag import PandaDocSearchTool


# ===========================================================================
# resolve_collection
# ===========================================================================


class TestResolveCollection:
    """Unit tests for the resolve_collection free function."""

    def test_returns_physical_name_from_sidecar(self, tmp_path):
        """When the sidecar has an entry, the physical slot name is returned."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text(
            json.dumps({"atlas_docs": "atlas_docs__b"}), encoding="utf-8"
        )
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs__b"

    def test_returns_logical_name_when_no_sidecar(self, tmp_path):
        """When no sidecar exists the logical name is returned unchanged (fallback)."""
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs"

    def test_returns_logical_name_when_entry_missing(self, tmp_path):
        """When the sidecar has no entry for the requested name, falls back."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text(
            json.dumps({"epic_docs": "epic_docs__a"}), encoding="utf-8"
        )
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs"

    def test_returns_logical_name_on_corrupt_sidecar(self, tmp_path):
        """A corrupt sidecar does not raise — falls back to the logical name."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text("{ not valid json !!", encoding="utf-8")
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs"

    def test_multiple_logical_names_resolved_independently(self, tmp_path):
        """Each logical name is resolved independently from the same sidecar."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text(
            json.dumps({
                "atlas_docs": "atlas_docs__a",
                "epic_docs": "epic_docs__b",
                "cgsim_docs": "cgsim_docs__a",
            }),
            encoding="utf-8",
        )
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs__a"
        assert resolve_collection(str(tmp_path), "epic_docs") == "epic_docs__b"
        assert resolve_collection(str(tmp_path), "cgsim_docs") == "cgsim_docs__a"

    def test_sidecar_updated_between_calls(self, tmp_path):
        """Re-reading on every call picks up a slot swap written between calls."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text(
            json.dumps({"atlas_docs": "atlas_docs__a"}), encoding="utf-8"
        )
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs__a"

        # Simulate document-monitor agent completing a blue/green swap.
        sidecar.write_text(
            json.dumps({"atlas_docs": "atlas_docs__b"}), encoding="utf-8"
        )
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs__b"

    def test_returns_logical_name_when_physical_is_empty_string(self, tmp_path):
        """A blank physical name in the sidecar is treated as missing (fallback)."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text(
            json.dumps({"atlas_docs": ""}), encoding="utf-8"
        )
        assert resolve_collection(str(tmp_path), "atlas_docs") == "atlas_docs"


# ===========================================================================
# PandaDocSearchTool — live re-resolution
# ===========================================================================


def _make_chroma_module(collection: Any, name: str = "test_col") -> types.ModuleType:
    """Build a minimal fake chromadb module for a given collection mock."""
    mod = types.ModuleType("chromadb")
    collection.name = name
    client = MagicMock()
    client.get_collection.return_value = collection
    mod.PersistentClient = MagicMock(return_value=client)  # type: ignore[attr-defined]
    return mod


class TestPandaDocSearchToolReResolution:
    """Tests for live slot re-resolution in PandaDocSearchTool._ensure_collection."""

    @pytest.mark.asyncio
    async def test_resolves_physical_name_via_sidecar(self, tmp_path, monkeypatch):
        """_ensure_collection opens the physical slot name read from the sidecar."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text(
            json.dumps({"bamboo_docs": "bamboo_docs__a"}), encoding="utf-8"
        )

        fake_collection = MagicMock()
        fake_collection.name = "bamboo_docs__a"
        fake_collection.query.return_value = {
            "documents": [["some text"]],
            "metadatas": [[{}]],
            "distances": [[0.1]],
        }
        chroma_mod = _make_chroma_module(fake_collection, "bamboo_docs__a")

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "bamboo_docs")

        tool = PandaDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            await tool.call({"query": "PanDA workflow"})

        # The client should have been asked for the physical slot, not the
        # logical name.
        chroma_mod.PersistentClient.return_value.get_collection.assert_called_once_with(
            name="bamboo_docs__a"
        )
        assert tool._resolved_physical == "bamboo_docs__a"

    @pytest.mark.asyncio
    async def test_invalidates_cache_when_slot_changes(self, tmp_path, monkeypatch):
        """When the sidecar swaps the active slot, the cached handle is replaced."""
        sidecar = tmp_path / ROUTING_SIDECAR

        # First slot: __a
        sidecar.write_text(
            json.dumps({"bamboo_docs": "bamboo_docs__a"}), encoding="utf-8"
        )

        fake_col_a = MagicMock()
        fake_col_a.name = "bamboo_docs__a"
        fake_col_a.query.return_value = {
            "documents": [["text from __a"]],
            "metadatas": [[{}]],
            "distances": [[0.1]],
        }

        fake_col_b = MagicMock()
        fake_col_b.name = "bamboo_docs__b"
        fake_col_b.query.return_value = {
            "documents": [["text from __b"]],
            "metadatas": [[{}]],
            "distances": [[0.1]],
        }

        # Client returns __a first, then __b after the sidecar updates.
        mock_client = MagicMock()
        mock_client.get_collection.side_effect = [fake_col_a, fake_col_b]

        chroma_mod = types.ModuleType("chromadb")
        chroma_mod.PersistentClient = MagicMock(return_value=mock_client)  # type: ignore[attr-defined]

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "bamboo_docs")

        tool = PandaDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            # First call — should open __a.
            await tool.call({"query": "first query"})
            assert tool._resolved_physical == "bamboo_docs__a"

            # Simulate a blue/green swap: document-monitor updates the sidecar.
            sidecar.write_text(
                json.dumps({"bamboo_docs": "bamboo_docs__b"}), encoding="utf-8"
            )

            # Second call — should detect the change and reopen on __b.
            await tool.call({"query": "second query"})
            assert tool._resolved_physical == "bamboo_docs__b"

        # get_collection should have been called twice: once for __a, once for __b.
        assert mock_client.get_collection.call_count == 2
        calls = [c.kwargs["name"] for c in mock_client.get_collection.call_args_list]
        assert calls == ["bamboo_docs__a", "bamboo_docs__b"]

    @pytest.mark.asyncio
    async def test_does_not_reopen_when_slot_unchanged(self, tmp_path, monkeypatch):
        """Repeated calls with no sidecar change do not reopen the collection."""
        sidecar = tmp_path / ROUTING_SIDECAR
        sidecar.write_text(
            json.dumps({"bamboo_docs": "bamboo_docs__a"}), encoding="utf-8"
        )

        fake_collection = MagicMock()
        fake_collection.name = "bamboo_docs__a"
        fake_collection.query.return_value = {
            "documents": [["text"]],
            "metadatas": [[{}]],
            "distances": [[0.1]],
        }
        mock_client = MagicMock()
        mock_client.get_collection.return_value = fake_collection

        chroma_mod = types.ModuleType("chromadb")
        chroma_mod.PersistentClient = MagicMock(return_value=mock_client)  # type: ignore[attr-defined]

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "bamboo_docs")

        tool = PandaDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            for _ in range(5):
                await tool.call({"query": "repeated query"})

        # Despite 5 calls, the collection should only have been opened once.
        assert mock_client.get_collection.call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_when_no_sidecar(self, tmp_path, monkeypatch):
        """Without a sidecar the logical name is used directly (pre-blue/green fallback)."""
        # No sidecar file written — tmp_path is empty.
        fake_collection = MagicMock()
        fake_collection.name = "bamboo_docs"
        fake_collection.query.return_value = {
            "documents": [["text"]],
            "metadatas": [[{}]],
            "distances": [[0.2]],
        }
        chroma_mod = _make_chroma_module(fake_collection, "bamboo_docs")

        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))
        monkeypatch.setenv("BAMBOO_CHROMA_COLLECTION", "bamboo_docs")

        tool = PandaDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            result = await tool.call({"query": "anything"})

        assert result[0]["type"] == "text"
        chroma_mod.PersistentClient.return_value.get_collection.assert_called_once_with(
            name="bamboo_docs"
        )
        assert tool._resolved_physical == "bamboo_docs"

    def test_reset_clears_resolved_physical(self, tmp_path, monkeypatch):
        """_reset() clears _resolved_physical alongside _client and _collection."""
        fake_collection = MagicMock()
        chroma_mod = _make_chroma_module(fake_collection)
        monkeypatch.setenv("BAMBOO_CHROMA_PATH", str(tmp_path))

        tool = PandaDocSearchTool()
        with patch.dict(sys.modules, {"chromadb": chroma_mod}):
            tool._ensure_collection()

        tool._reset()
        assert tool._client is None
        assert tool._collection is None
        assert tool._resolved_physical is None
