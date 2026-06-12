"""Implementation of ``panda_harvester_workers`` — Harvester pilot/worker statistics.

Fetches worker counts from the BigPanDA Harvester API endpoint::

    GET /harvester/getworkerstats/?lastupdate_from=<ISO>&lastupdate_to=<ISO>
                                   [&computingsite=<SITE>]

The endpoint returns a JSON list of records, each describing the number of
Harvester workers (``nworkers``) for a specific combination of
``computingsite``, ``harvesterid``, ``jobtype``, ``resourcetype``, and
``status``.  "Workers" and "pilots" are synonymous in this context.

The tool accepts an optional ``site`` filter and optional ISO-8601 ``from_dt``
/ ``to_dt`` window strings.  When timestamps are omitted the tool defaults to
the last hour so it answers questions like "How many pilots are running at BNL
right now?" without requiring the caller to supply a time range.

Public surface:

- ``get_definition()``                   — MCP tool definition dict
- ``PandaHarvesterWorkersTool``          — MCP tool class
- ``panda_harvester_workers_tool``       — module-level singleton

Design rules (per Bamboo architecture):

* All ``bamboo.tools.base`` and ``bamboo.llm.*`` imports are **deferred inside
  ``call()`` and helpers** — never at module level.  This keeps the module
  importable without bamboo installed.
* Blocking HTTP is wrapped in ``asyncio.to_thread()`` (HTTP fetches, unlike
  DuckDB, are safe with the thread pool on all platforms).
* ``call()`` never raises — errors are returned as ``text_content`` payloads.
* The full API response is stored as ``raw_payload`` in the evidence dict so
  ``bamboo_last_evidence`` can serve it via ``/json`` without re-fetching.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HARVESTER_PATH: str = "/harvester/getworkerstats/"

#: Short TTL for Harvester worker stats — pilots change frequently.
_CACHE_TTL_SECS: float = 30.0


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _default_window() -> tuple[str, str]:
    """Return ISO-8601 strings for a one-hour window ending now (UTC).

    Returns:
        ``(from_dt_iso, to_dt_iso)`` — both are UTC timestamps formatted as
        ``YYYY-MM-DDTHH:MM:SS``, suitable for the Harvester API.
    """
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    one_hour_ago = now - timedelta(hours=1)
    return one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S"), now.strftime("%Y-%m-%dT%H:%M:%S")


_ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _resolve_dt(value: str, fallback: str) -> str:
    """Return an absolute ISO-8601 timestamp, resolving relative expressions.

    The LLM planner occasionally emits OpenSearch-style relative expressions
    (``"now-6h"``, ``"now/d"``) or human-readable phrases (``"6 hours ago"``)
    instead of absolute ISO-8601 timestamps.  The BigPanDA Harvester API does
    not understand these and returns an error or empty result.

    Recognised relative patterns and their resolutions (all anchored to the
    current UTC time at call time):

    - ``now-Nh`` / ``now-Nm`` / ``now-Nd`` — subtract N hours/minutes/days
    - ``now/d`` — midnight of the current UTC day (start-of-day)
    - ``now`` — current UTC time
    - Any string not matching ``YYYY-MM-DD[T ]HH:MM:SS`` — returns *fallback*

    Args:
        value: Raw ``from_dt`` or ``to_dt`` string from the planner.
        fallback: Absolute ISO-8601 string to return when *value* cannot be
            resolved (e.g. the default window boundary).

    Returns:
        Absolute ISO-8601 string formatted as ``YYYY-MM-DDTHH:MM:SS`` (UTC).
    """
    import re as _re

    v = value.strip()

    # Already a valid absolute ISO timestamp — pass through unchanged.
    if _ISO_DT_RE.match(v):
        return v[:19]  # strip any trailing timezone suffix

    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S"

    # now-Nh / now-Nm / now-Nd
    _rel = _re.match(r"^now-(\d+)(h|m|d)$", v, _re.IGNORECASE)
    if _rel:
        n, unit = int(_rel.group(1)), _rel.group(2).lower()
        if unit == "h":
            delta = timedelta(hours=n)
        elif unit == "m":
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(days=n)
        return (now - delta).strftime(fmt)

    # now/d — start of current UTC day
    if _re.match(r"^now/d$", v, _re.IGNORECASE):
        return now.replace(hour=0, minute=0, second=0).strftime(fmt)

    # bare "now"
    if _re.match(r"^now$", v, _re.IGNORECASE):
        return now.strftime(fmt)

    # Unrecognised — use the supplied fallback
    logger.warning(
        "panda_harvester_workers: unrecognised from_dt/to_dt %r — using fallback %r",
        value, fallback,
    )
    return fallback


def build_harvester_url(
    base_url: str,
    from_dt: str,
    to_dt: str,
    site: str | None = None,
) -> str:
    """Build the Harvester worker-stats endpoint URL.

    Args:
        base_url: BigPanDA base URL, e.g. ``"https://bigpanda.cern.ch"``.
            Trailing slashes are stripped before appending the path.
        from_dt: Lower bound of the ``lastupdate`` filter in ISO-8601 format,
            e.g. ``"2026-03-01T00:00:00"``.
        to_dt: Upper bound of the ``lastupdate`` filter in ISO-8601 format.
        site: Optional computing site filter (``computingsite`` query param).
            Pass ``None`` to retrieve stats across all sites.

    Returns:
        Full URL string ready for an HTTP GET request.
    """
    root = base_url.rstrip("/")
    url = (
        f"{root}{_HARVESTER_PATH}"
        f"?lastupdate_from={from_dt}&lastupdate_to={to_dt}"
    )
    if site:
        url += f"&computingsite={site}"
    return url


# ---------------------------------------------------------------------------
# Synchronous HTTP fetch — no bamboo dependency
# ---------------------------------------------------------------------------


def fetch_worker_stats(
    base_url: str,
    from_dt: str,
    to_dt: str,
    site: str | None = None,
) -> dict[str, Any]:
    """Fetch Harvester worker stats and return a compact evidence dict.

    Synchronous; call via ``asyncio.to_thread()`` from async contexts.

    The Harvester API returns a JSON **list** of record dicts.  ``fetch_jsonish``
    wraps non-dict top-level values as ``{"_data": <value>}`` so callers always
    receive a dict.  This function extracts the list from ``"_data"`` when
    present, or walks the dict values looking for a list.

    Args:
        base_url: BigPanDA base URL (no trailing slash).
        from_dt: ISO-8601 lower bound for ``lastupdate_from``.
        to_dt: ISO-8601 upper bound for ``lastupdate_to``.
        site: Optional computing-site filter.

    Returns:
        Evidence dict with keys ``total_records``,
        ``nworkers_by_status``, ``nworkers_by_jobtype``,
        ``nworkers_by_resourcetype``, ``nworkers_by_site``,
        ``nworkers_total``, ``from_dt``, ``to_dt``, ``site_filter``,
        ``endpoint``, ``raw_payload``, and ``error``.  The raw record
        list is stored only in ``raw_payload`` and is stripped before
        LLM synthesis by ``bamboo_executor``.

    Raises:
        RuntimeError: If the HTTP response is non-2xx.
    """
    from askpanda_atlas._cache import cached_fetch_jsonish  # type: ignore[import]

    url = build_harvester_url(base_url, from_dt, to_dt, site)
    logger.debug("panda_harvester_workers: fetching %s", url)

    status, ctype, body, payload = cached_fetch_jsonish(url, ttl=_CACHE_TTL_SECS)

    if status < 200 or status >= 300:
        raise RuntimeError(
            f"HTTP {status} fetching Harvester worker stats from {url}"
        )
    if payload is None:
        snippet = body[:200] if body else ""
        raise RuntimeError(
            f"Non-JSON response (content-type={ctype!r}) from Harvester API: {snippet!r}"
        )

    # Extract the list of records from the parsed payload.
    # fetch_jsonish wraps a top-level list as {"_data": [...]}.
    records: list[dict[str, Any]] = _extract_records(payload)

    logger.debug(
        "panda_harvester_workers: received %d records (site_filter=%r)",
        len(records),
        site,
    )

    evidence = _aggregate_evidence(records, from_dt, to_dt, site)
    evidence["endpoint"] = url
    evidence["raw_payload"] = records  # store verbatim for /json inspection
    return evidence


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the list of worker-stat records from a parsed API payload.

    The Harvester API returns a bare JSON array which ``fetch_jsonish``
    wraps as ``{"_data": [...]}``.  If the top-level dict already is
    ``{"_data": list}``, unwrap it.  Otherwise search the dict values
    for the first non-empty list.  Fall back to an empty list on failure.

    Args:
        payload: Parsed JSON dict as returned by ``fetch_jsonish``.

    Returns:
        List of record dicts, or an empty list if none can be found.
    """
    # Common case: fetch_jsonish wraps a JSON array as {"_data": [...]}
    data = payload.get("_data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]

    # Fallback: search dict values for a list (future API change tolerance)
    for v in payload.values():
        if isinstance(v, list) and v:
            return [r for r in v if isinstance(r, dict)]

    return []


# ---------------------------------------------------------------------------
# Aggregation helpers (synchronous, no I/O)
# ---------------------------------------------------------------------------


def _aggregate_evidence(
    records: list[dict[str, Any]],
    from_dt: str,
    to_dt: str,
    site_filter: str | None,
) -> dict[str, Any]:
    """Aggregate raw Harvester records into a compact evidence dict.

    Produces two kinds of summary:

    1. **Flat breakdowns** (one dimension each): ``nworkers_by_status``,
       ``nworkers_by_jobtype``, ``nworkers_by_resourcetype``,
       ``nworkers_by_site``.

    2. **Pivot table** ``pivot``: a list of
       ``{status, jobtype, resourcetype, nworkers}`` dicts, summed
       across all harvester instances and computing sites.  This allows
       the LLM to answer any combination of status, job type, and
       resource type — including three-way slices like "running MCORE
       managed pilots" — without needing raw records.  The number of
       rows is bounded by
       ``len(statuses) × len(jobtypes) × len(resourcetypes)``
       (typically ≤ 54 in practice).

    Site is intentionally excluded from the pivot to keep it compact.
    Questions scoped to a specific site should use the ``site_filter``
    argument so the API query is already site-specific.  For all-sites
    queries, ``nworkers_by_site`` gives per-site totals.

    The raw records are **not** included in the returned dict — they are
    attached separately as ``raw_payload`` by the caller and stripped
    before LLM synthesis by ``bamboo_executor``.

    Args:
        records: List of raw record dicts from the Harvester API.  Each
            record is expected to have ``nworkers``, ``status``,
            ``jobtype``, ``resourcetype``, and ``computingsite`` keys.
        from_dt: ISO-8601 lower-bound timestamp used for the fetch.
        to_dt: ISO-8601 upper-bound timestamp used for the fetch.
        site_filter: The ``computingsite`` filter that was applied, or
            ``None`` if the query spans all sites.

    Returns:
        Compact evidence dict suitable for JSON serialisation and LLM
        synthesis.  Contains only aggregated counters, the pivot table,
        and metadata — never raw records.
    """
    nworkers_by_status: dict[str, int] = {}
    nworkers_by_jobtype: dict[str, int] = {}
    nworkers_by_resourcetype: dict[str, int] = {}
    nworkers_by_site: dict[str, int] = {}
    # Pivot keyed by (status, jobtype, resourcetype) — site excluded to stay compact.
    _pivot: dict[tuple[str, str, str], int] = {}
    nworkers_total: int = 0

    for rec in records:
        n = _safe_int(rec.get("nworkers"))
        if n is None:
            continue

        status = str(rec.get("status") or "unknown")
        jobtype = str(rec.get("jobtype") or "unknown")
        restype = str(rec.get("resourcetype") or "unknown")
        csite = str(rec.get("computingsite") or "unknown")

        nworkers_by_status[status] = nworkers_by_status.get(status, 0) + n
        nworkers_by_jobtype[jobtype] = nworkers_by_jobtype.get(jobtype, 0) + n
        nworkers_by_resourcetype[restype] = nworkers_by_resourcetype.get(restype, 0) + n
        nworkers_by_site[csite] = nworkers_by_site.get(csite, 0) + n
        nworkers_total += n

        key = (status, jobtype, restype)
        _pivot[key] = _pivot.get(key, 0) + n

    # Sort flat breakdowns by count descending for readability.
    nworkers_by_status = dict(
        sorted(nworkers_by_status.items(), key=lambda kv: kv[1], reverse=True)
    )
    nworkers_by_jobtype = dict(
        sorted(nworkers_by_jobtype.items(), key=lambda kv: kv[1], reverse=True)
    )
    nworkers_by_resourcetype = dict(
        sorted(nworkers_by_resourcetype.items(), key=lambda kv: kv[1], reverse=True)
    )
    nworkers_by_site = dict(
        sorted(nworkers_by_site.items(), key=lambda kv: kv[1], reverse=True)
    )

    # Serialise pivot as a list of dicts sorted by nworkers descending.
    pivot = [
        {"status": s, "jobtype": j, "resourcetype": r, "nworkers": n}
        for (s, j, r), n in sorted(_pivot.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return {
        "total_records": len(records),
        "nworkers_total": nworkers_total,
        "nworkers_by_status": nworkers_by_status,
        "nworkers_by_jobtype": nworkers_by_jobtype,
        "nworkers_by_resourcetype": nworkers_by_resourcetype,
        "nworkers_by_site": nworkers_by_site,
        "pivot": pivot,
        "from_dt": from_dt,
        "to_dt": to_dt,
        "site_filter": site_filter,
        "error": None,
    }


def _safe_int(value: Any) -> int | None:
    """Coerce *value* to ``int``, returning ``None`` on failure.

    Args:
        value: Raw value from an API record field.

    Returns:
        Integer value, or ``None`` if conversion is not possible.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Structured error constructors
# ---------------------------------------------------------------------------


def _error_evidence(
    from_dt: str,
    to_dt: str,
    site: str | None,
    detail: str,
) -> dict[str, Any]:
    """Return a structured evidence dict representing a fetch failure.

    The raw error detail is logged at DEBUG level but not exposed to the
    caller to avoid leaking internal information.

    Args:
        from_dt: Requested time-range lower bound.
        to_dt: Requested time-range upper bound.
        site: Requested site filter, or ``None``.
        detail: Internal error message for logging.

    Returns:
        Minimal evidence dict with ``error`` populated and all counters
        zeroed.
    """
    logger.debug("panda_harvester_workers error detail: %s", detail)
    return {
        "total_records": 0,
        "nworkers_total": 0,
        "nworkers_by_status": {},
        "nworkers_by_jobtype": {},
        "nworkers_by_resourcetype": {},
        "nworkers_by_site": {},
        "from_dt": from_dt,
        "to_dt": to_dt,
        "site_filter": site,
        "endpoint": None,
        "raw_payload": None,
        "error": (
            "Could not retrieve Harvester worker stats. "
            "The Harvester API may be temporarily unavailable — try again shortly."
        ),
    }


# ---------------------------------------------------------------------------
# MCP tool definition
# ---------------------------------------------------------------------------


def get_definition() -> dict[str, Any]:
    """Return the MCP tool definition for ``panda_harvester_workers``.

    Returns:
        Tool definition dict compatible with MCP discovery.
    """
    return {
        "name": "panda_harvester_workers",
        "description": (
            "Fetch Harvester worker (pilot) counts from BigPanDA for a given "
            "time window and optional computing site. "
            "Use this tool when the user asks about pilot counts, pilot status, "
            "how many pilots are running/idle/failed/submitted, or Harvester "
            "worker activity at an ATLAS site. "
            "Examples: 'How many pilots are running at BNL?', "
            "'Show pilot counts at CERN for the last 6 hours', "
            "'Were there Harvester workers yesterday at AGLT2?'. "
            "Worker counts are broken down by status, job type, resource type, "
            "and computing site."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Original user question (used by the LLM synthesiser).",
                },
                "site": {
                    "type": "string",
                    "description": (
                        "Optional computing site to filter by, e.g. 'BNL', 'CERN', "
                        "'AGLT2'.  Omit to retrieve stats across all sites."
                    ),
                },
                "from_dt": {
                    "type": "string",
                    "description": (
                        "ISO-8601 start of the time window, e.g. "
                        "'2026-03-01T00:00:00'.  Defaults to one hour ago (UTC) "
                        "when omitted."
                    ),
                },
                "to_dt": {
                    "type": "string",
                    "description": (
                        "ISO-8601 end of the time window, e.g. "
                        "'2026-03-01T06:00:00'.  Defaults to now (UTC) when omitted."
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


class PandaHarvesterWorkersTool:
    """MCP tool that fetches Harvester pilot/worker counts from BigPanDA.

    Queries the ``/harvester/getworkerstats/`` endpoint for a given time
    window and optional site filter, aggregates worker counts by status /
    job type / resource type / site, and returns a compact evidence dict
    structured for LLM synthesis.
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
        """Fetch Harvester worker stats and return evidence as MCP content.

        Resolves the time window (defaulting to the last hour when
        ``from_dt`` / ``to_dt`` are absent), wraps the blocking HTTP
        fetch in ``asyncio.to_thread``, and returns a JSON-serialised
        evidence dict.

        ``bamboo.tools.base`` is imported here (deferred) so the rest
        of this module remains importable when bamboo core is not
        installed.

        Args:
            arguments: Dict with optional ``"question"`` (str), ``"site"``
                (str), ``"from_dt"`` (str ISO-8601), and ``"to_dt"``
                (str ISO-8601).

        Returns:
            One-element MCP content list containing the JSON-serialised
            evidence dict, or an error payload if anything goes wrong.
        """
        from bamboo.tools.base import text_content  # deferred — see module docstring

        base_url: str = os.environ.get("PANDA_BASE_URL", "https://bigpanda.cern.ch")

        site: str | None = arguments.get("site") or None
        if site:
            site = site.strip() or None

        # Resolve time window — default to the last hour when not provided.
        from_dt_raw: str | None = arguments.get("from_dt")
        to_dt_raw: str | None = arguments.get("to_dt")

        default_from, default_to = _default_window()

        if from_dt_raw and to_dt_raw:
            from_dt = _resolve_dt(from_dt_raw.strip(), default_from)
            to_dt = _resolve_dt(to_dt_raw.strip(), default_to)
        else:
            from_dt, to_dt = default_from, default_to
            if from_dt_raw:
                from_dt = _resolve_dt(from_dt_raw.strip(), default_from)
            if to_dt_raw:
                to_dt = _resolve_dt(to_dt_raw.strip(), default_to)

        logger.debug(
            "panda_harvester_workers: site=%r from_dt=%r to_dt=%r",
            site, from_dt, to_dt,
        )

        try:
            evidence = await asyncio.to_thread(
                fetch_worker_stats, base_url, from_dt, to_dt, site
            )
            return text_content(json.dumps({"evidence": evidence}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("panda_harvester_workers tool call failed")
            error_ev = _error_evidence(from_dt, to_dt, site, detail=repr(exc))
            return text_content(json.dumps({"evidence": error_ev}))


panda_harvester_workers_tool = PandaHarvesterWorkersTool()


__all__ = [
    "PandaHarvesterWorkersTool",
    "_resolve_dt",
    "build_harvester_url",
    "fetch_worker_stats",
    "get_definition",
    "panda_harvester_workers_tool",
]
