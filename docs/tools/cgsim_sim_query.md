# `cgsim.sim_query`

**Package:** `askcgsim`
**Entry point:** `cgsim.sim_query`
**Module:** `askcgsim.sim_query_impl`
**Type:** Simulation data — natural-language to SQL (SQLite)

---

## Purpose

`cgsim.sim_query` answers natural-language questions about a CGSim simulation
run by translating them into SQL, executing the query read-only against the
simulation output SQLite database, and summarising the results in natural
language.

Use it for questions about *simulation results* — timings, resource usage,
site behaviour, network congestion, retry rates, I/O bottlenecks.  For
questions about *how CGSim or SimGrid works*, use
[`cgsim.doc_search`](cgsim_doc_search.md) or [`cgsim.doc_bm25`](cgsim_doc_bm25.md)
instead.

Typical questions:

- "How long did job J-001 take to execute?"
- "Which site had the most jobs allocated to it?"
- "Why did job J-002 spend so long queuing?"
- "Were any file transfers affected by network congestion?"
- "Which disk was the I/O bottleneck?"
- "What was the average execution time per site?"
- "Did jobs retry frequently?"
- "Was the grid under heavy load when job J-001 ran?"

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | Yes | Natural-language question about the simulation (max 2000 characters). |

---

## Data source

A local SQLite database produced by a CGSim simulation run.  The path is
configured by the `CGSIM_DB_PATH` environment variable (default: `cgsim.db`).

The database contains a single table, `EVENTS`, with one row per simulation
event.  Every job activity (allocation, execution, file transfer, disk read,
disk write) produces a `Started` row and a `Finished` row.  The `METADATA`
column holds a JSON object whose shape depends on the `(EVENT, STATE)` pair.

See [`docs/cgsim-database.md`](../cgsim-database.md) for the full schema
reference, field descriptions, and example queries.

---

## Pipeline

The tool makes **two LLM calls** per question:

1. **SQL generation** (temperature `0.0`, max 512 tokens) — the LLM translates
   the question into a single `SELECT` statement using a system prompt that
   includes the full EVENTS schema, `json_extract()` guidance, explicit
   constraints (no `cost` field, no `TIME`-difference durations), and eight
   worked example patterns.

2. **Result summarisation** (temperature `0.2`, max 1024 tokens) — the LLM
   receives the original question, the executed SQL, and the raw query results
   as JSON, and returns a natural-language summary with correct units (seconds,
   bytes, FLOP/s, fractions as percentages).

LLM call 2 is non-fatal: if it fails, the raw evidence dict is still returned
with `summary: null`.

---

## SQL guard

The generated SQL passes through `validate_and_guard()` in
`askcgsim/sim_query_schema.py` before execution.  Seven rules are enforced:

| Rule | What it blocks |
|---|---|
| Parse success | Malformed SQL |
| Single statement | Stacked statements |
| SELECT-only root | INSERT, UPDATE, DELETE, DROP, CREATE, etc. |
| No forbidden constructs anywhere | DDL/DML/DCL/TCL at any AST depth |
| No system tables | `sqlite_master`, `sqlite_sequence`, `information_schema`, `sqlite_*` |
| Table allow-list | Any table other than `events` |
| LIMIT injection | Queries without LIMIT get `LIMIT 200`; aggregations get `LIMIT 1000` |

The SQLite connection enforces two additional read-only layers independently of
the AST guard: `file:path?mode=ro` URI and `PRAGMA query_only = ON`.

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `question` | The original user question. |
| `sql` | The sanitised SQL query that was executed. |
| `columns` | List of column names in the result. |
| `rows` | List of result rows (each row is a dict of column→value). |
| `row_count` | Number of rows returned. |
| `truncated` | `true` if the result was capped at `MAX_ROWS`. |
| `execution_time_ms` | SQLite query execution time in milliseconds. |
| `db_path` | Path to the database file that was queried. |
| `summary` | Natural-language summary from LLM call 2, or `null` if summarisation failed. |
| `error` | User-safe error string, or `null` on success. |
| `guard_rejection` | Guard rule that rejected the SQL, or `null` if the query passed. |

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `CGSIM_DB_PATH` | `cgsim.db` | Path to the CGSim SQLite simulation output file. |

---

## Key design notes

- **Synchronous SQLite execution** — SQLite queries run on the event loop thread
  (no `asyncio.to_thread`), consistent with the DuckDB precedent in
  `panda_jobs_query`.  CGSim databases are local files and queries complete in
  < 20 ms.
- **sqlglot `sqlite` dialect** — SQL is parsed with the `sqlite` dialect for
  correct AST construction, then rendered without a dialect specifier so that
  `JSON_EXTRACT` is preserved in its canonical form (not transformed to the
  SQLite `->` operator, which some Python builds do not support).
- **Deferred imports** — all `bamboo.llm.*` and `bamboo.tools.base` imports are
  inside `call()` and helper functions, keeping the module importable without
  bamboo core installed.
- **Non-raising `call()`** — every failure mode (LLM error, guard rejection,
  missing file, execution error) returns a structured evidence dict rather than
  raising an exception.

---

## See also

- [`cgsim.doc_search`](cgsim_doc_search.md) — vector search over CGSim / SimGrid docs
- [`cgsim.doc_bm25`](cgsim_doc_bm25.md) — BM25 keyword search over the same corpus
- [`docs/cgsim-database.md`](../cgsim-database.md) — full EVENTS schema, field reference, and example SQL
