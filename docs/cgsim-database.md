# CGSim Simulation Database (`cgsim.sim_query`)

This document describes the `cgsim.sim_query` tool, which answers
natural-language questions about a CGSim simulation run by translating
questions into SQL and executing them against the local simulation output
SQLite database.

---

## Overview

[CGSim](https://simgrid.org/) simulates the lifecycle of jobs on a distributed
computing grid.  Every event — job allocation, execution, file transfer, disk
read and write — is recorded to a **SQLite database**.  `cgsim.sim_query` reads
that database and answers questions like:

- *"How long did job J-001 take to execute?"*
- *"Which site had the most jobs allocated to it?"*
- *"Why did job J-002 spend so long queuing?"*
- *"Were any file transfers affected by network congestion?"*
- *"Which disk was the I/O bottleneck?"*
- *"What was the average execution time per site?"*
- *"Did jobs retry frequently?"*

The tool uses the configured LLM to generate a SQL query (LLM call 1),
validates it through a strict AST-based guard, executes it read-only against
the SQLite file, then uses the LLM again to summarise the results in natural
language (LLM call 2).

---

## Data model

### Table: `EVENTS`

The entire simulation history lives in one table.

| Column | Type | Description |
|---|---|---|
| `_ID` | INTEGER | Auto-assigned row ID. Not meaningful for analysis. |
| `EVENT` | TEXT | Activity type: `JobAllocation` \| `JobExecution` \| `FileTransfer` \| `FileRead` \| `FileWrite` |
| `STATE` | TEXT | Lifecycle stage: `Started` \| `Finished` |
| `STATUS` | TEXT | Job status at row-write time: `pending` \| `assigned` \| `running` \| `finished` |
| `JOB_ID` | TEXT | Unique job identifier — groups all rows for a single job. |
| `TIME` | REAL | Simulation clock timestamp (seconds). **Not the authoritative duration source** — see below. |
| `METADATA` | TEXT | JSON object. Shape depends on `(EVENT, STATE)`. |

### Key invariants

**Paired rows.** Every activity produces exactly one `Started` row and one
`Finished` row, both sharing the same `JOB_ID`.

**Duration.** The authoritative elapsed time for any activity is the `duration`
field in the **Finished** row's `METADATA`.  Do not compute
`Finished.TIME − Started.TIME` — internal queuing means this may differ from
the true active duration.

**Units.** All `TIME` and `duration` values are in **seconds** (simulation
clock).  File sizes are in **bytes**.  Speeds are in **FLOP/s** or **bytes/s**.

**Utilisation fields.** `site_cpu_util`, `grid_cpu_util`, `site_storage_util`,
and `grid_storage_util` are snapshots in `[0.0, 1.0]` taken at row-write time.
They are not averages; they correlate load with job timing.

**`cost` field excluded.** The `cost` field in `JobExecution/Finished` is a
placeholder that is not yet calibrated.  The tool explicitly excludes it from
all generated SQL.

---

### METADATA fields by event type

> All numeric units are base SI: seconds, bytes, FLOP, FLOP/s, bytes/s.

#### `JobAllocation`

| Field | Started | Finished | Description |
|---|:-:|:-:|---|
| `site` | ✓ | ✓ | Site where the job was allocated. |
| `host` | ✓ | ✓ | Host machine selected. |
| `site_storage_util` | | ✓ | Storage utilisation at site (fraction). |
| `grid_storage_util` | | ✓ | Storage utilisation across the grid (fraction). |
| `site_cpu_util` | | ✓ | CPU utilisation at site (fraction). |
| `grid_cpu_util` | | ✓ | CPU utilisation across the grid (fraction). |

#### `JobExecution`

| Field | Started | Finished | Description |
|---|:-:|:-:|---|
| `flops` | ✓ | ✓ | Total FLOP required / performed. |
| `site` | ✓ | ✓ | Execution site. |
| `host` | ✓ | ✓ | Execution host. |
| `cores` | ✓ | ✓ | CPU cores allocated. |
| `speed` | ✓ | ✓ | FLOP/s per core (SimGrid model). `duration = flops / (speed × cores)`. |
| `site_cpu_util` | ✓ | ✓ | CPU utilisation at site (fraction). |
| `grid_cpu_util` | ✓ | ✓ | CPU utilisation across the grid (fraction). |
| `duration` | | ✓ | **Authoritative compute time (seconds).** |
| `retries` | | ✓ | 0 = succeeded on first attempt. |
| `total_io_read_time` | | ✓ | Sum of all `FileRead` durations for this job (seconds). |
| `file_transfer_queue_time` | | ✓ | Wait for remote files to arrive (seconds). |
| `resource_waiting_queue_time` | | ✓ | Wait for a free compute slot (seconds). |
| `total_queue_time` | | ✓ | `file_transfer_queue_time + resource_waiting_queue_time` (seconds). |

#### `FileTransfer`

| Field | Started | Finished | Description |
|---|:-:|:-:|---|
| `file` | ✓ | ✓ | File name. |
| `size` | ✓ | ✓ | File size (bytes). |
| `source_site` | ✓ | ✓ | Sending site. |
| `destination_site` | ✓ | ✓ | Receiving site (typically the execution site). |
| `bandwidth` | ✓ | ✓ | Link maximum capacity (bytes/s). |
| `latency` | ✓ | ✓ | One-way latency (seconds). |
| `link_load` | ✓ | ✓ | Fraction of link bandwidth currently in use. High = congestion. |
| `site_storage_util` | ✓ | ✓ | Storage utilisation at destination site (fraction). |
| `grid_storage_util` | ✓ | ✓ | Storage utilisation across the grid (fraction). |
| `duration` | | ✓ | **Authoritative transfer time (seconds).** |

#### `FileRead`

| Field | Started | Finished | Description |
|---|:-:|:-:|---|
| `file` | ✓ | ✓ | File name. |
| `size` | ✓ | ✓ | File size (bytes). |
| `site` | ✓ | ✓ | Site where the read occurred. |
| `host` | ✓ | ✓ | Host performing the read. |
| `disk` | ✓ | ✓ | Disk device identifier (e.g. `sda`, `nvme0n1`). |
| `disk_read_bw` | ✓ | ✓ | Maximum read bandwidth of the device (bytes/s). |
| `duration` | | ✓ | **Authoritative read time (seconds).** |

#### `FileWrite`

| Field | Started | Finished | Description |
|---|:-:|:-:|---|
| `file` | ✓ | ✓ | File name. |
| `size` | ✓ | ✓ | File size (bytes). |
| `site` | ✓ | ✓ | Site where the write occurred. |
| `host` | ✓ | ✓ | Host performing the write. |
| `disk` | ✓ | ✓ | Disk device identifier. |
| `disk_write_bw` | ✓ | ✓ | Maximum write bandwidth of the device (bytes/s). |
| `site_storage_util` | ✓ | ✓ | Storage utilisation at site (fraction). |
| `grid_storage_util` | ✓ | ✓ | Storage utilisation across the grid (fraction). |
| `duration` | | ✓ | **Authoritative write time (seconds).** |

---

### Total wall-clock time

A job's total perceived wall-clock time is the sum of four components, all
available on the `JobExecution/Finished` row:

```
resource_waiting_queue_time   (waiting for a free compute slot)
+ file_transfer_queue_time    (waiting for remote files)
+ total_io_read_time          (reading input files from disk)
+ duration                    (active compute time)
= total wall-clock time
```

`total_queue_time` is a convenience field equal to
`file_transfer_queue_time + resource_waiting_queue_time`.

---

## Example questions and generated SQL

**How long did job J-001 take to execute?**

```sql
SELECT json_extract(METADATA, '$.duration') AS execution_duration_s
FROM EVENTS
WHERE JOB_ID = 'J-001' AND EVENT = 'JobExecution' AND STATE = 'Finished'
LIMIT 1
```

**What was the total wall-clock time for job J-001?**

```sql
SELECT
    json_extract(METADATA, '$.duration')                    AS compute_s,
    json_extract(METADATA, '$.total_queue_time')            AS queue_s,
    json_extract(METADATA, '$.total_io_read_time')          AS io_read_s,
    json_extract(METADATA, '$.duration')
    + json_extract(METADATA, '$.total_queue_time')
    + json_extract(METADATA, '$.total_io_read_time')        AS total_wall_clock_s
FROM EVENTS
WHERE JOB_ID = 'J-001' AND EVENT = 'JobExecution' AND STATE = 'Finished'
LIMIT 1
```

**Why did job J-001 spend so long queuing?**

```sql
SELECT
    json_extract(METADATA, '$.file_transfer_queue_time')      AS file_transfer_wait_s,
    json_extract(METADATA, '$.resource_waiting_queue_time')   AS resource_wait_s,
    json_extract(METADATA, '$.total_queue_time')              AS total_queue_s
FROM EVENTS
WHERE JOB_ID = 'J-001' AND EVENT = 'JobExecution' AND STATE = 'Finished'
LIMIT 1
```

**Which site had the most jobs allocated to it?**

```sql
SELECT json_extract(METADATA, '$.site') AS site, COUNT(*) AS job_count
FROM EVENTS
WHERE EVENT = 'JobAllocation' AND STATE = 'Finished'
GROUP BY site
ORDER BY job_count DESC
LIMIT 200
```

**Which file transfers were affected by network congestion?**

```sql
SELECT JOB_ID,
       json_extract(METADATA, '$.link_load')        AS link_load,
       json_extract(METADATA, '$.source_site')      AS source,
       json_extract(METADATA, '$.destination_site') AS dest
FROM EVENTS
WHERE EVENT = 'FileTransfer' AND STATE = 'Started'
  AND json_extract(METADATA, '$.link_load') > 0.8
ORDER BY link_load DESC
LIMIT 200
```

**Average execution time per site?**

```sql
SELECT json_extract(METADATA, '$.site')                    AS site,
       AVG(json_extract(METADATA, '$.duration'))            AS avg_duration_s,
       COUNT(*)                                             AS job_count
FROM EVENTS
WHERE EVENT = 'JobExecution' AND STATE = 'Finished'
GROUP BY site
ORDER BY avg_duration_s DESC
LIMIT 200
```

**Did jobs retry frequently?**

```sql
SELECT retries, COUNT(*) AS n
FROM (
    SELECT json_extract(METADATA, '$.retries') AS retries
    FROM EVENTS
    WHERE EVENT = 'JobExecution' AND STATE = 'Finished'
)
GROUP BY retries
ORDER BY n DESC
LIMIT 200
```

**Which disk was the I/O bottleneck?**

```sql
SELECT json_extract(METADATA, '$.disk')                                         AS disk,
       AVG(CAST(json_extract(METADATA, '$.size') AS REAL)
           / json_extract(METADATA, '$.duration'))                               AS avg_throughput_bytes_per_s,
       json_extract(METADATA, '$.disk_read_bw')                                  AS max_bw_bytes_per_s,
       COUNT(*)                                                                   AS n
FROM EVENTS
WHERE EVENT = 'FileRead' AND STATE = 'Finished'
GROUP BY disk, max_bw_bytes_per_s
ORDER BY avg_throughput_bytes_per_s ASC
LIMIT 200
```

---

## Security: the AST guard

Every SQL string generated by the LLM passes through `validate_and_guard()`
in `sim_query_schema.py` before it touches the database.  The guard uses
[sqlglot](https://github.com/tobymao/sqlglot) to parse the SQL into an AST
(SQLite dialect) and applies seven rules:

| Rule | What it blocks |
|---|---|
| Parse success | Malformed SQL — any input sqlglot cannot parse |
| Single statement | Stacked statements (`SELECT 1; DROP TABLE EVENTS`) |
| SELECT-only root | INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, GRANT, REVOKE, COMMIT, ROLLBACK at the top level |
| No forbidden nodes anywhere | All DDL/DML/DCL/TCL at any depth of the AST, including subqueries |
| No system tables | `sqlite_master`, `sqlite_sequence`, `information_schema`, `sqlite_*`-prefixed tables |
| Table allow-list | Any table not in `{events}` |
| LIMIT injection | Queries without a LIMIT get `LIMIT 200` injected (raw); aggregation queries (`GROUP BY`) get `LIMIT 1000` |

The guard is AST-based, not regex-based.  Obfuscation tricks such as mixed
case, comment injection, or whitespace padding do not bypass it because
sqlglot normalises the AST before inspection.

In addition to the AST guard, the database connection enforces two independent
read-only layers before any SQL is executed:

1. **SQLite URI `?mode=ro`** — the SQLite driver refuses any write at the OS level.
2. **`PRAGMA query_only = ON`** — a second enforcement inside the SQLite library.

These four layers (URI flag, PRAGMA, AST guard, allow-list) are independent:
a bypass of the AST guard still cannot write to the file.

---

## Pipeline: two LLM calls

```
User question
      │
      ▼
LLM call 1 (temperature 0.0, max 512 tokens)
  System prompt: schema context + SQL rules + 8 example patterns
  Output: SQL SELECT statement (or CANNOT_ANSWER sentinel)
      │
      ▼
  fence stripping → cannot-answer detection → AST guard
      │
      ▼
  SQLite execute (read-only, synchronous on event loop thread)
      │
      ▼
LLM call 2 (temperature 0.2, max 1024 tokens)
  System prompt: original question + SQL used + raw results + unit context
  Output: natural-language summary
      │
      ▼
Evidence dict returned (includes both raw rows and the summary)
```

LLM call 2 (summarisation) is non-fatal.  If it fails, the evidence dict
is still returned with the raw query results — only the `summary` field will
be `null`.

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `CGSIM_DB_PATH` | `cgsim.db` | Path to the CGSim SQLite database file. |

Set this in `bamboo_env.sh` before starting the server:

```bash
export CGSIM_DB_PATH="/path/to/simulation/cgsim.db"
```

The tool returns a descriptive error (not a crash) if the file is absent.

---

## The CGSim reader library

The `askcgsim` package vendors `cgsim_reader.py` — a standalone Python module
with no third-party dependencies that provides typed structured access to the
EVENTS table.  Its `CGSimReader` class and `EventRow` dataclass are used by
other AskCGSim tools; `cgsim.sim_query` queries the database directly via
`sqlite3` for maximum flexibility in SQL generation.

See the module docstring in `askcgsim/cgsim_reader.py` for the full API.

---

## See also

- [`cgsim.doc_search`](tools/cgsim_doc_search.md) — vector search over CGSim / SimGrid documentation
- [`cgsim.doc_bm25`](tools/cgsim_doc_bm25.md) — BM25 keyword search over the same corpus
- [`docs/plugins.md`](plugins.md) — plugin architecture and entry point conventions
