"""Implementation of ``panda_harvester_timeseries`` — per-bucket pilot counts.

Queries the OpenSearch ``atlas_harvesterworkers-*`` index for per-bucket
worker counts for a single status over a time window::

    GET atlas_harvesterworkers-*
    filter: @timestamp in [from_dt, to_dt]
    filter: status.keyword == <status>
    filter: computingsite.keyword == <site>   (optional)
    agg:    date_histogram on @timestamp with fixed_interval

The bucket interval is derived automatically from the window duration so
that the resulting time series always has a reasonable number of bars
(≈ 12–20 buckets) regardless of whether the user asked about the last
20 minutes or the last 6 hours.

Environment variables
---------------------
ASKPANDA_OPENSEARCH
    Password for the OpenSearch HTTP auth.  **Required.**  The tool
    returns a graceful error dict when this variable is absent.
ASKPANDA_OPENSEARCH_HOST
    Base URL of the OpenSearch cluster.
    Default: ``https://os-atlas.cern.ch/os``
ASKPANDA_OPENSEARCH_USER
    HTTP auth username.  Default: ``pilot-monitor-agent``
ASKPANDA_OPENSEARCH_CA
    Path to the CA certificate bundle.
    Default: ``/etc/pki/tls/certs/CERN-bundle.pem``
ASKPANDA_OPENSEARCH_VERIFY_CERTS
    Set to ``"false"`` to disable TLS certificate verification (local
    development without the CERN CA bundle).

Public surface
--------------
- ``get_definition()``                         — MCP tool definition dict
- ``PandaHarvesterTimeseriesTool``             — MCP tool class
- ``panda_harvester_timeseries_tool``          — module-level singleton
- ``compute_interval(from_dt, to_dt)``         — derive bucket width
- ``fetch_timeseries(...)``                    — synchronous OpenSearch query
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_INDEX: str = "atlas_harvesterworkers-*"
_DEFAULT_HOST: str = "https://os-atlas.cern.ch/os"
_DEFAULT_USER: str = "pilot-monitor-agent"
_DEFAULT_CA: str = "/etc/pki/tls/certs/CERN-bundle.pem"

#: Cache TTL for timeseries results — 60 s matches metadata TTL.
_CACHE_TTL_SECS: float = 60.0

#: Cache key prefix so timeseries entries do not collide with other tools.
_CACHE_PREFIX: str = "harvester_timeseries:"


# ---------------------------------------------------------------------------
# Interval derivation
# ---------------------------------------------------------------------------


def compute_interval(from_dt: str, to_dt: str) -> str:
    """Derive a sensible OpenSearch ``fixed_interval`` for a time window.

    Targets approximately 12–20 buckets across the window:

    =========  ============  ==========
    Window     Interval      Max buckets
    =========  ============  ==========
    <= 30 min  ``1m``        30
    <= 3 h     ``5m``        36
    <= 12 h    ``15m``       48
    > 12 h     ``1h``        —
    =========  ============  ==========

    Args:
        from_dt: ISO-8601 lower bound of the window (no timezone suffix).
        to_dt:   ISO-8601 upper bound of the window (no timezone suffix).

    Returns:
        OpenSearch ``fixed_interval`` string.  Falls back to ``"5m"`` if
        either timestamp cannot be parsed.
    """
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        t0 = datetime.strptime(from_dt[:19], fmt)
        t1 = datetime.strptime(to_dt[:19], fmt)
        minutes = (t1 - t0).total_seconds() / 60.0
    except (ValueError, TypeError):
        return "5m"

    if minutes <= 30:
        return "1m"
    if minutes <= 180:
        return "5m"
    if minutes <= 720:
        return "15m"
    return "1h"


# ---------------------------------------------------------------------------
# Default time window
# ---------------------------------------------------------------------------


def _default_window() -> tuple[str, str]:
    """Return ISO-8601 strings for a one-hour window ending now (UTC).

    Returns:
        ``(from_dt_iso, to_dt_iso)`` formatted as ``YYYY-MM-DDTHH:MM:SS``.
    """
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    one_hour_ago = now - timedelta(hours=1)
    return (
        one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S"),
        now.strftime("%Y-%m-%dT%H:%M:%S"),
    )


# ---------------------------------------------------------------------------
# OpenSearch client
# ---------------------------------------------------------------------------


def create_os_client() -> Any:
    """Create an authenticated OpenSearch client from environment variables.

    Delegates to :func:`bamboo.llm.opensearch_client.create_os_client` using
    the ``ASKPANDA_OPENSEARCH`` read password.  Kept as a module-level
    function so existing call sites and tests that patch it by name continue
    to work without modification.

    Returns:
        An authenticated :class:`opensearchpy.OpenSearch` client.

    Raises:
        RuntimeError: If ``ASKPANDA_OPENSEARCH`` is not set.
        ImportError: If ``opensearch-py`` is not installed.
    """
    from bamboo.llm.opensearch_client import create_os_client as _shared_factory

    password = os.environ.get("ASKPANDA_OPENSEARCH", "")
    if not password:
        raise RuntimeError(
            "Environment variable ASKPANDA_OPENSEARCH is not set. "
            "Set it to your OpenSearch password to enable timeseries charts."
        )
    return _shared_factory(password)


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------


def fetch_timeseries(
    status: str,
    from_dt: str,
    to_dt: str,
    site: str | None = None,
    interval: str | None = None,
    index: str = _DEFAULT_INDEX,
) -> list[dict[str, Any]]:
    """Fetch per-bucket pilot counts from OpenSearch for a single status.

    Creates a fresh OpenSearch client for each call.  Checks the
    module-level cache first; on a miss, executes the query and caches
    the result for :data:`_CACHE_TTL_SECS` seconds.

    The query applies a ``date_histogram`` aggregation over ``@timestamp``
    with a ``fixed_interval`` derived from the window (or supplied by the
    caller).  An optional ``computingsite.keyword`` term filter scopes
    results to a single site.

    Args:
        status: Harvester worker status to filter on, e.g. ``"running"``.
        from_dt: ISO-8601 lower bound of the query window.
        to_dt: ISO-8601 upper bound of the query window.
        site: Optional computing-site filter (``computingsite.keyword``).
        interval: OpenSearch ``fixed_interval`` override.  Derived
            automatically when ``None``.
        index: OpenSearch index pattern to query.

    Returns:
        List of ``{"timestamp": str, "count": int}`` dicts in ascending
        timestamp order.  Empty list when no buckets are returned.

    Raises:
        RuntimeError: If ``ASKPANDA_OPENSEARCH`` is not set or the query
            fails with a non-recoverable error.
        ImportError: If ``opensearch-py`` or ``opensearch-dsl`` are not
            installed.
    """
    from askpanda_atlas._cache import _MISS, _get, _set  # type: ignore[import]

    effective_interval = interval or compute_interval(from_dt, to_dt)
    cache_key = (
        f"{_CACHE_PREFIX}{status}|{from_dt}|{to_dt}|{site or ''}|{effective_interval}"
    )

    cached = _get(cache_key)
    if cached is not _MISS:
        logger.debug("panda_harvester_timeseries: cache hit for %s", cache_key)
        return cached  # type: ignore[return-value]

    # create_os_client() validates ASKPANDA_OPENSEARCH before the import.
    client = create_os_client()

    from opensearch_dsl import Search  # type: ignore[import]

    s = (
        Search(using=client, index=index)
        .filter("range", **{"@timestamp": {"gte": from_dt, "lte": to_dt}})
        .filter("term", **{"status.keyword": status})
        .extra(size=0)
    )
    if site:
        s = s.filter("term", **{"computingsite.keyword": site})

    s.aggs.bucket(
        "counts_over_time",
        "date_histogram",
        field="@timestamp",
        fixed_interval=effective_interval,
    )

    response = s.execute()
    buckets: list[dict[str, Any]] = [
        {"timestamp": b.key_as_string, "count": b.doc_count}
        for b in response.aggregations.counts_over_time.buckets
    ]

    _set(cache_key, buckets, _CACHE_TTL_SECS)
    logger.debug(
        "panda_harvester_timeseries: %d buckets status=%r site=%r interval=%s",
        len(buckets), status, site, effective_interval,
    )
    return buckets


# ---------------------------------------------------------------------------
# Structured error constructor
# ---------------------------------------------------------------------------


def _error_evidence(
    status: str,
    from_dt: str,
    to_dt: str,
    site: str | None,
    detail: str,
) -> dict[str, Any]:
    """Return a structured evidence dict representing a fetch failure.

    Args:
        status: Requested status filter.
        from_dt: Requested time-range lower bound.
        to_dt: Requested time-range upper bound.
        site: Requested site filter, or ``None``.
        detail: Internal error message (logged at DEBUG only).

    Returns:
        Evidence dict with ``error`` populated and ``buckets`` empty.
    """
    logger.debug("panda_harvester_timeseries error: %s", detail)
    return {
        "status": status,
        "interval": None,
        "from_dt": from_dt,
        "to_dt": to_dt,
        "site_filter": site,
        "buckets": [],
        "total_buckets": 0,
        "endpoint": _DEFAULT_INDEX,
        "error": (
            "Could not retrieve Harvester timeseries data. "
            "Check that ASKPANDA_OPENSEARCH is set and the OpenSearch "
            "cluster is reachable."
        ),
    }


# ---------------------------------------------------------------------------
# MCP tool definition
# ---------------------------------------------------------------------------


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for ``panda_harvester_timeseries``.

    Returns:
        Tool definition dict compatible with MCP discovery.
    """
    return {
        "name": "panda_harvester_timeseries",
        "description": (
            "Fetch per-bucket Harvester pilot counts from OpenSearch for a "
            "single status over a time window.  Used to render ASCII "
            "time-series charts in the TUI.  Requires ASKPANDA_OPENSEARCH "
            "to be set."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Original user question.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Harvester worker status to chart, e.g. 'running', "
                        "'submitted', 'finished', 'failed'.  Default: 'running'."
                    ),
                },
                "from_dt": {
                    "type": "string",
                    "description": "ISO-8601 start of the window.  Default: one hour ago.",
                },
                "to_dt": {
                    "type": "string",
                    "description": "ISO-8601 end of the window.  Default: now.",
                },
                "site": {
                    "type": "string",
                    "description": "Optional computing-site filter (``computingsite.keyword`` field), e.g. 'BNL'.",
                },
                "interval": {
                    "type": "string",
                    "description": (
                        "OpenSearch fixed_interval override, e.g. '1m', '5m'. "
                        "Derived automatically when omitted."
                    ),
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class PandaHarvesterTimeseriesTool:
    """MCP tool fetching per-bucket Harvester pilot counts from OpenSearch.

    Queries the ``atlas_harvesterworkers-*`` index for a single status
    over a configurable time window, returning a compact bucket list
    suitable for ASCII chart rendering in the TUI.
    """

    def __init__(self) -> None:
        """Initialise with the cached tool definition."""
        self._def: dict[str, Any] = get_definition()

    def get_definition(self) -> dict[str, Any]:
        """Return the MCP tool definition.

        Returns:
            Tool definition dictionary.
        """
        return self._def

    async def call(self, arguments: dict[str, Any]) -> list[Any]:
        """Fetch Harvester timeseries and return evidence as MCP content.

        Resolves the time window (defaulting to the last hour), derives
        the bucket interval from the window duration, and wraps the
        blocking OpenSearch query in ``asyncio.to_thread``.

        Args:
            arguments: Dict with optional ``"status"`` (str),
                ``"from_dt"`` (str), ``"to_dt"`` (str), ``"site"``
                (str), ``"interval"`` (str).

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence dict, or an error payload on failure.
        """
        from bamboo.tools.base import text_content  # deferred — bamboo-core

        status: str = (arguments.get("status") or "running").strip().lower()
        site: str | None = (arguments.get("site") or "").strip() or None

        from_dt_raw: str | None = arguments.get("from_dt")
        to_dt_raw: str | None = arguments.get("to_dt")
        if from_dt_raw and to_dt_raw:
            from_dt = from_dt_raw.strip()
            to_dt = to_dt_raw.strip()
        else:
            from_dt, to_dt = _default_window()
            if from_dt_raw:
                from_dt = from_dt_raw.strip()
            if to_dt_raw:
                to_dt = to_dt_raw.strip()

        interval: str | None = (arguments.get("interval") or "").strip() or None
        effective_interval = interval or compute_interval(from_dt, to_dt)

        logger.debug(
            "panda_harvester_timeseries: status=%r site=%r from=%r to=%r interval=%s",
            status, site, from_dt, to_dt, effective_interval,
        )

        try:
            buckets = await asyncio.to_thread(
                fetch_timeseries, status, from_dt, to_dt, site, effective_interval,
            )
            evidence: dict[str, Any] = {
                "status": status,
                "interval": effective_interval,
                "from_dt": from_dt,
                "to_dt": to_dt,
                "site_filter": site,
                "buckets": buckets,
                "total_buckets": len(buckets),
                "endpoint": _DEFAULT_INDEX,
                "error": None,
            }
            return text_content(json.dumps({"evidence": evidence}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("panda_harvester_timeseries tool call failed")
            error_ev = _error_evidence(status, from_dt, to_dt, site, detail=repr(exc))
            return text_content(json.dumps({"evidence": error_ev}))


panda_harvester_timeseries_tool = PandaHarvesterTimeseriesTool()

__all__ = [
    "PandaHarvesterTimeseriesTool",
    "compute_interval",
    "fetch_timeseries",
    "get_definition",
    "panda_harvester_timeseries_tool",
]
