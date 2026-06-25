"""Schema constants and LLM prompt builder for the ``panda_job_stats`` tool.

This module has **zero bamboo-core dependency** so it can be imported freely
by tests, stubs, and the tool implementation alike.

It defines:

* :data:`INDEX_PATTERN` — the OpenSearch index to query.
* :data:`JOB_STATS_FIELDS` — confirmed field registry for the current index
  schema (all confirmed fields: core identifiers, timing, I/O, errors, task
  context, software environment, CPU/HS06, memory, carbon, and infrastructure).
* :data:`VALID_METRICS` — permitted OpenSearch aggregation types.
* :data:`KEYWORD_GROUP_BY_FIELDS` — keyword fields permitted as ``group_by``
  targets in terms aggregations.
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

#: OpenSearch index pattern for the PanDA job stats table.
INDEX_PATTERN: str = "atlas_panda_job_stats-*"

# ---------------------------------------------------------------------------
# Confirmed job stats fields (batch 1 + batch 2)
# ---------------------------------------------------------------------------

#: Ordered list of ``(field_name, os_type, unit, description)`` tuples for
#: every field confirmed present in the ``atlas_panda_job_stats-*`` index.
#: Extend this list as Sasha adds further field batches.
JOB_STATS_FIELDS: list[tuple[str, str, str, str]] = [
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
    # ── I/O and data transfer ─────────────────────────────────────────────
    (
        "ninputdatafiles", "integer", "—",
        "Number of input files staged in.",
    ),
    (
        "inputfilebytes", "long", "bytes",
        "Total input data size in bytes.",
    ),
    (
        "inputfiletype", "keyword", "—",
        "Input file type, e.g. EVNT, DAOD_PHYS.",
    ),
    (
        "noutputdatafiles", "integer", "—",
        "Number of output files produced.",
    ),
    (
        "outputfilebytes", "long", "bytes",
        "Total output data size in bytes.",
    ),
    (
        "totrbytes", "long", "bytes",
        "Total bytes read during execution.",
    ),
    (
        "totwbytes", "long", "bytes",
        "Total bytes written during execution.",
    ),
    (
        "raterbytes", "double", "bytes/s",
        "Average read throughput in bytes per second.",
    ),
    (
        "ratewbytes", "double", "bytes/s",
        "Average write throughput in bytes per second.",
    ),
    (
        "transfertype", "keyword", "—",
        "Transfer type, e.g. direct, fax.",
    ),
    # ── Errors ────────────────────────────────────────────────────────────
    (
        "piloterrorcode", "integer", "—",
        "Pilot error code; 0 = no error.",
    ),
    (
        "piloterrordiag", "keyword", "—",
        "Pilot error diagnostic message.",
    ),
    (
        "exeerrorcode", "integer", "—",
        "Payload execution error code.",
    ),
    (
        "exeerrordiag", "keyword", "—",
        "Payload execution diagnostic message.",
    ),
    (
        "ddmerrorcode", "integer", "—",
        "DDM / data management error code.",
    ),
    (
        "ddmerrordiag", "keyword", "—",
        "DDM error diagnostic message.",
    ),
    (
        "transexitcode", "integer", "—",
        "Transformation exit code.",
    ),
    (
        "jobdispatchererrorcode", "integer", "—",
        "Job dispatcher error code.",
    ),
    (
        "taskbuffererrorcode", "integer", "—",
        "Task buffer error code.",
    ),
    # ── Task and campaign context ──────────────────────────────────────────
    (
        "produsername", "keyword", "—",
        "Submitter username.",
    ),
    (
        "prodsourcelabel", "keyword", "—",
        "Production source label: 'user' or 'managed'.",
    ),
    (
        "task_status", "keyword", "—",
        "Parent task status.",
    ),
    (
        "task_type", "keyword", "—",
        "Task type, e.g. prod, anal.",
    ),
    (
        "task_category", "keyword", "—",
        "Task category, e.g. 'production', 'user analysis'.",
    ),
    (
        "task_campaign", "keyword", "—",
        "Campaign label, e.g. 'MC16:MC16e'.",
    ),
    (
        "task_framework", "keyword", "—",
        "Task framework, e.g. easyjet, Athena.",
    ),
    (
        "task_transuses", "keyword", "—",
        "ATLAS release used by the task.",
    ),
    (
        "task_nattempts", "integer", "—",
        "Total job attempts across the task.",
    ),
    (
        "task_name", "keyword", "—",
        "Full task name string.",
    ),
    (
        "task_username", "keyword", "—",
        "Task owner username.",
    ),
    (
        "task_site", "keyword", "—",
        "Task site constraint.",
    ),
    (
        "task_cloud", "keyword", "—",
        "Task cloud constraint.",
    ),
    (
        "task_workinggroup", "keyword", "—",
        "Working group, e.g. AP_TOPQ.",
    ),
    (
        "task_errordialog", "keyword", "—",
        "Task-level error dialog.",
    ),
    (
        "task_starttime", "date", "UTC",
        "Task start time (ISO-8601 string).",
    ),
    (
        "task_creationdate", "date", "UTC",
        "Task creation date (ISO-8601 string).",
    ),
    (
        "task_endtime", "date", "UTC",
        "Task end time (ISO-8601 string; null if still running).",
    ),
    (
        "task_modificationtime", "date", "UTC",
        "Last task modification time (ISO-8601 string).",
    ),
    # ── Software environment ───────────────────────────────────────────────
    (
        "atlasrelease", "keyword", "—",
        "ATLAS software release, e.g. Atlas-21.0.129.",
    ),
    (
        "cmtconfig", "keyword", "—",
        "Build configuration, e.g. x86_64-centos7-gcc62-opt.",
    ),
    (
        "homepackage", "keyword", "—",
        "Home package, e.g. Athena/21.0.129.",
    ),
    # ── Carbon footprint (may be null for non-terminal jobs) ───────────────
    (
        "gco2global", "double", "g CO2",
        "Global-average CO2 footprint in grams. Currently null for many jobs.",
    ),
    (
        "gco2regional", "double", "g CO2",
        "Regional CO2 footprint in grams. Currently null for many jobs.",
    ),
    # ── CPU and HS06 accounting ────────────────────────────────────────────
    (
        "cpuconsumptiontime", "long", "s",
        "Raw CPU seconds consumed.",
    ),
    (
        "cpuconsumptionunit", "keyword", "—",
        "Processor description string.",
    ),
    (
        "hs06sec", "long", "HS06·s",
        "HS06-normalised CPU (HS06 * walltime). May be null for non-terminal jobs.",
    ),
    (
        "hs06", "double", "—",
        "HS06 benchmark factor for the slot.",
    ),
    (
        "corecount", "integer", "—",
        "Number of cores requested.",
    ),
    (
        "actualcorecount", "double", "—",
        "Actual core usage (may be fractional).",
    ),
    (
        "cpu_eff", "double", "%",
        "CPU efficiency percentage (cpuconsumptiontime / (job_walltime * corecount) * 100).",
    ),
    # ── Memory ────────────────────────────────────────────────────────────
    (
        "avgrss", "long", "kB",
        "Average resident set size in kilobytes.",
    ),
    (
        "maxrss", "long", "kB",
        "Peak resident set size in kilobytes. Compare to minramcount.",
    ),
    (
        "avgpss", "long", "kB",
        "Average proportional set size in kilobytes.",
    ),
    (
        "maxpss", "long", "kB",
        "Peak proportional set size in kilobytes.",
    ),
    (
        "avgvmem", "long", "kB",
        "Average virtual memory in kilobytes.",
    ),
    (
        "maxvmem", "long", "kB",
        "Peak virtual memory in kilobytes.",
    ),
    (
        "avgswap", "long", "kB",
        "Average swap usage in kilobytes. Non-zero indicates memory pressure.",
    ),
    (
        "maxswap", "long", "kB",
        "Peak swap usage in kilobytes.",
    ),
    (
        "minramcount", "integer", "MB",
        "Minimum RAM requested at submission in megabytes.",
    ),
    # ── Infrastructure and pilot traceability ─────────────────────────────
    (
        "computingelement", "keyword", "—",
        "CE endpoint, e.g. grid1.oscer.ou.edu:9619.",
    ),
    (
        "schedulerid", "keyword", "—",
        "Harvester instance, e.g. harvester-CERN_central_A.",
    ),
    (
        "batchid", "keyword", "—",
        "Batch system job ID (keyword; format varies by site).",
    ),
    # ── Additional classification fields ──────────────────────────────────
    (
        "dst_experiment_site", "keyword", "—",
        "Destination experiment site.",
    ),
    (
        "tier", "keyword", "—",
        "Site tier: T1, T2, T3.",
    ),
    (
        "atlas_resource_type", "keyword", "—",
        "Resource type, e.g. GRID, CLOUD.",
    ),
    (
        "country", "keyword", "—",
        "Country / region code, e.g. US-CENT-SWPP.",
    ),
    (
        "source_table", "keyword", "—",
        "PanDA source table, e.g. JOBSACTIVE4.",
    ),
    (
        "table_priority", "integer", "—",
        "Ingestion priority.",
    ),
]

#: Set of field names that are numeric and valid aggregation targets.
NUMERIC_FIELDS: frozenset[str] = frozenset(
    name
    for name, os_type, _unit, _desc in JOB_STATS_FIELDS
    if os_type in ("integer", "long", "float", "double")
)

#: Set of all confirmed field names (numeric and non-numeric).
ALL_FIELD_NAMES: frozenset[str] = frozenset(
    name for name, _t, _u, _d in JOB_STATS_FIELDS
)

# ---------------------------------------------------------------------------
# Aggregation metrics
# ---------------------------------------------------------------------------

#: Permitted OpenSearch single-value metric aggregation types.
VALID_METRICS: frozenset[str] = frozenset({"avg", "sum", "min", "max", "value_count"})

#: Keyword fields permitted as ``group_by`` targets in terms aggregations.
#: Only fields with ``keyword`` mapping in OpenSearch may be used for bucketing.
KEYWORD_GROUP_BY_FIELDS: frozenset[str] = frozenset({
    "computingsite",
    "jobstatus",
    "tier",
    "task_campaign",
    "task_type",
    "task_workinggroup",
    "prodsourcelabel",
    "transfertype",
    "inputfiletype",
    "atlasrelease",
    "country",
    "atlas_resource_type",
})

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

#: Result cache TTL in seconds.  Job stats data is historical/stable so a
#: longer TTL than live pilot data is appropriate.
CACHE_TTL_SECS: float = 120.0

#: Cache key prefix to avoid collisions with other tools.
CACHE_PREFIX: str = "job_stats:"

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

#: Sentinel returned by the LLM when it cannot extract query parameters.
CANNOT_ANSWER_SENTINEL: str = "CANNOT_ANSWER"

#: Field context block injected into the LLM system prompt.
_FIELD_CONTEXT: str = "\n".join(
    f"  {name:<32} ({os_type}, {unit})  {desc}"
    for name, os_type, unit, desc in JOB_STATS_FIELDS
)

#: System prompt template for NL → query-parameter extraction.
_SYSTEM_TEMPLATE: str = """\
TODAY={current_utc_date}  NOW={current_utc_datetime}

You are a query-parameter extractor for an OpenSearch index that stores
PanDA job statistics (index: atlas_panda_job_stats-*).

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
                  Compute from TODAY/NOW above.
                  Omit when the user does not specify a time range.
  "to_dt"       : ISO-8601 upper bound on @timestamp.
                  Compute from TODAY/NOW above.
                  Omit when the user does not specify a time range.
  "group_by"    : field to bucket results by, e.g. "computingsite",
                  "jobstatus", "tier", "task_campaign".  Use when the
                  question asks "which site", "per site", "by site",
                  "breakdown by", "ranked by", etc.  Only keyword fields
                  may be used here.  Omit for global (single-value)
                  aggregations.
  "top_n"       : number of top buckets to return (integer, default 5,
                  max 20).  Only used when group_by is present.

Rules:
- If the user's question cannot be answered using the available fields and
  aggregations, respond with exactly: CANNOT_ANSWER
- Never include fields that are date/keyword-only in "field" — only numeric
  fields may be aggregated.  Numeric fields are:
  {numeric_fields}
- For "value_count" metric, "field" should be "pandaid" (count distinct jobs).
- For site filters use the value exactly as the user states it; the
  implementation will apply a wildcard search so partial names are fine.
- Memory fields (avgrss, maxrss, avgpss, maxpss, avgvmem, maxvmem, avgswap,
  maxswap) are in kilobytes (kB). minramcount is in megabytes (MB).
- CPU efficiency (cpu_eff) is a percentage. cpuconsumptiontime is in seconds.
- hs06sec may be null for non-terminal jobs (running, transferring).
- gco2global and gco2regional (CO2 footprint in grams) may be null.
- I/O byte fields (inputfilebytes, outputfilebytes, totrbytes, totwbytes) are
  in bytes; throughput fields (raterbytes, ratewbytes) are in bytes/second.
- DATE RULE: use TODAY={current_utc_date} and NOW={current_utc_datetime} for
  every date calculation. Do not use any other date.
  "today"       → from_dt: "{current_utc_date}T00:00:00",
                  to_dt:   "{current_utc_date}T23:59:59"
  "last hour"   → from_dt: "{one_hour_ago}",
                  to_dt:   "{current_utc_datetime}"
  "last 7 days" → from_dt: "{week_ago_date}T00:00:00",
                  to_dt:   "{current_utc_date}T23:59:59"
- Do not include null values — omit the key entirely when the value is absent.

Examples (with today = {current_utc_date}):

  "What is the average stage-in time at BNL today?"
  → {{"metric": "avg", "field": "pilottiming_stagein", "site": "BNL",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average wall-clock time for finished jobs at CERN today?"
  → {{"metric": "avg", "field": "job_walltime", "site": "CERN",
      "jobstatus": "finished",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average RSS memory usage at CERN today?"
  → {{"metric": "avg", "field": "avgrss", "site": "CERN",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the peak memory usage at BNL today?"
  → {{"metric": "avg", "field": "maxrss", "site": "BNL",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average CPU efficiency at IN2P3 today?"
  → {{"metric": "avg", "field": "cpu_eff", "site": "IN2P3",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the total HS06-seconds consumed at TRIUMF today?"
  → {{"metric": "sum", "field": "hs06sec", "site": "TRIUMF",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average input data volume per job at BNL today?"
  → {{"metric": "avg", "field": "inputfilebytes", "site": "BNL",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average write throughput at CERN today?"
  → {{"metric": "avg", "field": "ratewbytes", "site": "CERN",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "How many jobs ran for campaign MC16:MC16e today?"
  → {{"metric": "value_count", "field": "pandaid",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the total wall-clock time for finished jobs at CERN?"
  → {{"metric": "sum", "field": "job_walltime", "site": "CERN",
      "jobstatus": "finished"}}

  "How many jobs ran at IN2P3 today?"
  → {{"metric": "value_count", "field": "pandaid", "site": "IN2P3",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average stage-in time at BNL over the last 7 days?"
  → {{"metric": "avg", "field": "pilottiming_stagein", "site": "BNL",
      "from_dt": "{week_ago_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the minimum queue time for failed jobs?"
  → {{"metric": "min", "field": "job_queuetime", "jobstatus": "failed"}}

  "Which site has the highest peak memory usage today?"
  → {{"metric": "max", "field": "maxrss", "group_by": "computingsite",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average wall-clock time per site today?"
  → {{"metric": "avg", "field": "job_walltime", "group_by": "computingsite",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "Which site has the worst CPU efficiency today?"
  → {{"metric": "avg", "field": "cpu_eff", "group_by": "computingsite",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "What is the average stage-in time broken down by tier today?"
  → {{"metric": "avg", "field": "pilottiming_stagein", "group_by": "tier",
      "from_dt": "{current_utc_date}T00:00:00", "to_dt": "{current_utc_date}T23:59:59"}}

  "How many cores does each site have?"
  → CANNOT_ANSWER
"""


def build_query_prompt(question: str) -> list[dict[str, Any]]:
    """Build the LLM message list for query-parameter extraction.

    Injects the current UTC date and time into both the system prompt and the
    user message so the LLM can compute concrete ISO-8601 timestamps for
    relative time expressions such as "today", "last 7 days", or "last hour"
    without guessing the date.

    The user-message prefix uses an imperative anchor ("TODAY IS … USE THESE
    DATES ONLY.") that instruction-following models honour more reliably than
    prose date blocks buried in a long system prompt.

    Args:
        question: Natural-language question from the user.

    Returns:
        A list of ``{"role": str, "content": str}`` dicts suitable for
        passing to any Bamboo LLM provider.  The list always has two
        elements: a system message and a user message.
    """
    import datetime  # deferred — only needed at prompt-build time

    now_utc = datetime.datetime.now(tz=datetime.timezone.utc)
    current_utc_datetime = now_utc.strftime("%Y-%m-%dT%H:%M:%S")
    current_utc_date = now_utc.strftime("%Y-%m-%d")
    week_ago_date = (now_utc - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    one_hour_ago = (now_utc - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")

    numeric_list = ", ".join(sorted(NUMERIC_FIELDS))
    valid_metrics_list = ", ".join(sorted(VALID_METRICS))

    system_content = _SYSTEM_TEMPLATE.format(
        field_context=_FIELD_CONTEXT,
        numeric_fields=numeric_list,
        valid_metrics=valid_metrics_list,
        current_utc_datetime=current_utc_datetime,
        current_utc_date=current_utc_date,
        week_ago_date=week_ago_date,
        one_hour_ago=one_hour_ago,
    )
    user_content = (
        f"TODAY IS {current_utc_date}. "
        f"NOW IS {current_utc_datetime} UTC. "
        f"USE THESE DATES ONLY.\n\n"
        f"{question}"
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
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
    "JOB_STATS_FIELDS",
    "KEYWORD_GROUP_BY_FIELDS",
    "NUMERIC_FIELDS",
    "VALID_METRICS",
    "build_query_prompt",
]
