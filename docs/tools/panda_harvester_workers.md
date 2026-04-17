# `panda_harvester_workers`

**Package:** `askpanda_atlas`
**Module:** `askpanda_atlas.harvester_worker_impl`
**Type:** Operational data — Harvester pilot/worker counts

---

## Purpose

`panda_harvester_workers` fetches current Harvester worker (pilot) counts from the BigPanDA Harvester API. It answers questions about how many pilots are running, idle, failed, or submitted at a given site and time window.

"Workers" and "pilots" are synonymous in this context — both refer to Harvester worker processes that submit and manage PanDA jobs at computing sites.

Typical questions:
- "How many pilots are running at BNL?"
- "Show pilot counts at CERN for the last 6 hours."
- "How many MCORE pilots are running at BNL since yesterday?"
- "Were there Harvester workers at AGLT2 last week?"

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | Yes | Original user question (used by the LLM synthesiser). |
| `site` | string | No | Computing site to filter by, e.g. `"BNL"`, `"CERN"`, `"AGLT2"`. Accepts both short names and full queue names. |
| `from_dt` | string | No | ISO-8601 start of the time window (UTC). Default: one hour ago. |
| `to_dt` | string | No | ISO-8601 end of the time window (UTC). Default: now. |

When `from_dt` and `to_dt` are omitted, the tool defaults to the last hour.

---

## Data source

Calls the BigPanDA Harvester API:

```
GET <base_url>/harvester/getworkerstats/
    ?lastupdate_from=<from_dt>
    &lastupdate_to=<to_dt>
    [&computingsite=<site>]
```

Results are cached for 30 seconds (short TTL because pilot counts change frequently).

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `from_dt` / `to_dt` | Actual time window queried. |
| `site_filter` | Site filter applied, or `null` for all sites. |
| `nworkers_total` | Total worker count across all statuses. |
| `nworkers_by_status` | Dict of counts by status, e.g. `{"running": 412, "idle": 88, "failed": 3}`. |
| `nworkers_by_resourcetype` | Dict of counts by resource type (e.g. `{"MCORE": 210, "SCORE": 290}`). |
| `nworkers_by_site` | Dict of counts by computing site. |
| `pivot_rows` | List of `{status, jobtype, resourcetype, nworkers}` dicts for the full breakdown. |
| `error` | Error string if the fetch failed, otherwise absent. |
| `raw_payload` | Full Harvester API response (excluded from LLM evidence). |

---

## Routing

`bamboo_answer` routes to this tool via the pilot fast-path, which triggers on signal phrases in `_PILOT_SIGNALS`:

- Status-qualified phrases: `"pilots running"`, `"running pilots"`, `"pilots failed"`, `"pilots idle"`, etc.
- Count phrases: `"how many pilots"`, `"pilot count"`, `"pilot activity"`, `"pilot stats"`
- Resource-type phrases: `"mcore pilots"`, `"score pilots"`, `"hcore pilots"`
- Site phrases: `"pilots at"`, `"pilots for"`
- Harvester terminology: `"harvester worker"`, `"nworkers"`

Time window extraction (`_extract_time_window_from_question`) runs automatically and injects `from_dt`/`to_dt` into the tool call when the question mentions a time range ("last 6 hours", "since yesterday", "past 3 days").

---

## Auto-chart

When the TUI receives a response from `panda_harvester_workers`, it attempts to auto-render an ASCII bar chart via `_try_auto_chart`. The `/chart` command can also trigger a chart explicitly. Charts are powered by the `atlas.harvester_timeseries` tool, which queries OpenSearch for per-bucket time-series data.

---

## See also

- [`panda_harvester_timeseries`](panda_harvester_timeseries.md) — per-bucket time-series data for ASCII charts
- [`panda_jobs_query`](panda_jobs_query.md) — job-level statistics from the ingestion database
- [`panda_server_health`](panda_server_health.md) — PanDA server liveness
