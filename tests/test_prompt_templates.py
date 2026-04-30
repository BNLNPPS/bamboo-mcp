"""Tests for :func:`bamboo.prompts.templates.get_bamboo_system_prompt`.

Verifies that the system prompt is correctly tailored to each plugin so
the LLM does not frame answers in terms of the wrong experiment domain.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_atlas_prompt_mentions_atlas() -> None:
    """ATLAS system prompt mentions PanDA and ATLAS."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    result = await get_bamboo_system_prompt(plugin_id="atlas")
    text = result["messages"][0]["content"]["text"]
    assert "ATLAS" in text
    assert "PanDA" in text


@pytest.mark.asyncio
async def test_epic_prompt_mentions_epic() -> None:
    """ePIC system prompt mentions ePIC."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    result = await get_bamboo_system_prompt(plugin_id="epic")
    text = result["messages"][0]["content"]["text"]
    assert "ePIC" in text


@pytest.mark.asyncio
async def test_cgsim_prompt_mentions_cgsim_and_simgrid() -> None:
    """CGSim system prompt mentions CGSim and SimGrid."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    result = await get_bamboo_system_prompt(plugin_id="cgsim")
    text = result["messages"][0]["content"]["text"]
    assert "CGSim" in text
    assert "SimGrid" in text


@pytest.mark.asyncio
async def test_cgsim_prompt_does_not_say_askpanda() -> None:
    """CGSim system prompt does not call the assistant AskPanDA."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    result = await get_bamboo_system_prompt(plugin_id="cgsim")
    text = result["messages"][0]["content"]["text"]
    assert "AskPanDA" not in text


@pytest.mark.asyncio
async def test_cgsim_prompt_discourages_panda_framing() -> None:
    """CGSim system prompt explicitly instructs not to frame in terms of PanDA/ATLAS."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    result = await get_bamboo_system_prompt(plugin_id="cgsim")
    text = result["messages"][0]["content"]["text"]
    assert "Do not frame" in text or "not frame" in text


@pytest.mark.asyncio
async def test_unknown_plugin_uses_default() -> None:
    """Unknown plugin_id falls back to the generic default prompt."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    result = await get_bamboo_system_prompt(plugin_id="unknown_plugin_xyz")
    text = result["messages"][0]["content"]["text"]
    assert isinstance(text, str)
    assert len(text) > 0


@pytest.mark.asyncio
async def test_default_plugin_is_atlas() -> None:
    """Default (no argument) behaves the same as plugin_id='atlas'."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    default = await get_bamboo_system_prompt()
    explicit = await get_bamboo_system_prompt(plugin_id="atlas")
    assert default == explicit


@pytest.mark.asyncio
async def test_result_structure() -> None:
    """Return value always has the expected MCP messages structure."""
    from bamboo.prompts.templates import get_bamboo_system_prompt

    for plugin_id in ("atlas", "epic", "cgsim", "default"):
        result = await get_bamboo_system_prompt(plugin_id=plugin_id)
        assert "messages" in result
        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert msg["role"] == "assistant"
        assert isinstance(msg["content"]["text"], str)
