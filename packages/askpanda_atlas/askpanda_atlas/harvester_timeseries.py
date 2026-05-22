"""Harvester timeseries tool — askpanda_atlas plugin package.

Delegates to the canonical implementation in
``askpanda_atlas.harvester_timeseries_impl``.  The fallback path
(ImportError) covers environments where ``opensearch-py`` or
``opensearch-dsl`` are not installed; in that case the tool is not
registered and the TUI skips the timeseries chart silently.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

try:
    from askpanda_atlas.harvester_timeseries_impl import (  # noqa: F401
        PandaHarvesterTimeseriesTool,
        get_definition,
        panda_harvester_timeseries_tool,
    )
except ImportError as _exc:
    _logger.warning(
        "panda_harvester_timeseries tool unavailable (missing dependency): %s", _exc
    )
    raise

__all__ = [
    "PandaHarvesterTimeseriesTool",
    "get_definition",
    "panda_harvester_timeseries_tool",
]
