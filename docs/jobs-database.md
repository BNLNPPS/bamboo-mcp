# PanDA Jobs Database (`panda_jobs_query`)

This document describes the `panda_jobs_query` tool, which answers
natural-language questions about live PanDA job data by translating questions
into SQL queries and executing them against the local ingestion database.

---

## Overview

The ingestion agent (`askpanda-ingestion-agent`) periodically downloads job
data from BigPanDA and writes it to a local [DuckDB](https://duckdb.org) file
(`jobs.duckdb` by default).  `panda_jobs_query` reads that file and answers
questions like:

- *"How many jobs failed at BNL?"*
- *"What are the top errors at SWT2_CPB?"*
- *"How many jobs are in each status at AGLT2?"*
- *"Which queues have the most failed jobs right now?"*
- *"When was the database last updated?"*

The tool uses the configured LLM to generate a SQL query, validates it through
a strict AST-based guard, executes it read-only against the DuckDB file, and
returns the results.

---

## Data freshness

The ingestion agent queries:

```
https://bigpanda.cern.ch/jobs/?computingsite=<QUEUE>&json&hours=1
```

**The database reflects jobs active at each configured queue within
approximately the last hour.**  Jobs that left that window remain in the
database with their last-known state but no longer receive updates.

Queries with relative time phrases like *"today"* or *"yesterday"* may return
zero results unless the ingestion agent has been running long enough to
accumulate history, and unless the `hours` parameter in the agent config has
been widened (see [Widening the time window](#widening-the-time-window)).

To check data freshness:

```
When was the database last updated?
```

This generates:

```sql
SELECT _queue, MAX(_fetched_utc) AS last_fetched, COUNT(*) AS job_count
FROM jobs
GROUP BY _queue
ORDER BY last_fetched DESC
LIMIT 500
```

---

## Database schema

Three tables are exposed to the LLM.  All other tables (internal DuckDB
system tables, `information_schema`, etc.) are blocked by the guard.

### `jobs`

One row per PanDA job.  Rows are upserted on each ingestion cycle, so each
row always reflects the latest known state.

Key columns for natural-language queries:

| Column | Type | Description |
|---|---|---|
| `pandaid` | BIGINT | Unique job identifier (primary key) |
| `jobstatus` | VARCHAR | `defined`, `waiting`, `sent`, `starting`, `running`, `holding`, `merging`, `finished`, `failed`, `cancelled`, `closed` |
| `computingsite` | VARCHAR | Computing site / queue name (same as `_queue`) |
| `_queue` | VARCHAR | Which queue was polled by the ingestion agent |
| `_fetched_utc` | TIMESTAMP | When the ingestion agent last fetched this row |
| `jeditaskid` | BIGINT | JEDI task the job belongs to |
| `produserid` | VARCHAR | User DN or production role that submitted the job |
| `piloterrorcode` | INTEGER | Pilot error code (0 = no error) |
| `piloterrordiag` | VARCHAR | Pilot error diagnostic message |
| `exeerrorcode` | INTEGER | Payload execution error code (0 = no error) |
| `exeerrordiag` | VARCHAR | Payload execution diagnostic |
| `cpuefficiency` | DOUBLE | CPU efficiency ratio (0.0–1.0+) |
| `durationsec` | DOUBLE | Wall-clock run time in seconds |
| `statechangetime` | TIMESTAMP | Last status transition time |
| `creationtime` | TIMESTAMP | Job creation time |

### `selectionsummary`

Pre-aggregated facet counts from BigPanDA.  One row per facet field per
queue, replaced on every ingestion cycle.  Useful for fast counts without
scanning the full `jobs` table.

| Column | Description |
|---|---|
| `field` | Facet name (e.g. `jobstatus`, `cloud`, `gshare`) |
| `list_json` | JSON array of `{kname, kvalue}` — value and count pairs |
| `stats_json` | JSON aggregate stats (e.g. `{"sum": 9928}`) |
| `_queue` | Source queue |

### `errors_by_count`

Ranked error frequency table from BigPanDA.  One row per error code per
queue, replaced on every ingestion cycle.

| Column | Description |
|---|---|
| `error` | Error category: `pilot`, `exe`, `ddm`, `brokerage`, etc. |
| `codename` | Symbolic error name |
| `codeval` | Numeric error code |
| `count` | Number of jobs currently affected |
| `diag` | Diagnostic string |
| `example_pandaid` | A representative job with this error |

---

## Example questions and generated SQL

**How many jobs failed at BNL?**

```sql
SELECT COUNT(*) AS n
FROM jobs
WHERE _queue = 'BNL' AND jobstatus = 'failed'
LIMIT 500
```

**How many jobs are in each status at BNL?**

```sql
SELECT jobstatus, COUNT(*) AS n
FROM jobs
WHERE _queue = 'BNL'
GROUP BY jobstatus
ORDER BY n DESC
LIMIT 500
```

**What are the top errors at SWT2_CPB?**

```sql
SELECT error, codename, codeval, SUM(count) AS total_count, MIN(diag) AS diag
FROM errors_by_count
WHERE _queue ILIKE 'SWT2_CPB%'
GROUP BY error, codename, codeval
ORDER BY total_count DESC
LIMIT 10
```

> **Note:** `errors_by_count.count` is a per-queue count. A site may have
> multiple queues (e.g. `BNL_ATLAS_TIER1`, `BNL_ATLAS_TIER1-condor`), so
> always use `SUM(count) GROUP BY error, codename, codeval` when filtering
> by site with `ILIKE`. A bare `ORDER BY count` returns only the
> highest single-queue row, not the site total.

**Which queues have the most failed jobs?**

```sql
SELECT _queue, COUNT(*) AS failed_jobs
FROM jobs
WHERE jobstatus = 'failed'
GROUP BY _queue
ORDER BY failed_jobs DESC
LIMIT 500
```

**Which jobs are running at BNL?**

```sql
SELECT pandaid, produserid, durationsec, cpuefficiency
FROM jobs
WHERE _queue = 'BNL' AND jobstatus = 'running'
ORDER BY durationsec DESC
LIMIT 500
```

---

## Security: the AST guard

Every SQL string generated by the LLM passes through `validate_and_guard()`
in `jobs_query_schema.py` before it touches DuckDB.  The guard uses
[sqlglot](https://github.com/tobymao/sqlglot) to parse the SQL into an AST
and applies seven rules:

| Rule | What it blocks |
|---|---|
| Single statement | Stacked statements (`SELECT 1; DROP TABLE jobs`) |
| SELECT-only root | INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, GRANT, REVOKE, COMMIT, ROLLBACK |
| No forbidden nodes anywhere | DDL/DML/DCL/TCL at any depth of the AST, including subqueries |
| No system tables | `information_schema`, `pg_catalog`, `sqlite_master`, `duckdb_*`, `pg_*` |
| No system functions | `duckdb_tables()`, `duckdb_columns()`, and similar internal functions |
| Table allow-list | Any table not in `{jobs, selectionsummary, errors_by_count}` |
| LIMIT injection | Queries without a LIMIT clause get `LIMIT 500` injected |

The guard is AST-based, not regex-based.  Obfuscation tricks like `SE/**/LECT`
or mixed case do not bypass it because sqlglot normalises the AST before
inspection.

The DuckDB connection is always opened with `read_only=True` as a backstop.
Any write attempt raises a `duckdb.InvalidInputException` even if the guard
were somehow bypassed.

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `PANDA_DUCKDB_PATH` | `jobs.duckdb` | Path to the DuckDB file written by the ingestion agent |

Set this in `bamboo_env.sh`:

```bash
export PANDA_DUCKDB_PATH="/path/to/askpanda-atlas-agents/jobs.duckdb"
```

---

## Routing

`bamboo_answer` detects jobs DB questions via `_is_jobs_db_question()`, which
matches a set of signal phrases against the question text.  Matched questions
bypass the topic guard entirely (they are self-evidently on-topic) and route
directly to `panda_jobs_query` via `_build_deterministic_plan()`.

Signal phrases include (non-exhaustive): `"how many"`, `"failed at"`,
`"running at"`, `"top errors"`, `"each status"`, `"by status"`,
`"which queues"`, `"which sites"`, `"last updated"`, `"most failed"`.

Questions containing `"task"` are excluded — they route to `panda_task_status`
instead.  Contextual follow-ups that reference a task ID in history (e.g.
*"how many of those jobs failed?"*) also correctly route to
`panda_task_status` because contextual ID resolution runs before the jobs DB
intercept.

---

## Multi-database disambiguation (future)

The `QUERYABLE_DATABASES` dict in `bamboo_answer.py` is a registry of all
queryable databases.  Currently it contains only `"jobs"`.

When a second database (e.g. CRIC) is added, ambiguous questions — ones that
match jobs DB signal phrases but contain no database-specific keyword — will
trigger a clarification response:

> *"I can query multiple databases.  Which one did you mean?  Please rephrase
> mentioning the database name."*

To activate disambiguation, uncomment the `"cric"` entry in
`QUERYABLE_DATABASES` and `_DB_KEYWORDS` in `bamboo_answer.py`.  No other
changes are required.

---

## Widening the time window

The ingestion agent's `hours` parameter controls how far back the BigPanDA
API returns jobs.  The default is `hours=1`.

To enable reliable time-based queries (*"how many jobs failed today?"*),
increase this to `hours=24` in the agent configuration
(`ingestion-agent.yaml`).  This increases payload size and ingestion time but
gives the `statechangetime` column sufficient history for day-level filtering:

```sql
-- Jobs that failed at BNL today
SELECT COUNT(*) AS n
FROM jobs
WHERE _queue = 'BNL'
  AND jobstatus = 'failed'
  AND DATE(statechangetime) = CURRENT_DATE
LIMIT 500
```

For long-running deployments, the `jobs` table grows unboundedly (rows are
upserted but never deleted).  A periodic archival step (moving old rows to a
`jobs_history` table or a Parquet file) is recommended for production use.
