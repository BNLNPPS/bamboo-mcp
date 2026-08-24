#!/usr/bin/env python3
"""Standalone CLI entry point for the PanDA core-dump analyzer.

The implementation lives in ``askpanda_atlas._core_dump_analyzer`` rather than
here.  It has to: the ``atlas.core_dump_analysis`` MCP tool imports
``reconcile_llm_analysis``, ``core_evidence_from_dict``, ``build_system_prompt``,
``build_user_prompt`` and ``extract_json_object`` from it, and the ATLAS plugin's
``pyproject.toml`` lists its packages explicitly, so a module outside
``askpanda_atlas/`` would be absent from any non-editable install — working in a
source checkout and failing only in the container image.

This wrapper exists so the analyzer is still discoverable and runnable from
``scripts/``, which is where the repository keeps its operator tools::

    python scripts/analyze_core_dump.py core.18277 --mode hang
    python scripts/analyze_core_dump.py core.18277 --no-llm --json evidence.json

It adds the plugin directory to ``sys.path`` when the package is not already
importable, so the script works from a plain checkout with nothing installed.
See ``scripts/README-core_dump_analysis.md`` for usage, prerequisites and the
relationship to the MCP tool.

Every argument is forwarded unchanged; run with ``--help`` for the full list.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _ensure_importable() -> None:
    """Put the ATLAS plugin directory on ``sys.path`` if it is not already there.

    Prefers an installed ``askpanda_atlas`` and only falls back to the checkout
    layout, so that a deployed environment is never shadowed by whichever
    working copy the script happens to be run from.
    """
    try:
        import askpanda_atlas  # noqa: F401
    except ImportError:
        plugin_dir = Path(__file__).resolve().parents[1] / "packages" / "askpanda_atlas"
        if not plugin_dir.is_dir():
            raise
        sys.path.insert(0, str(plugin_dir))


def main() -> int:
    """Delegate to the analyzer's own entry point.

    Returns:
        The analyzer's exit status: ``0`` on success, ``1`` on a handled error,
        ``130`` on interrupt.
    """
    _ensure_importable()
    from askpanda_atlas._core_dump_analyzer import main as analyzer_main

    return analyzer_main()


if __name__ == "__main__":
    sys.exit(main())
