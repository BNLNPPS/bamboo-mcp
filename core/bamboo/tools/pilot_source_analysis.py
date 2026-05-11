"""Pilot source analysis tool — public re-export for core.py and tests.

The canonical implementation lives in
``askpanda_atlas.pilot_source_analysis_impl``.
This module re-exports it so the rest of core can use the stable name
``bamboo.tools.pilot_source_analysis`` without importing directly from a plugin.
"""
from __future__ import annotations

from askpanda_atlas.pilot_source_analysis import (  # noqa: F401  (re-export)
    PilotSourceAnalysisTool,
    extract_function_source,
    fetch_and_analyse_pilot_source,
    fetch_pilot_module,
    get_definition,
    parse_exception_line,
    parse_traceback_frames,
    pilot_source_analysis_tool,
)

__all__ = [
    "PilotSourceAnalysisTool",
    "extract_function_source",
    "fetch_and_analyse_pilot_source",
    "fetch_pilot_module",
    "get_definition",
    "parse_exception_line",
    "parse_traceback_frames",
    "pilot_source_analysis_tool",
]
