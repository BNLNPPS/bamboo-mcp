"""AskCGSim UI manifest tool.

Returns UI branding metadata used by Bamboo clients (Textual / Streamlit).

This module deliberately avoids importing from ``bamboo.tools.base`` so it
can be loaded standalone (e.g. during testing of just the plugin package).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any, Sequence


def _text_content(text: str) -> list[dict[str, Any]]:
    """Create a minimal MCP text content payload.

    This is a local copy of the ``text_content`` helper from
    ``bamboo.tools.base`` so the plugin package does not have a hard
    import dependency on bamboo core.

    Args:
        text: Response text.

    Returns:
        One-element list compatible with the MCP content format.
    """
    return [{"type": "text", "text": text}]


def _load_banner_lines() -> Sequence[str]:
    """Load ASCII banner lines from banner.txt shipped with this plugin.

    Returns:
        Sequence of non-empty banner lines, falling back to a plain-text
        banner if the resource file cannot be read.
    """
    try:
        pkg = __package__ or __name__.rpartition(".")[0]
        banner_path = resources.files(pkg).joinpath("banner.txt")
        txt = banner_path.read_text(encoding="utf-8")
        lines = [ln.rstrip("\n") for ln in txt.splitlines()]
        return [ln for ln in lines if ln.strip() != ""]
    except Exception:  # pylint: disable=broad-exception-caught
        return (
            " █████╗ ███████╗██╗  ██╗ ██████╗ ██████╗ ███████╗██╗███╗   ███╗\n"
            "██╔══██╗██╔════╝██║ ██╔╝██╔════╝██╔════╝ ██╔════╝██║████╗ ████║\n"
            "███████║███████╗█████╔╝ ██║     ██║  ███╗███████╗██║██╔████╔██║\n"
            "██╔══██║╚════██║██╔═██╗ ██║     ██║   ██║╚════██║██║██║╚██╔╝██║\n"
            "██║  ██║███████║██║  ██╗╚██████╗╚██████╔╝███████║██║██║ ╚═╝ ██║\n"
            "╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝╚═╝     ╚═╝"
        ).splitlines()


@dataclass(frozen=True)
class CgsimUiManifestTool:
    """Tool that returns UI metadata for AskCGSim / Bamboo MCP."""

    @staticmethod
    def get_definition() -> dict[str, Any]:
        """Return MCP tool definition.

        Returns:
            Tool definition dict.
        """
        return {
            "name": "cgsim.ui_manifest",
            "description": (
                "Return UI branding metadata (banner, display name, help text, accent)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }

    async def call(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the manifest payload as JSON text.

        Args:
            arguments: Unused; present for MCP interface compatibility.

        Returns:
            One-element MCP content list with JSON-encoded manifest.
        """
        _ = arguments  # unused

        payload = {
            "plugin_id": "cgsim",
            "display_name": "Bamboo – AskCGSim",
            "help": (
                "Enter to send \u2022 /help \u2022 /plugin <id> "
                "\u2022 /tools \u2022 /debug on|off"
            ),
            "banner": list(_load_banner_lines()),
            "accent": "green",
        }
        return _text_content(json.dumps(payload, ensure_ascii=False))


cgsim_ui_manifest_tool = CgsimUiManifestTool()

__all__ = ["CgsimUiManifestTool", "cgsim_ui_manifest_tool"]
