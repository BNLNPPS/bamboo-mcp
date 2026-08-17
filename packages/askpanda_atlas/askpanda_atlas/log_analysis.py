"""ATLAS PanDA job log analysis tool — askpanda_atlas plugin package.

Delegates to the canonical implementation in
``askpanda_atlas.log_analysis_impl``.

If bamboo core is not installed ``log_analysis_impl`` will fail to import
``bamboo.tools.base``; in that case a minimal fallback implementation is
used so the tool can still be exercised in isolation.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

try:
    from askpanda_atlas.log_analysis_impl import (  # noqa: F401
        FailureContext,
        PandaLogAnalysisTool,
        classify_failure,
        extract_failure_context,
        extract_log_excerpt,
        get_definition,
        panda_log_analysis_tool,
    )
except ImportError:
    from askpanda_atlas._fallback_log_analysis import (  # type: ignore[no-redef]  # noqa: F401,F811
        FailureContext,
        PandaLogAnalysisTool,
        classify_failure,
        extract_failure_context,
        extract_log_excerpt,
        get_definition,
    )
    panda_log_analysis_tool = PandaLogAnalysisTool()  # type: ignore[assignment]  # noqa: F811

__all__ = [
    "FailureContext",
    "PandaLogAnalysisTool",
    "classify_failure",
    "extract_failure_context",
    "extract_log_excerpt",
    "get_definition",
    "panda_log_analysis_tool",
]
