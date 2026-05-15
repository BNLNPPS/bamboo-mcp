"""Prompt templates.

In MCP, prompts are a discoverable interface for clients.
Keep prompts small and composable; use tools for data access.
"""
from __future__ import annotations
from typing import Any


def _text_msg(text: str) -> dict[str, Any]:
    """Create a text message dict for MCP prompt messages.

    Args:
        text: The text content of the message.

    Returns:
        Dictionary with role 'assistant' and text content.
    """
    return {"role": "assistant", "content": {"type": "text", "text": text}}


#: Per-plugin identity lines injected into the system prompt.
#: Keys are plugin IDs; the fallback is ``"default"``.
_PLUGIN_IDENTITY: dict[str, str] = {
    "atlas": (
        "You are AskPanDA, an assistant for PanDA/ATLAS workflow operations. "
        "Prefer calling tools for factual data (task status, queue info, pilots). "
        "If data is missing, ask for identifiers (task id, job id, site) and propose next steps."
    ),
    "epic": (
        "You are AskPanDA, an assistant for PanDA/ePIC workflow operations at BNL. "
        "Prefer calling tools for factual data (task status, queue info, pilots). "
        "If data is missing, ask for identifiers (task id, job id, site) and propose next steps."
    ),
    "cgsim": (
        "You are Bamboo, an assistant for AskCGSim and SimGrid distributed computing "
        "simulation, with specific knowledge of the CGSim/PanDA integration. "
        "CGSim is a SimGrid-based framework for simulating large-scale computing "
        "grids such as the WLCG. It ingests historical PanDA job records for "
        "calibration and is designed to simulate infrastructures managed by PanDA. "
        "Questions about the PanDA/CGSim connection — such as simulating PanDA "
        "brokerage, using PanDA job logs for calibration, or modelling ATLAS/PanDA "
        "workloads in CGSim — are explicitly in scope and should be answered directly. "
        "Prefer calling tools for factual data. "
        "Do not deflect PanDA/CGSim correlation questions as out of scope."
    ),
    "default": (
        "You are Bamboo, an assistant for distributed computing and HEP workflow systems. "
        "Prefer calling tools for factual data.  If data is missing, ask for identifiers and "
        "propose next steps."
    ),
}


async def get_bamboo_system_prompt(plugin_id: str = "atlas") -> dict[str, Any]:
    """Get the system prompt for the Bamboo assistant.

    The prompt is tailored to the active plugin so the LLM does not
    incorrectly frame answers in terms of a different experiment's domain.

    Args:
        plugin_id: Active plugin identifier (e.g. ``"atlas"``, ``"epic"``,
            ``"cgsim"``).  Defaults to ``"atlas"`` for backwards compatibility.

    Returns:
        Dictionary with 'messages' list containing the system prompt.
    """
    identity = _PLUGIN_IDENTITY.get(plugin_id, _PLUGIN_IDENTITY["default"])
    return {"messages": [_text_msg(identity)]}


async def get_failure_triage_prompt(log_text: str) -> dict[str, Any]:
    """Get a prompt template for analyzing failure logs.

    Produces a structured analysis prompt for triaging workflow failures including
    classification, root causes, mitigation steps, and metadata collection guidance.

    Args:
        log_text: The failure log text to be analyzed.

    Returns:
        Dictionary with 'messages' list containing the analysis prompt.
    """
    return {
        "messages": [
            _text_msg(
                "Analyze the following failure log and produce: "
                "(1) classification, (2) likely root causes, (3) immediate mitigation, "
                "(4) what additional metadata to collect.\n\n"
                f"LOG:\n{log_text}"
            )
        ]
    }
