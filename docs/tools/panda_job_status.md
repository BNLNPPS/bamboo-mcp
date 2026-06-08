# `panda_job_status`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.job_status`
**Type:** Operational data — individual job metadata

---

## Purpose

`panda_job_status` fetches status and metadata for a specific PanDA job by its job ID. Use it for questions about a single job's current state, execution site, pilot errors, timing, or file summary — without triggering full log download and failure analysis.

Typical questions:
- "What is the status of job 6837798305?"
- "Where did job 7100840246 run?"
- "What pilot error code did job 6799893074 get?"

For failure diagnosis (downloading the pilot log, classifying the error, suggesting next steps), use [`panda_log_analysis`](panda_log_analysis.md) instead.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `job_id` | integer | Yes | PanDA job ID (`pandaid`). |
| `query` | string | No | Original user query (passed to the LLM synthesiser). |

---

## Data source

Fetches from the `panda` MCP server's `download_bigpanda_metadata` tool, which calls:

```
GET https://bigpanda.cern.ch/job?pandaid=<job_id>&json
```

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `job_id` | The queried job ID. |
| `monitor_url` | BigPanDA monitor URL for this job. |
| `jobstatus` | Current status (e.g. `finished`, `failed`, `running`). |
| `jobsubstatus` | Sub-status if available. |
| `computingsite` | Site where the job ran or is running. |
| `cloud` | Cloud region. |
| `queue` | Queue name. |
| `piloterrorcode` / `piloterrordiag` | Pilot error code and diagnostic string. |
| `exeerrorcode` / `exeerrordiag` | Execution error code and diagnostic. |
| `starttime` / `endtime` | Job timing. |
| `duration` | Wall-clock duration in seconds. |
| `files_summary` | Summary of input/output/log files by type and status. |
| `not_found` | `true` if the job ID was not found in BigPanDA. |

---

## Synthesis prompt

The LLM synthesis uses `_SYSTEM_JOB`, which instructs the model to:
- Report the job not found if `evidence.not_found` is true.
- Summarise status, site, queue, pilot error, and timing.
- Include the BigPanDA monitor URL as plain text (not a Markdown hyperlink).

---

## Routing

`bamboo_answer` routes to this tool deterministically when a job ID is present and no failure-analysis keywords are detected. If failure keywords are present, `panda_log_analysis` takes priority.

---

## See also

- [`panda_log_analysis`](panda_log_analysis.md) — full failure diagnosis including pilot log download
- [`panda_task_status`](panda_task_status.md) — task-level status covering all jobs in a task
