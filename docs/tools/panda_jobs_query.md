# `panda_jobs_query`

**Package:** `askpanda_atlas`
**Module:** `askpanda_atlas.jobs_query_impl`
**Type:** Operational data — aggregate job statistics (DuckDB)

---

## Purpose

`panda_jobs_query` answers natural-language questions about PanDA jobs by translating them into SQL and querying a local DuckDB ingestion database. It is optimised for aggregate questions across many jobs at one or more sites — counts, error breakdowns, status distributions — rather than questions about a single job.

Typical questions:
- "How many jobs failed at BNL in the last hour?"
- "What are the top errors at SWT2_CPB?"
- "Which jobs are running at CERN right now?"
- "Show me the error breakdown for jobs at AGLT2."

For a specific job's status or failure log, use [`panda_job_status`](panda_job_status.md) or [`panda_log_analysis`](panda_log_analysis.md) instead.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | Yes | Natural-language question about PanDA jobs. |

---

## Data source

A local DuckDB file populated by the Bamboo ingestion agent, containing jobs active at each computing site within approximately the last hour. The database path is configured by the `JOBS_DB_PATH` environment variable.

The database reflects a periodic snapshot, not a live query to BigPanDA. The synthesis response includes a "Database last updated" footnote showing the snapshot timestamp.

---

## SQL generation

The tool uses an LLM call (temperature `0.0`) to translate the natural-language question into a SQL `SELECT` statement against the `jobs` table. The query is validated through an AST guard (`jobs_query_schema`) before execution:

- Only `SELECT` statements are permitted (no `INSERT`, `UPDATE`, `DELETE`, `DROP`).
- Aggregation queries (`GROUP BY`) use a higher row limit (`MAX_ROWS_AGGREGATION = 500`).
- Plain `SELECT` queries are capped at `MAX_ROWS = 50` rows.

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `question` | The original user question. |
| `sql` | The SQL query generated and executed. |
| `columns` | List of column names in the result. |
| `rows` | List of result rows (each row is a list of values). |
| `row_count` | Number of rows returned. |
| `db_last_modified` | Snapshot timestamp of the database file (UTC string). |
| `error` | Error string if the query failed, otherwise absent. |

---

## Routing

`bamboo_answer` routes to this tool via the jobs DB fast-path, which fires on signal phrases such as `"jobs failed"`, `"error breakdown"`, `"job count"`, `"jobs at"`, and similar patterns. The routing is checked after the pilot fast-path, so questions mentioning both pilots and jobs go through the combined site-health path instead.

---

## Key design notes

- DuckDB queries run synchronously on the event loop thread (not via `asyncio.to_thread`) to avoid macOS thread-pool conflicts with the DuckDB connection.
- The SQL AST guard uses `sqlglot.parse()` to validate query structure before execution. Queries that do not parse as a single `SELECT` statement are rejected.
- The LLM call and DuckDB execution are both within `call()` — the LLM generates SQL, the DB executes it, and the result is serialised into the evidence dict in one round-trip.

---

## See also

- [`cric_query`](cric_query.md) — queue and site configuration questions (separate CRIC database)
- [`panda_job_status`](panda_job_status.md) — metadata for a single specific job
- [`panda_harvester_workers`](panda_harvester_workers.md) — pilot/worker counts from the Harvester API
