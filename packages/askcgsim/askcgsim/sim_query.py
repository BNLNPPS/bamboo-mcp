"""CGSim simulation database NL-to-SQL query tool — askcgsim plugin package.

Delegates to the canonical implementation in
``askcgsim.sim_query_impl``.  The fallback path (ImportError) is reserved
for environments where ``sqlglot`` is not installed; in that case the tool
is not registered and Bamboo falls back gracefully to the LLM planner which
will route to documentation search.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

try:
    from askcgsim.sim_query_impl import (  # noqa: F401
        CgsimSimQueryTool,
        cgsim_sim_query_tool,
        get_definition,
    )
except ImportError as _exc:
    _logger.warning(
        "cgsim.sim_query tool unavailable (missing dependency): %s", _exc
    )
    raise

__all__ = ["CgsimSimQueryTool", "cgsim_sim_query_tool", "get_definition"]
