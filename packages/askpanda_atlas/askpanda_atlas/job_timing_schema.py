"""Schema constants and LLM prompt builder for the ``panda_job_timing`` tool.

This module has **zero bamboo-core dependency** so it can be imported freely
by tests, stubs, and the tool implementation alike.

It defines:

* :data:`INDEX_PATTERN` — the OpenSearch index to query.
* :data:`TIMING_FIELDS` — confirmed field registry for the current index
  schema (batch 1: core identifiers + timing).
* :data:`VALID_METRICS` — permitted OpenSearch aggregation types.
* :data:`DEFAULT_WINDOW_HOURS` — default look-back window when the caller
  omits ``from_dt`` / ``to_dt``.
* :data:`CACHE_TTL_SECS` — result cache TTL.
* :func:`build_query_prompt` — build the LLM message list for query-parameter
  extraction.
* :data:`CANNOT_ANSWER_SENTINEL` — sentinel returned by the LLM when it
  cannot extract query parameters.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

#: OpenSearch index pattern for the PanDA job timing table.
INDEX_PATTERN: str = "atlas_panda_job_timing-*"

# ---------------------------------------------------------------------------
# Confirmed timing fields (batch 1)
# ---------------------------------------------------------------------------

#: Ordered list of ``(field_name, os_type, unit, description)`` tuples for
#: every field confirmed present in the ``atlas_panda_job_timing-*`` index.
#: Extend this list as Sasha adds further field batches.
TIMING_FIELDS: list[tuple[str, str, str, str]] = [
    # ── Core identifiers / status ──────────────────────────────────────────
    (
        "pandaid", "long", "—",
        "Unique PanDA job identifier (primary key).",
    ),
    (
        "jobstatus", "keyword", "—",
        "Job state: finished / failed / cancelled / closed / running / etc.",
    ),
    (
        "computingsite", "keyword", "—",
        "Computing site / queue name, e.g. 'BNL_ATLAS_1'.",
    ),
    (
        "jeditaskid", "long", "—",
        "JEDI task ID the job belongs to.",
    ),
    (
        "taskid", "long", "—",
        "PanDA task ID.",
    ),
    (
        "attemptnr", "integer", "—",
        "Attempt number. Values > 1 indicate retried jobs.",
    ),
    (
        "statechangetime", "date", "UTC",
        "UTC timestamp of the last job status transition (also mapped to @timestamp).",
    ),
    (
        "creationtime", "date", "UTC",
        "UTC timestamp when the job was created / submitted.",
    ),
    # ── Timing ────────────────────────────────────────────────────────────
    (
        "starttime", "date", "UTC",
        "UTC timestamp when job execution started on the worker node.",
    ),
    (
        "endtime", "date", "UTC",
        "UTC timestamp when job execution finished.",
    ),
    (
        "job_walltime", "integer", "s",
        "Pre-computed wall-clock execution time in seconds (endtime − starttime).",
    ),
    (
        "job_queuetime", "integer", "s",
        "Pre-computed queue wait time in seconds (starttime − creationtime).",
    ),
    (
        "pilottiming_getjob", "integer", "s",
        "Parsed from pilottiming[0]: time for the getJob curl call to complete.",
    ),
    (
        "pilottiming_stagein", "integer", "s",
        "Parsed from pilottiming[1]: total stage-in time including replica lookup.",
    ),
    (
        "pilottiming_payload", "integer", "s",
        "Parsed from pilottiming[2]: payload execution time including pre/post-processing.",
    ),
    (
        "pilottiming_stageout", "integer", "s",
        "Parsed from pilottiming[3]: total stage-out time including log transfer.",
    ),
    (
        "pilottiming_initial_setup", "integer", "s",
        "Parsed from pilottiming[4]: pilot startup to getJob — proxy check, queue data download.",
    ),
    (
        "pilottiming_payload_setup", "integer", "s",
        "Parsed from pilottiming[5]: time before to after payload setup script execution.",
    ),
]

#: Set of field names that are numeric and valid aggregation targets.
NUMERIC_FIELDS: frozenset[str] = frozenset(
    name
    for name, os_type, _unit, _desc in TIMING_FIELDS
    if os_type in ("integer", "long", "float", "double")
)

#: Set of all confirmed field names (numeric and non-numeric).
ALL_FIELD_NAMES: frozenset[str] = frozenset(
    name for name, _t, _u, _d in TIMING_FIELDS
)

# ---------------------------------------------------------------------------
# Aggregation metrics
# ---------------------------------------------------------------------------

#: Permitted OpenSearch single-value metric aggregation types.
VALID_METRICS: frozenset[str] = frozenset({"avg", "sum", "min", "max", "value_count"})

#: Default metric when the caller does not specify one.
DEFAULT_METRIC: str = "avg"

#: Default field to aggregate when the caller does not specify one.
DEFAULT_FIELD: str = "job_walltime"

# ---------------------------------------------------------------------------
# Time window defaults
# ---------------------------------------------------------------------------

#: Default look-back window in hours when ``from_dt`` / ``to_dt`` are absent.
DEFAULT_WINDOW_HOURS: int = 24

# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------

#: Result cache TTL in seconds.  Job timing data is historical/stable so a
#: longer TTL than live pilot data is appropriate.
CACHE_TTL_SECS: float = 120.0

#: Cache key prefix to avoid collisions with other tools.
CACHE_PREFIX: str = "job_timing:"

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

#: Sentinel returned by the LLM when it cannot extract query parameters.
CANNOT_ANSWER_SENTINEL: str = "CANNOT_ANSWER"

#: Field context block injected into the LLM system prompt.
_FIELD_CONTEXT: str = "\n".join(
    f"  {name:<32} ({os_type}, {unit})  {desc}"
    for name, os_type, unit, desc in TIMING_FIELDS
)

#: System prompt template for NL → query-parameter extraction.
_SYSTEM_TEMPLATE: str = """\
You are a query-parameter extractor for an OpenSearch index that stores
PanDA job timing data (index: atlas_panda_job_timing-*).

Current UTC date and time: {current_utc_datetime}
Today's date (UTC):        {current_utc_date}

Available fields:
{field_context}

Valid aggregation metrics: avg, sum, min, max, value_count
Default metric: avg
Default field:  job_walltime

Your task: extract structured query parameters from the user's question.

Respond with ONLY a JSON object — no explanation, no markdown fences — with
these keys (all optional except none are required if you cannot answer):

  "metric"      : one of {valid_metrics}   (default: "avg")
  "field"       : field name to aggregate  (default: "job_walltime")
                  Must be one of the numeric fields listed above.
  "site"        : computingsite keyword filter, e.g. "BNL_ATLAS_1"
                  Use the shortest unambiguous prefix or full name.
                  Omit when no site is mentioned.
  "jobstatus"   : jobstatus keyword filter, e.g. "finished", "failed"
                  Omit when no job status is mentioned.
  "jeditaskid"  : integer JEDI task ID filter.  Omit when not mentioned.
  "from_dt"     : ISO-8601 lower bound on @timestamp (= statechangetime).
                  Compute from the current UTC date/time above.
                  Omit when the user does not specify a time range.
  "to_dt"       : ISO-8601 upper bound on @timestamp.
                  Compute from the current UTC date/time above.
                  Omit when the user does not specify a time range.

Rules:
- If the user's question cannot be answered using the available fields and
  aggregations, respond with exactly: CANNOT_ANSWER
- Never include fields that are date/keyword-only in "field" — only numeric
  fields may be aggregated.  Numeric fields are:
  {numeric_fields}
- For "value_count" metric, "field" should be "pandaid" (count distinct jobs).
- For site filters use the value exactly as the user states it; the
  implementation will apply a wildcard search so partial names are fine.
- IMPORTANT: all date/time values must be computed from the current UTC
  date/time shown above.  Never invent or guess dates.
  "today"     → from_dt: "{current_utc_date}T00:00:00", to_dt: "{current_utc_date}T23:59:59"
  "last hour" → from_dt: subtract 1 hour from {current_utc_datetime}
  "last 7 days" → from_dt: subtract 7 days from {current_utc_date}T00:00:00
- Do not include null values — omit the key entirely when the value is absent.

Examples (with today = {current_utc_date}):

  "What is the average stage-in time at BNL today?"
  → {{"metric": "avg", "field": "pilottiming_stagein", "site": "BNL",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average stage-in time at BNL over the last 7 days?"
  → {{"metric": "avg", "field": "pilottiming_stagein", "site": "BNL",
      "from_dt": "{week_ago_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the total wall-clock time for finished jobs at CERN?"
  → {{"metric": "sum", "field": "job_walltime", "site": "CERN",
      "jobstatus": "finished"}}

  "How many jobs ran at IN2P3 today?"
  → {{"metric": "value_count", "field": "pandaid", "site": "IN2P3",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the minimum queue time for failed jobs?"
  → {{"metric": "min", "field": "job_queuetime", "jobstatus": "failed"}}

  "What is the average payload setup time globally?"
  → {{"metric": "avg", "field": "pilottiming_payload_setup"}}

  "How many cores does each site have?"
  → CANNOT_ANSWER
"""


def build_query_prompt(question: str) -> list[dict[str, Any]]:
    """Build the LLM message list for query-parameter extraction.

    Injects the current UTC date and time into the system prompt so the LLM
    can compute concrete ISO-8601 timestamps for relative time expressions
    such as "today", "last 7 days", or "last hour" without guessing the date.

    Args:
        question: Natural-language question from the user.

    Returns:
        A list of ``{"role": str, "content": str}`` dicts suitable for
        passing to any Bamboo LLM provider.
    """
    import datetime  # deferred — only needed at prompt-build time

    now_utc = datetime.datetime.now(tz=datetime.timezone.utc)
    current_utc_datetime = now_utc.strftime("%Y-%m-%dT%H:%M:%S")
    current_utc_date = now_utc.strftime("%Y-%m-%d")
    week_ago_date = (now_utc - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

    numeric_list = ", ".join(sorted(NUMERIC_FIELDS))
    valid_metrics_list = ", ".join(sorted(VALID_METRICS))

    system_content = _SYSTEM_TEMPLATE.format(
        field_context=_FIELD_CONTEXT,
        numeric_fields=numeric_list,
        valid_metrics=valid_metrics_list,
        current_utc_datetime=current_utc_datetime,
        current_utc_date=current_utc_date,
        week_ago_date=week_ago_date,
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": question},
    ]


__all__ = [
    "ALL_FIELD_NAMES",
    "CACHE_PREFIX",
    "CACHE_TTL_SECS",
    "CANNOT_ANSWER_SENTINEL",
    "DEFAULT_FIELD",
    "DEFAULT_METRIC",
    "DEFAULT_WINDOW_HOURS",
    "INDEX_PATTERN",
    "NUMERIC_FIELDS",
    "TIMING_FIELDS",
    "VALID_METRICS",
    "build_query_prompt",
]
