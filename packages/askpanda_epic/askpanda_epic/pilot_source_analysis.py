"""ePIC PanDA pilot source analysis tool — askpanda_epic plugin package.

Delegates to the canonical implementation in
``askpanda_atlas.pilot_source_analysis_impl``.

The pilot source analysis logic is experiment-agnostic: ePIC and ATLAS both
use the PanDAWMS/pilot3 codebase, so the same traceback parsing, GitHub
fetching, and AST-based function extraction applies to both experiments.

If ``askpanda_atlas`` is not installed a minimal fallback implementation is
used so the tool can still be exercised in isolation without crashing.
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
    from askpanda_epic._fallback_pilot_source_analysis import (  # type: ignore[no-redef]  # noqa: F401,F811
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
