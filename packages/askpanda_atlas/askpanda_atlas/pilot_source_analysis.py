"""ATLAS pilot source analysis tool — askpanda_atlas plugin package.

Delegates to the canonical implementation in
``askpanda_atlas.pilot_source_analysis_impl``.

If bamboo core is not installed ``pilot_source_analysis_impl`` will fail to
import ``bamboo.tools.base``; in that case a minimal fallback implementation
is used so the tool can still be exercised in isolation.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

try:
    from askpanda_atlas.pilot_source_analysis_impl import (  # noqa: F401
        PilotSourceAnalysisTool,
        extract_function_source,
        fetch_and_analyse_pilot_source,
        fetch_pilot_module,
        get_definition,
        parse_exception_line,
        parse_traceback_frames,
        pilot_source_analysis_tool,
    )
except ImportError:
    from askpanda_atlas._fallback_pilot_source_analysis import (  # type: ignore[no-redef]  # noqa: F401,F811
        PilotSourceAnalysisTool,
        extract_function_source,
        fetch_and_analyse_pilot_source,
        fetch_pilot_module,
        get_definition,
        parse_exception_line,
        parse_traceback_frames,
    )
    pilot_source_analysis_tool = PilotSourceAnalysisTool()  # type: ignore[assignment]  # noqa: F811

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
