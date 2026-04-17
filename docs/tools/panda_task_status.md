# `panda_task_status`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.task_status_atlas`
**Type:** Operational data — task metadata and progress

---

## Purpose

`panda_task_status` fetches the current status, progress, and metadata of a PanDA task by its task ID (`jeditaskid`). Use it for questions about a task as a whole: its completion rate, dataset breakdown, error counts, or the list of jobs it contains.

Typical questions:
- "What is the status of task 48432100?"
- "How many jobs failed in task 28191852?"
- "What datasets does task 32000001 process?"
- "Is task 48432100 done?"

For questions about a single specific job within a task, use [`panda_job_status`](panda_job_status.md) or [`panda_log_analysis`](panda_log_analysis.md) instead.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `task_id` | integer | Yes | PanDA task ID (`jeditaskid`). |
| `query` | string | No | Original user query (passed to the LLM synthesiser). |
| `include_jobs` | boolean | No (default `true`) | Include individual job records in the response (`?jobs=1`). |
| `timeout` | integer | No (default `30`) | HTTP timeout in seconds. |

---

## Data source

Fetches from BigPanDA:

```
GET https://bigpanda.cern.ch/task/<task_id>/?json[&jobs=1]
```

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `task_id` | The queried task ID. |
| `monitor_url` | BigPanDA monitor URL for this task. |
| `fetched_url` | Exact URL fetched (includes `?jobs=1` if requested). |
| `http_status` | HTTP response status code. |
| `status` | Task status (e.g. `done`, `running`, `failed`, `broken`). |
| `superstatus` | High-level status grouping. |
| `taskname` | Full task name string. |
| `username` | Owner of the task. |
| `creationdate` / `starttime` / `endtime` | Task lifecycle timestamps. |
| `dsinfo` | Dataset information dict from BigPanDA. |
| `datasets_summary` | Compact summary: input/output dataset names and sizes. |
| `job_counts` | Dict of job counts by status (e.g. `{"finished": 450, "failed": 12}`). |
| `payload` | Full raw BigPanDA API response (excluded from LLM evidence to save tokens). |

---

## Synthesis prompt

The LLM synthesis uses `_SYSTEM_TASK`, which instructs the model to answer the user's specific question using only data present in the evidence — no hallucination of missing fields. The prompt handles the `not_found` case and encourages compact, fact-based answers.

---

## Routing

`bamboo_answer` routes to this tool deterministically when a task ID is extracted from the question, with no job ID present. Task IDs are identified by patterns like `"task 48432100"`, `"task:48432100"`, or `"task-48432100"`.

---

## See also

- [`panda_job_status`](panda_job_status.md) — individual job status
- [`panda_log_analysis`](panda_log_analysis.md) — failure diagnosis for a specific job
- [`panda_jobs_query`](panda_jobs_query.md) — aggregate job statistics across sites
