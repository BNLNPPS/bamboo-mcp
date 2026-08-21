"""Tests for the tool-name canon shared by the server, planner and executor.

Bamboo names the same tool in three places — the MCP wire, the planner catalog
and the exact-string comparisons in ``bamboo_executor`` — and a disagreement
between them does not raise.  It degrades: the tool still runs, but its
evidence lands under a key nothing reads, so the specialist synthesis path is
skipped and the answer falls through to generic prose.  These tests pin the
agreement rather than the mechanism.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bamboo.tools.bamboo_executor as ex_mod
from bamboo.tools._tool_names import (
    alias_map,
    canonical_tool_name,
    reset_alias_cache,
    wire_tool_definitions,
)
from bamboo.tools.bamboo_executor import _CORE_DUMP_TOOL, _execute_one_tool


@pytest.fixture(autouse=True)
def _clear_evidence_store() -> Any:
    """Reset the module-global evidence store around every test.

    Yields:
        None.
    """
    ex_mod._last_evidence_store.clear()
    yield
    ex_mod._last_evidence_store.clear()


def _wire_names() -> set[str]:
    """Return every tool name the MCP server exposes to clients.

    Returns:
        Set of built-in ``TOOLS`` advertised names plus plugin wire names.
    """
    from bamboo.core import TOOLS

    names: set[str] = {str(d["name"]) for d in wire_tool_definitions()}
    for tool in TOOLS.values():
        get_def = getattr(tool, "get_definition", None)
        if callable(get_def):
            defn = get_def()
            if isinstance(defn, dict) and defn.get("name"):
                names.add(str(defn["name"]))
    return names


class TestCanonicalNames:
    """The alias map collapses every accepted spelling onto the wire name."""

    def test_entry_point_key_is_canonical_when_not_built_in(self) -> None:
        """A plugin tool with no built-in namesake is canonical as its key."""
        assert canonical_tool_name("core_dump_analysis") == "atlas.core_dump_analysis"
        assert canonical_tool_name("atlas.core_dump_analysis") == "atlas.core_dump_analysis"

    def test_built_in_name_wins_over_the_entry_point_key(self) -> None:
        """``atlas.log_analysis`` is never on the wire; the built-in name is.

        ``bamboo.core`` drops an entry point whose tool advertises a name
        already in ``TOOLS``, so a plan naming the entry-point key must still
        reach the ``panda_log_analysis`` evidence that ``get_last_core_dump_offer``
        and ``get_last_traceback_evidence`` both read.
        """
        assert canonical_tool_name("atlas.log_analysis") == "panda_log_analysis"
        assert canonical_tool_name("panda_log_analysis") == "panda_log_analysis"

    def test_unknown_names_pass_through_unchanged(self) -> None:
        """An unresolvable name is returned as given, not raised on.

        ``_execute_one_tool`` reports it as unknown using the caller's own
        spelling, which is what makes the error diagnosable.
        """
        assert canonical_tool_name("no_such_tool") == "no_such_tool"
        assert canonical_tool_name("") == ""

    def test_every_wire_name_is_its_own_canonical_form(self) -> None:
        """Canonicalisation is idempotent on the names clients can call."""
        for name in _wire_names():
            assert canonical_tool_name(name) == name

    def test_ambiguous_aliases_are_omitted(self) -> None:
        """An alias claimed by two wire names resolves to neither.

        Guessing would route one plugin's evidence to another plugin's reader.
        """
        with patch(
            "bamboo.tools._tool_names.list_tool_entry_points",
            return_value=[
                {"name": "atlas.widget", "group": "bamboo.tools", "value": "x"},
                {"name": "epic.widget", "group": "bamboo.tools", "value": "y"},
            ],
        ), patch("bamboo.tools._tool_names.find_tool_by_name") as find_mock:
            obj = MagicMock()
            obj.get_definition.return_value = {"name": "widget"}
            find_mock.return_value = MagicMock(obj=obj)

            # The map is memoised; without this the patched discovery would be
            # ignored in favour of whatever a previous test cached.
            reset_alias_cache()
            try:
                aliases = alias_map()
            finally:
                reset_alias_cache()

        assert "widget" not in aliases
        assert aliases["atlas.widget"] == "atlas.widget"
        assert aliases["epic.widget"] == "epic.widget"


class TestPlannerCatalogMatchesTheWire:
    """What the planner may propose is what a client can actually call."""

    def test_catalog_names_are_all_wire_names(self) -> None:
        """No catalogued tool is unreachable.

        The catalog previously advertised ``core_dump_analysis``, which the
        server does not expose, while the routing guidance named
        ``atlas.core_dump_analysis``, which the catalog did not list.
        """
        from bamboo.tools.planner import _collect_tool_catalog

        catalog = {str(entry["name"]) for entry in _collect_tool_catalog()}
        assert catalog <= _wire_names()

    def test_every_namespaced_name_in_the_prompt_is_catalogued(self) -> None:
        """The routing guidance never names a tool the hard rule forbids.

        The planner is told to propose only catalogued tools.  A guidance line
        naming an uncatalogued tool is not a no-op: the model resolves the
        contradiction by discarding the guidance and picking a catalogued
        neighbour, which is how an explicit core-dump request came back as a
        log analysis.
        """
        import re

        from bamboo.tools.planner import _collect_tool_catalog, build_planner_system_prompt

        catalog = {str(entry["name"]) for entry in _collect_tool_catalog()}
        prompt = build_planner_system_prompt({}, "atlas")
        named = set(re.findall(r"\b(?:atlas|epic|cgsim)\.[a-z_]+", prompt))
        assert named <= catalog


class TestExecutorRecordsCanonicalNames:
    """The executor records the wire name, not the plan's spelling."""

    @staticmethod
    def _tool_call(name: str) -> Any:
        """Build a minimal tool-call descriptor.

        Args:
            name: The spelling the plan used.

        Returns:
            An object exposing ``tool``, ``namespace`` and ``arguments``.
        """
        tc = MagicMock()
        tc.tool = name
        tc.namespace = None
        tc.arguments = {"job_id": 1}
        return tc

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "requested",
        ["atlas.core_dump_analysis", "core_dump_analysis"],
    )
    async def test_an_alias_spelling_still_reaches_core_dump_synthesis(
        self, requested: str,
    ) -> None:
        """Either spelling records ``_CORE_DUMP_TOOL``.

        ``_resolve_tool`` accepts both, so both run the tool.  Before
        canonicalisation the unqualified spelling stored its evidence under a
        key ``_core_dump_evidence`` does not read and produced a
        ``called_tool_names`` entry ``_CORE_DUMP_TOOL in ...`` does not match,
        so ``_synthesise_core_dump`` was skipped and ``reconcile_llm_analysis``
        never ran on the model's output.
        """
        tool_obj = MagicMock()
        tool_obj.get_definition.return_value = {}
        tool_obj.call = AsyncMock(return_value=[{
            "type": "text",
            "text": '{"evidence": {"state": "queued"}, "text": "queued"}',
        }])

        called: list[str] = []
        errors: list[str] = []
        with patch.object(ex_mod, "_resolve_tool", return_value=tool_obj):
            await _execute_one_tool(self._tool_call(requested), called, [], errors)

        assert errors == []
        assert called == [_CORE_DUMP_TOOL]
        assert _CORE_DUMP_TOOL in ex_mod._last_evidence_store
        assert ex_mod._last_evidence_store["last_tool"] == _CORE_DUMP_TOOL

    @pytest.mark.asyncio
    async def test_an_unknown_tool_is_reported_with_the_requested_spelling(
        self,
    ) -> None:
        """The error echoes what the plan wrote, not a normalised guess."""
        called: list[str] = []
        errors: list[str] = []
        with patch.object(ex_mod, "_resolve_tool", return_value=None):
            await _execute_one_tool(self._tool_call("wobble"), called, [], errors)

        assert called == []
        assert errors == ["Unknown tool: wobble"]
