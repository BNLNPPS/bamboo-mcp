# `panda_log_analysis`

**Package:** `askpanda_atlas`, `askpanda_epic`
**Modules:** `askpanda_atlas.log_analysis_impl`, `askpanda_epic.log_analysis_impl`
**Type:** Operational data — job failure diagnosis

---

## Purpose

`panda_log_analysis` diagnoses why a specific PanDA job failed. It fetches job metadata and the pilot log from the PanDA monitor, extracts the most relevant failure context from the log, classifies the failure type, and returns structured evidence for LLM synthesis.

This is the primary tool for questions like:
- "Why did job 7099498577 fail?"
- "What error caused job 6837798305 to fail?"
- "Analyse the failure of job 7100840246."

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `job_id` | integer | Yes | PanDA job ID (`pandaid`) to analyse. |
| `query` | string | No | Original user query (passed to the LLM synthesiser). |
| `context` | string | No | Optional additional context (site, task ID, release). |

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `job_id` | The queried job ID. |
| `monitor_url` | BigPanDA / PanDA monitor URL for this job. |
| `jobstatus` | Final job status (`failed`, `holding`, `cancelled`, etc.). |
| `jobsubstatus` | Sub-status if available. |
| `computingsite` | Site where the job ran. |
| `cloud` | Cloud region. |
| `atlasrelease` | ATLAS software release (key name is from the PanDA API; present in both ATLAS and ePIC). |
| `jeditaskid` | Parent task ID. |
| `attemptnr` / `maxattempt` | Retry attempt number and maximum allowed. |
| `transformation` | Payload transformation script (e.g. `POOLtoEI_tf.py`). |
| `piloterrorcode` / `piloterrordiag` | Numeric pilot error code and diagnostic string. |
| `exeerrorcode` / `exeerrordiag` | Execution error code and diagnostic string. |
| `taskbuffererrorcode` / `taskbuffererrordiag` | Task buffer error code and diagnostic string. |
| `ddmerrorcode` / `ddmerrordiag` | DDM (data management) error code and diagnostic string. |
| `starttime` / `endtime` / `duration` | Job timing information. |
| `failure_type` | Classified failure category (see below). |
| `log_url` | URL to fetch the pilot log (may not be available immediately after job completion). |
| `log_available` | Whether the log was successfully downloaded. |
| `log_excerpt` | Most relevant section of the pilot log, extracted by pattern matching. |
| `links_md` | Pre-built Markdown links block appended verbatim after LLM synthesis. |

---

## Failure classification

The tool classifies failures into categories using pattern matching against error fields and log content:

| Category | Signals |
|---|---|
| `looping_job` | "looping job", "no recently updated files" |
| `stagein_timeout` | "stage-in timeout", "ddm", `ddmerrorcode != 0` |
| `memory_exceeded` | "memory exceeded", "oom", cgroup kill |
| `segfault` | "segfault", "signal 11", "core dump" |
| `payload_failure` | `piloterrorcode == 1305`, `payload.stdout` |
| `cvmfs_timeout` | "cvmfs", "remote file open timed out" |
| `network_error` | "connection refused", "timeout", network keywords |
| `jedi_reassignment` | "reassigned by jedi", `commandtopilot` reassignment |
| `unknown` | No pattern matched |

---

## Log extraction

Logs are fetched only for jobs in `failed`, `holding`, or `cancelled` states. The log file selected depends on `piloterrorcode`:

- **Code 1305** (payload failure): `payload.stdout` is fetched.
- **All other codes**: `pilotlog.txt` is fetched.

The extraction uses a pattern matched to the pilot error code to find the relevant context window (± 30 lines around the first match). If no pattern matches, the last 60 lines of the log are used as fallback.

---

## Links

The `links_md` evidence field contains a Markdown links block constructed from programmatic URLs — not from LLM text — so the URLs are always correct. `bamboo_executor` strips any LLM-invented links section and appends this block verbatim to the synthesised answer. The TUI then rewrites `[label](url)` entries to `label — url` so both the label and the URL are visible in the terminal.

Example output after synthesis:

```
Links:
- BigPanDA Monitor — https://bigpanda.cern.ch/job?pandaid=7099498577
- Pilot Log — https://bigpanda.cern.ch/filebrowser/?pandaid=7099498577&json&filename=pilotlog.txt
```

---

## Plugin differences

| Aspect | `askpanda_atlas` | `askpanda_epic` |
|---|---|---|
| Monitor label | `BigPanDA Monitor` | `PanDA Monitor` |
| Cache module | `askpanda_atlas._cache` | `askpanda_epic._cache` |
| Tool tags | includes `"bigpanda"`, `"atlas"` | includes `"epic"`, `"eic"` |

The analysis logic (log extraction, failure classification, evidence structure) is identical in both packages.

---

## Routing

`bamboo_answer` routes to this tool deterministically when a job ID and failure-analysis keywords are both present in the question. The routing uses `_is_log_analysis_request`, which matches patterns like "analyse", "analyze", "why", "fail", "log", "diagnos" near a job ID.

---

## See also

- [`panda_job_status`](panda_job_status.md) — job status without log analysis (for status/metadata questions)
- [`bamboo_last_evidence`](bamboo_last_evidence.md) — inspect the raw evidence dict via `/inspect` or `/json`
