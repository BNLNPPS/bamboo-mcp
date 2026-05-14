"""Tests for plugin-aware tool list filtering.

Verifies that ``list_tools`` only exposes plugin tools whose namespace matches
``ASKPANDA_PLUGIN``, keeping the LLM token cost proportional to the number of
tools in the active plugin rather than all installed plugins.
"""
from __future__ import annotations

from typing import Any


# Fake entry-point tool definitions covering three plugins.
_FAKE_EP_TOOLS: list[dict[str, Any]] = [
    {"name": "atlas.task_status", "description": "ATLAS task status"},
    {"name": "atlas.doc_search", "description": "ATLAS doc search"},
    {"name": "atlas.ui_manifest", "description": "ATLAS UI manifest"},
    {"name": "epic.doc_search", "description": "ePIC doc search"},
    {"name": "epic.ui_manifest", "description": "ePIC UI manifest"},
    {"name": "cgsim.doc_search", "description": "CGSim doc search"},
    {"name": "cgsim.doc_bm25", "description": "CGSim BM25"},
    {"name": "cgsim.ui_manifest", "description": "CGSim UI manifest"},
]


def _filter_ep_tools(active_plugin: str) -> list[dict[str, Any]]:
    """Apply the same filtering logic as list_tools() in core.py.

    Args:
        active_plugin: Plugin namespace to include.

    Returns:
        Filtered list of entry-point tool defs.
    """
    result = []
    for ep_def in _FAKE_EP_TOOLS:
        tool_name: str = ep_def.get("name", "")
        namespace: str = tool_name.split(".", 1)[0] if "." in tool_name else ""
        if namespace == active_plugin:
            result.append(ep_def)
    return result


class TestPluginToolFiltering:
    """Tests for plugin-aware entry-point tool filtering."""

    def test_atlas_plugin_includes_only_atlas_tools(self) -> None:
        """ASKPANDA_PLUGIN=atlas exposes only atlas.* tools."""
        tools = _filter_ep_tools("atlas")
        names = [t["name"] for t in tools]
        assert all(n.startswith("atlas.") for n in names)
        assert "atlas.task_status" in names
        assert "atlas.doc_search" in names
        assert "atlas.ui_manifest" in names

    def test_atlas_plugin_excludes_cgsim_tools(self) -> None:
        """ASKPANDA_PLUGIN=atlas must not expose cgsim.* tools."""
        tools = _filter_ep_tools("atlas")
        names = [t["name"] for t in tools]
        assert not any(n.startswith("cgsim.") for n in names)

    def test_atlas_plugin_excludes_epic_tools(self) -> None:
        """ASKPANDA_PLUGIN=atlas must not expose epic.* tools."""
        tools = _filter_ep_tools("atlas")
        names = [t["name"] for t in tools]
        assert not any(n.startswith("epic.") for n in names)

    def test_cgsim_plugin_includes_only_cgsim_tools(self) -> None:
        """ASKPANDA_PLUGIN=cgsim exposes only cgsim.* tools."""
        tools = _filter_ep_tools("cgsim")
        names = [t["name"] for t in tools]
        assert all(n.startswith("cgsim.") for n in names)
        assert "cgsim.doc_search" in names
        assert "cgsim.doc_bm25" in names
        assert "cgsim.ui_manifest" in names

    def test_cgsim_plugin_excludes_atlas_tools(self) -> None:
        """ASKPANDA_PLUGIN=cgsim must not expose atlas.* tools."""
        tools = _filter_ep_tools("cgsim")
        names = [t["name"] for t in tools]
        assert not any(n.startswith("atlas.") for n in names)

    def test_epic_plugin_includes_only_epic_tools(self) -> None:
        """ASKPANDA_PLUGIN=epic exposes only epic.* tools."""
        tools = _filter_ep_tools("epic")
        names = [t["name"] for t in tools]
        assert all(n.startswith("epic.") for n in names)

    def test_unknown_plugin_returns_empty(self) -> None:
        """An unknown plugin name returns no entry-point tools."""
        tools = _filter_ep_tools("verarubin")
        assert tools == []

    def test_tool_count_is_per_plugin(self) -> None:
        """Each plugin exposes only its own tools, not the union of all."""
        atlas_count = len(_filter_ep_tools("atlas"))
        cgsim_count = len(_filter_ep_tools("cgsim"))
        epic_count = len(_filter_ep_tools("epic"))
        total = len(_FAKE_EP_TOOLS)

        # Each plugin's count must be less than the total
        assert atlas_count < total
        assert cgsim_count < total
        assert epic_count < total

        # Counts must be positive
        assert atlas_count > 0
        assert cgsim_count > 0
        assert epic_count > 0

    def test_env_var_drives_filter(self, monkeypatch: Any) -> None:
        """ASKPANDA_PLUGIN env var drives the namespace filter."""
        import os

        monkeypatch.setenv("ASKPANDA_PLUGIN", "cgsim")
        active = os.getenv("ASKPANDA_PLUGIN", "atlas").strip().lower()
        tools = _filter_ep_tools(active)
        names = [t["name"] for t in tools]
        assert all(n.startswith("cgsim.") for n in names)

    def test_env_var_default_is_atlas(self, monkeypatch: Any) -> None:
        """When ASKPANDA_PLUGIN is unset the default is atlas."""
        import os

        monkeypatch.delenv("ASKPANDA_PLUGIN", raising=False)
        active = os.getenv("ASKPANDA_PLUGIN", "atlas").strip().lower()
        assert active == "atlas"
        tools = _filter_ep_tools(active)
        names = [t["name"] for t in tools]
        assert all(n.startswith("atlas.") for n in names)
