# Bamboo MCP tools reference

This directory contains one document per MCP tool exposed by the Bamboo server.

---

## Orchestration layer

These tools handle routing, planning, and synthesis. They are called by clients (including the TUI) rather than being called directly for data.

| Tool | Document | Description |
|---|---|---|
| `bamboo_answer` | [bamboo_answer.md](bamboo_answer.md) | Primary entry point — routes questions and returns synthesised answers. |
| `bamboo_plan` | [bamboo_plan.md](bamboo_plan.md) | LLM-backed planner that decomposes questions into tool call plans. |
| `bamboo_last_evidence` | [bamboo_last_evidence.md](bamboo_last_evidence.md) | Returns the evidence dict from the most recent data tool call. |

---

## Operational data tools

These tools fetch live PanDA data from BigPanDA, Harvester, OpenSearch, or local databases.

| Tool | Document | Description |
|---|---|---|
| `panda_log_analysis` | [panda_log_analysis.md](panda_log_analysis.md) | Job failure diagnosis — downloads pilot log, classifies failure, returns evidence. |
| `panda_job_status` | [panda_job_status.md](panda_job_status.md) | Status and metadata for a single PanDA job. |
| `panda_task_status` | [panda_task_status.md](panda_task_status.md) | Status, progress, and dataset info for a PanDA task. |
| `panda_harvester_workers` | [panda_harvester_workers.md](panda_harvester_workers.md) | Harvester pilot/worker counts from the BigPanDA API. |
| `panda_harvester_timeseries` | [panda_harvester_timeseries.md](panda_harvester_timeseries.md) | Per-bucket pilot counts from OpenSearch for time-series charts. |
| `panda_jobs_query` | [panda_jobs_query.md](panda_jobs_query.md) | Aggregate job statistics via natural-language SQL against the ingestion DB. |
| `cric_query` | [cric_query.md](cric_query.md) | Queue and site configuration via natural-language SQL against the CRIC DB. |
| `panda_server_health` | [panda_server_health.md](panda_server_health.md) | PanDA server liveness check. |

---

## Documentation retrieval tools

These tools search the PanDA/Bamboo documentation corpus for conceptual questions.

| Tool | Document | Description |
|---|---|---|
| `panda_doc_search` | [panda_doc_search.md](panda_doc_search.md) | Vector similarity search (ChromaDB). |
| `panda_doc_bm25` | [panda_doc_bm25.md](panda_doc_bm25.md) | BM25 keyword search over the same corpus. |

---

## Infrastructure tools

| Tool | Document | Description |
|---|---|---|
| `bamboo_health` | [bamboo_health.md](bamboo_health.md) | Bamboo server health — version, LLM config, integration flags. |
| `bamboo_llm_answer` | [bamboo_llm_answer.md](bamboo_llm_answer.md) | Direct LLM passthrough — no routing or data tools. |

---

## Stub tools (not production)

These tools exist in the codebase but are not connected to live data. They are superseded by the production tools listed above.

| Tool | Document | Superseded by |
|---|---|---|
| `panda_pilot_status` | [panda_pilot_status.md](panda_pilot_status.md) | `panda_harvester_workers` |
| `panda_queue_info` | [panda_queue_info.md](panda_queue_info.md) | `cric_query` |

---

## Plugin differences

`panda_log_analysis` and `panda_task_status` have separate implementations in the `askpanda_atlas` and `askpanda_epic` plugin packages. The analysis logic is identical; the differences are in monitor URL labels, cache modules, and tool tags. See each tool document for details.

All plugin tools are loaded via Python entry points. The MCP server overwrites `get_definition()["name"]` with the entry point key at load time — for example, `panda_harvester_timeseries` is registered as `atlas.harvester_timeseries` and must be called by that name.
