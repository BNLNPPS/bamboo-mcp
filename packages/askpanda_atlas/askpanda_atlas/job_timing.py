"""PanDA job timing tool — askpanda_atlas plugin package.

Delegates to the canonical implementation in
``askpanda_atlas.job_timing_impl``.  The fallback path (ImportError) covers
environments where ``opensearch-py`` or ``opensearch-dsl`` are not installed;
in that case the tool is not registered and Bamboo falls back gracefully to
the LLM planner which will route to documentation search.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

try:
    from askpanda_atlas.job_timing_impl import (  # noqa: F401
        PandaJobTimingTool,
        get_definition,
        panda_job_timing_tool,
    )
except ImportError as _exc:
    _logger.warning(
        "panda_job_timing tool unavailable (missing dependency): %s", _exc
    )
    raise

__all__ = [
    "PandaJobTimingTool",
    "get_definition",
    "panda_job_timing_tool",
]
