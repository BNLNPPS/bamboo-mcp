# `panda_harvester_timeseries`

**Package:** `askpanda_atlas`
**Module:** `askpanda_atlas.harvester_timeseries_impl`
**Entry point name:** `atlas.harvester_timeseries`
**Type:** Operational data — pilot count time series (OpenSearch)

---

## Purpose

`panda_harvester_timeseries` fetches per-bucket Harvester pilot counts from the OpenSearch `atlas_harvesterworkers-*` index for a single worker status over a time window. It is used primarily by the TUI's ASCII chart feature (`/chart` command and auto-chart) to visualise how pilot counts changed over time.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | No | Original user question (context only). |
| `status` | string | No (default `"running"`) | Harvester worker status to chart: `running`, `submitted`, `finished`, `failed`, `cancelled`, `missed`, `idle`. |
| `from_dt` | string | No (default: one hour ago) | ISO-8601 start of the window (UTC). |
| `to_dt` | string | No (default: now) | ISO-8601 end of the window (UTC). |
| `site` | string | No | Computing site to filter by. |

---

## Data source

Queries the OpenSearch cluster at `ASKPANDA_OPENSEARCH_HOST` (default: `https://os-atlas.cern.ch/os`) using the index pattern `atlas_harvesterworkers-*`.

**Required environment variable:** `ASKPANDA_OPENSEARCH` — the HTTP Basic Auth password. The tool returns a graceful error if this is absent.

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `ASKPANDA_OPENSEARCH_HOST` | `https://os-atlas.cern.ch/os` | OpenSearch base URL. |
| `ASKPANDA_OPENSEARCH_USER` | `pilot-monitor-agent` | HTTP auth username. |
| `ASKPANDA_OPENSEARCH_CA` | `/etc/pki/tls/certs/CERN-bundle.pem` | CA certificate bundle path. |
| `ASKPANDA_OPENSEARCH_VERIFY_CERTS` | `"true"` | Set to `"false"` to disable TLS verification (local dev). |

**Requires CERN VPN** to reach `os-atlas.cern.ch`.

---

## Bucket interval

The bucket width is derived automatically from the window duration to produce approximately 12–20 buckets:

| Window | Interval |
|---|---|
| ≤ 30 min | 2 min |
| ≤ 2 h | 10 min |
| ≤ 12 h | 30 min |
| ≤ 3 days | 2 h |
| > 3 days | 6 h |

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `status` | The worker status queried. |
| `from_dt` / `to_dt` | Time window used. |
| `site` | Site filter applied, or `null`. |
| `interval` | OpenSearch bucket interval used (e.g. `"30m"`). |
| `buckets` | List of `{timestamp, count}` dicts in chronological order. |
| `total` | Total pilot count across all buckets. |
| `error` | Error string if the query failed, otherwise absent. |

---

## Entry point naming

The MCP server overwrites `get_definition()["name"]` with the entry point key when loading plugin tools. This tool must therefore always be called as `atlas.harvester_timeseries`, not `panda_harvester_timeseries`. This naming convention applies to all plugin tools loaded via entry points.

---

## Key design note

The field name in the OpenSearch index is `computingsite.keyword` (lowercase), not `computingSite.keyword`. This distinction matters when constructing filters manually.

---

## See also

- [`panda_harvester_workers`](panda_harvester_workers.md) — aggregate pilot counts (BigPanDA API, no OpenSearch required)
