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
| `log_url` | URL to fetch the primary log file (may not be available immediately after job completion). |
| `stderr_url` | URL to fetch `payload.stderr` (populated only for code 1305 jobs; `null` otherwise). |
| `log_available` | Whether at least one log file was successfully downloaded. |
| `log_excerpt` | Most relevant section of the log, extracted by pattern matching. For code 1305 jobs this combines stdout and stderr content (separated by `--- payload.stderr ---`). |
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

Logs are fetched only for jobs in `failed`, `holding`, or `cancelled` states.

### File selection

| Condition | Files fetched |
|---|---|
| `piloterrorcode == 1305` (payload failure) | `payload.stdout` (primary) + `payload.stderr` (appended if available, separated by `--- payload.stderr ---`) |
| All other codes | `pilotlog.txt` only |

The stderr fetch for code 1305 is intentional: Python tracebacks, C++ exceptions, and segfaults frequently appear only on stderr and would be missed if only stdout were examined.

### Context window extraction for `pilotlog.txt`

This is a three-level priority cascade:

**Level 1 — hardcoded pattern for known codes (`_PILOT_CODE_PATTERNS`)**

Eight pilot error codes have a hardcoded regex pattern that is known to appear near the failure in the log:

| Code | Pattern used |
|---|---|
| 1099 | `"Failed to stage-in file"` |
| 1104 | `r"work directory .* is too large"` |
| 1150 | `"pilot has decided to kill looping job"` |
| 1151 | `"File transfer timed out"` |
| 1201 | `"caught signal"` |
| 1235 | `"job has exceeded the memory limit"` |
| 1324 | `"Service not available"` |

When a match is found, the 40 lines immediately preceding (and including) the matching line are returned. The pilot writes thousands of lines; without anchoring, the relevant 40 lines would be buried.

**Level 2 — `piloterrordiag` as a fallback pattern (the scalability mechanism)**

For the 100+ pilot error codes not in `_PILOT_CODE_PATTERNS`, the tool uses the first 40 characters of `piloterrordiag` (regex-escaped) as the search pattern. This works because the pilot generates `piloterrordiag` directly from the log message it just wrote — the same text that becomes the diagnostic string was written to the log moments earlier. Searching for it therefore finds the right line without requiring a handcrafted entry per error code.

Example: if `piloterrordiag` is `"Failed to execute payload: exit code 1 from ..."`, the pattern `"Failed to execute payload"` is searched in the log and the surrounding 40 lines are returned.

**Level 3 — tail fallback**

If the `piloterrordiag` pattern fails to match (e.g. the diag was set programmatically without a corresponding log line, or the log was truncated), the last 40 lines of the log are returned. This is a last resort that ensures something is always sent to the LLM rather than an empty excerpt.

### Context window extraction for payload logs (code 1305)

No pattern matching is attempted. The last 300 lines of the combined stdout+stderr text are returned. Payload logs are unstructured application output where a keyword anchor would be unreliable; the failure is almost always near the end.

### Character cap

All excerpts are truncated to **6 000 characters** before being embedded in the LLM evidence dict. This keeps the synthesis prompt within token budget while preserving enough context for diagnosis.

---

## Links

The `links_md` evidence field contains a Markdown links block constructed from programmatic URLs — not from LLM text — so the URLs are always correct. `bamboo_executor` strips any LLM-invented links section and appends this block verbatim to the synthesised answer. The TUI then rewrites `[label](url)` entries to `label — url` so both the label and the URL are visible in the terminal.

Example output after synthesis:

For a payload failure (code 1305) the block includes a third link:

```
Links:
- BigPanDA Monitor — https://bigpanda.cern.ch/job?pandaid=7099498577
- Pilot Log — https://bigpanda.cern.ch/filebrowser/?pandaid=7099498577&json&filename=payload.stdout
- Payload stderr — https://bigpanda.cern.ch/filebrowser/?pandaid=7099498577&json&filename=payload.stderr
```

For pilot log failures:

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
