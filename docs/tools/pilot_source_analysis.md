# `pilot_source_analysis`

**Package:** `askpanda_atlas`
**Modules:** `askpanda_atlas.pilot_source_analysis_impl`
**Type:** Source code analysis — pilot3 exception deep-dive

---

## Purpose

`pilot_source_analysis` is a follow-up tool for jobs classified as `pilot_monitoring_error` by `panda_log_analysis`. It fetches the specific pilot3 source modules named in the exception traceback from the [PanDAWMS/pilot3](https://github.com/PanDAWMS/pilot3) GitHub repository, extracts the exact functions involved using AST-based parsing, and returns the source snippets as structured evidence for LLM synthesis.

Use when the user wants to understand *why* the pilot code raised an exception — not just that it did — or when they ask whether the pilot code could be improved or patched.

This tool is **not** for general job diagnosis. The planner will only route to it when `panda_log_analysis` has already returned `failure_type='pilot_monitoring_error'` and the follow-up question is specifically about the pilot code or the exception cause.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `job_id` | integer | Yes | PanDA job ID — used to label the evidence. |
| `log_excerpt` | string | Yes | The `evidence.log_excerpt` from the prior `panda_log_analysis` call. Must contain the Python traceback. |
| `pilot_error_diag` | string | No | `evidence.piloterrordiag` from job metadata, used as a fallback exception description if the traceback cannot be parsed. |

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `job_id` | The queried job ID. |
| `exception` | The exception string extracted from the traceback (e.g. `"KeyError: 'getpwuid(): uid not found: 6435'"`). |
| `traceback_frames` | List of `{pilot_path, func}` dicts parsed from the traceback, one per unique `(file, function)` pair in call-chain order. Non-pilot3 frames (e.g. CPython stdlib in CVMFS) are excluded. |
| `source_snippets` | Dict keyed by `"path::function"` containing the extracted source of each function, up to 4 000 characters each. |
| `github_urls` | Dict keyed by pilot path with links to the corresponding file on GitHub (`/blob/master/...`). |
| `files_fetched` | List of pilot paths successfully downloaded from GitHub. |
| `missing_functions` | Functions named in the traceback that could not be found in the downloaded source (e.g. lambdas, dynamically generated functions). |
| `fetch_errors` | Dict keyed by pilot path with HTTP error messages for any failed GitHub fetches. |

---

## How it works

### 1. Traceback parsing

`parse_traceback_frames` scans the `log_excerpt` for Python `File "..."` lines whose paths contain `pilot/`. Only paths starting with `pilot/` are included — stdlib frames (e.g. `getpass.py` in CVMFS) are excluded because they are not part of the pilot3 codebase and the fix does not lie there. Each unique `(pilot_path, function)` pair is returned once, in call-chain order.

Example input:
```
File "/tmp/atlas_8GX3ynDr/pilot3/pilot/util/psutils.py", line 428, in list_processes_and_threads
```
Parsed as: `{"pilot_path": "pilot/util/psutils.py", "func": "list_processes_and_threads"}`.

### 2. Module fetching

Each unique pilot path is fetched exactly once from:
```
https://raw.githubusercontent.com/PanDAWMS/pilot3/master/<pilot_path>
```
A file that appears multiple times in the traceback (e.g. `processes.py` with two frames) is downloaded only once. Fetch failures are recorded in `fetch_errors` but do not abort the analysis — other files are still fetched.

### 3. Function extraction

`extract_function_source` uses Python's `ast` module to locate each named function in its module. This is more reliable than regex: it correctly handles decorators (which are included in the output), async function definitions, and functions nested inside classes. The extracted source is dedented and capped at 4 000 characters.

### 4. Synthesis

The `_SYSTEM_PILOT_SOURCE` synthesis prompt instructs the LLM to:
- Quote the exact source lines responsible for the exception.
- Explain whether the root cause is a pilot code defect or a site configuration issue.
- Suggest a concrete fix or workaround.
- Include GitHub source links so developers can navigate directly to the relevant file.

---

## Worked example

For job 7099503721 (`pilot_monitoring_error`, code 1354):

**Traceback frames extracted:**
```
pilot/util/monitoring.py  :: set_cpu_consumption_time
pilot/util/processes.py   :: get_current_cpu_consumption_time
pilot/util/processes.py   :: get_ps_cache
pilot/util/psutils.py     :: list_processes_and_threads
```

**Key snippet extracted from `pilot/util/psutils.py::list_processes_and_threads`:**
```python
def list_processes_and_threads():
    ...
    current_user = getpass.getuser()   # raises KeyError if UID not in passwd
    ...
```

**LLM synthesis outcome:** The `getpass.getuser()` call in `list_processes_and_threads` invokes `pwd.getpwuid(os.getuid())` internally. On this BNL worker node, UID 6435 exists in the process table (belonging to a process from another user's job or a system daemon) but is not in `/etc/passwd` or the LDAP/SSSD user database. The fix is to wrap the call in a `try/except KeyError` and fall back to `str(os.getuid())` — which is standard practice for container/HPC environments where process tables may contain UIDs that don't resolve to usernames.

---

## Routing

The planner routes to this tool when:
1. `panda_log_analysis` has already been called and returned `failure_type='pilot_monitoring_error'`.
2. The follow-up question asks about the pilot code, the exception cause, or whether a fix is possible.

Example trigger phrases: "why did the pilot code fail", "show me the source", "what is wrong with the pilot", "can this be fixed".

This tool is never routed to for initial job diagnosis questions — use `panda_log_analysis` for those.

---

## Availability

| Package | Entry point | Status |
|---|---|---|
| `askpanda_atlas` | `atlas.pilot_source_analysis` | Available |
| `askpanda_epic` | `epic.pilot_source_analysis` | Available |

Both experiments use the same PanDAWMS/pilot3 codebase, so the tool is fully applicable to ePIC jobs. The `askpanda_epic` plugin re-exports the implementation from `askpanda_atlas.pilot_source_analysis_impl` directly — the analysis logic is not duplicated. If `askpanda_atlas` is not installed alongside `askpanda_epic`, a fallback stub is used that returns a clear error message.

---

## Limitations

- Only fetches from the `master` branch of `PanDAWMS/pilot3`. If the job ran with a different pilot branch or a patched version, the fetched source may not match exactly.
- Requires a live internet connection to `raw.githubusercontent.com`. If GitHub is unreachable the `fetch_errors` field will be populated and `source_snippets` will be empty, but the evidence dict (with `traceback_frames` and `exception`) is still returned and useful.
- Functions defined as lambdas, inside `exec()`, or generated dynamically will appear in `missing_functions`.

---

## See also

- [`panda_log_analysis`](panda_log_analysis.md) — primary job failure diagnosis tool; must be called before `pilot_source_analysis`
- [`bamboo_last_evidence`](bamboo_last_evidence.md) — inspect the raw evidence dict via `/inspect` or `/json`
