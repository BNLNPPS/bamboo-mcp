"""ATLAS core-dump analysis tool — askpanda_atlas plugin package.

Delegates to the canonical implementation in
``askpanda_atlas.core_dump_analysis_impl``.

There is deliberately **no fallback implementation**, so this follows the
``job_stats`` precedent rather than the ``log_analysis`` one: on ImportError
the failure is logged and re-raised, and the tool is simply not registered.

A fallback here could not do anything useful.  The implementation reaches
``askpanda_atlas._job_prep``, which is coupled to ``log_analysis_impl`` so that
the core it analyses is necessarily the one the probe named, and the analysis
itself needs CVMFS on the host, plus a container runtime and a gdb, regardless
of what is importable.  Note that neither of the latter two need be installed
on the host itself: ALRB supplies apptainer from CVMFS when the host has none,
and gdb comes from inside the release container.  A degraded path would be a
tool that accepts requests it cannot serve.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

try:
    from askpanda_atlas.core_dump_analysis_impl import (  # noqa: F401
        CoreDumpAnalysisTool,
        core_dump_analysis_tool,
        get_definition,
    )
except ImportError as _exc:
    _logger.warning(
        "core_dump_analysis tool unavailable (missing dependency): %s", _exc
    )
    raise

__all__ = [
    "CoreDumpAnalysisTool",
    "core_dump_analysis_tool",
    "get_definition",
]
