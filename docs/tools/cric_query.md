# `cric_query`

**Package:** `askpanda_atlas`
**Module:** `askpanda_atlas.cric_query_impl`
**Type:** Operational data — queue and site configuration (DuckDB)

---

## Purpose

`cric_query` answers natural-language questions about ATLAS computing queues and site configuration by translating them into SQL and querying a local DuckDB snapshot of the CRIC (Computing Resource Information Catalogue) database.

Typical questions:
- "Which queues are using the rucio copytool?"
- "Is the BNL queue online?"
- "What is the status of all queues at CERN?"
- "Which sites have more than 1000 running jobs?"
- "Which MCORE queues are online at BNL?"
- "List all queues in brokeroff status."

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | Yes | Natural-language question about ATLAS queues or site configuration. |

---

## Data source

A local DuckDB file at the path configured by `CRIC_DB_PATH` (default: not set; must be provided). The database contains the `queuedata` table with the following key columns:

| Column | Type | Description |
|---|---|---|
| `queue` | VARCHAR | Queue name (primary identifier). |
| `status` | VARCHAR | Queue status: `online`, `offline`, `test`, `brokeroff`. |
| `atlas_site` | VARCHAR | ATLAS site name. |
| `copytools` | JSON array | List of copytools configured for the queue. |
| `acopytools` | JSON array | List of alternative copytools. |
| `resource_type` | VARCHAR | Resource type, e.g. `MCORE`, `SCORE`, `HCORE`. |

The database reflects the latest CRIC snapshot fetched by the ingestion agent. The synthesis response includes a "Database last updated" footnote.

---

## SQL generation

An LLM call (temperature `0.0`) translates the question into a SQL `SELECT` against the `queuedata` table. The query is validated by the AST guard (`cric_query_schema`) before execution:

- Only `SELECT` statements are permitted.
- Plain queries are capped at `MAX_ROWS = 50` rows.
- Aggregation queries (`GROUP BY`) use `MAX_ROWS_AGGREGATION = 500`.

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `question` | The original user question. |
| `sql` | The SQL query generated and executed. |
| `columns` | List of column names in the result. |
| `rows` | List of result rows. |
| `row_count` | Number of rows returned. |
| `db_last_modified` | Snapshot timestamp of the CRIC database file. |
| `error` | Error string if the query failed, otherwise absent. |

---

## Large result bypass

When `cric_query` returns a large number of individual queue rows (e.g. "list all queues at CERN"), LLM synthesis would produce a truncated or unhelpful response. In this case `bamboo_executor` detects the large result and formats the table directly, writing it to a temp file and returning a short sentinel `__CRIC_TABLE_READY__:<N>` to the TUI. The TUI fetches the formatted table via a second `bamboo_last_evidence(mode="table")` call, bypassing the LLM entirely for that response.

---

## Routing

`bamboo_answer` routes to this tool when the question contains CRIC-specific signal phrases: queue names, copytool keywords, CRIC-related terminology, or site configuration vocabulary. The routing shares a disambiguation path with `panda_jobs_query` — questions that could match either are sent to whichever DB is more appropriate based on the question content.

---

## Key design notes

- DuckDB queries run synchronously on the event loop thread (same as `panda_jobs_query`) to avoid macOS thread-pool issues.
- `copytools` and `acopytools` columns are JSON arrays stored as strings; the schema includes guidance for the LLM to use `json_extract` or `like '%value%'` patterns.
- Status values are lowercase: `online`, `offline`, `test`, `brokeroff`.

---

## See also

- [`panda_jobs_query`](panda_jobs_query.md) — aggregate job statistics (separate jobs database)
- [`panda_harvester_workers`](panda_harvester_workers.md) — live pilot counts at sites
