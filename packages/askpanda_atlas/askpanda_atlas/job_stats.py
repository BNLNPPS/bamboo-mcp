"""PanDA job stats tool — askpanda_atlas plugin package.

Delegates to the canonical implementation in
``askpanda_atlas.job_stats_impl``.  The fallback path (ImportError) covers
environments where ``opensearch-py`` or ``opensearch-dsl`` are not installed;
in that case the tool is not registered and Bamboo falls back gracefully to
the LLM planner which will route to documentation search.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

try:
    from askpanda_atlas.job_stats_impl import (  # noqa: F401
        PandaJobStatsTool,
        get_definition,
        panda_job_stats_tool,
    )
except ImportError as _exc:
    _logger.warning(
        "panda_job_stats tool unavailable (missing dependency): %s", _exc
    )
    raise

__all__ = [
    "PandaJobStatsTool",
    "get_definition",
    "panda_job_stats_tool",
]
