"""PanDA job log analysis tool — public re-export for core.py and tests.

The canonical implementation lives in ``askpanda_atlas.log_analysis``.
This module re-exports it so the rest of core can use the stable name
``bamboo.tools.log_analysis`` without importing directly from a plugin.
"""
from __future__ import annotations

from askpanda_atlas.log_analysis import (  # noqa: F401  (re-export)
    FailureContext,
    PandaLogAnalysisTool,
    classify_failure,
    extract_failure_context,
    extract_log_excerpt,
    get_definition,
    panda_log_analysis_tool,
)

__all__ = [
    "FailureContext",
    "PandaLogAnalysisTool",
    "classify_failure",
    "extract_failure_context",
    "extract_log_excerpt",
    "get_definition",
    "panda_log_analysis_tool",
]
