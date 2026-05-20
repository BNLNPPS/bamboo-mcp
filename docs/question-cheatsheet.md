# AskPanDA Question Cheat Sheet

A ready-to-paste collection of test questions for Bamboo / AskPanDA, grouped
by tool and routing path.  Use these to verify routing, synthesis quality, and
evidence correctness after code changes.

After each question, type `/tracing` to see which tools were called and how
long each step took.  Type `/costs` to see the estimated LLM token cost.

---

## Task status (`panda_task_status`)

Questions containing a numeric task ID route here deterministically.

```
What is the status of task 29871234?
Summarise task 27431862.
Has task 29004501 finished?
Why is task 28765432 failing?
Show me the progress of task 27000001.
```

> **Expected routing:** `route=FAST_PATH`, tool = `panda_task_status`.
> **TUI shorthand:** `/task 29871234`

---

## Job status (`panda_job_status`)

Questions containing a job (pandaid) route here.

```
What is the status of job 6837798305?
Show me the metadata for pandaid 6901234567.
Which site ran job 6837798305?
What files does job 6901234567 have?
```

> **Expected routing:** `route=FAST_PATH`, tool = `panda_job_status`.
> **TUI shorthand:** `/job 6837798305`

---

## Job failure analysis (`panda_log_analysis`)

Job ID questions with failure/diagnostic keywords route here.

```
Why did job 6837798305 fail?
Analyse the failure of job 6901234567.
What error caused job 6837798305 to die?
Diagnose job 6901234567.
```

> **Expected routing:** `route=FAST_PATH`, tool = `panda_log_analysis`.
> **TUI shorthand:** `/job 6837798305` (also triggers analysis when the job is failed)

---

## Pilot / Harvester statistics (`panda_harvester_workers`)

Questions about live pilot or Harvester worker counts.

```
How many pilots are running at BNL right now?
How many pilots are running at MWT2?
What is the pilot count at AGLT2?
Show me pilot statistics for CERN.
How many idle pilots are there at SWT2_CPB?
How many MCORE pilots are running at BNL?
How many managed pilots are submitted at IN2P3-CC?
How many pilots ran at BNL in the last 3 hours?
Show me pilot activity at TRIUMF since yesterday.
What is the pilot status at SLAC-SCS?
How many harvester workers are at NET2?
```

> **Expected routing:** `route=FAST_PATH`, tool = `panda_harvester_workers`, with `site=<SITE>` extracted.
> **Note:** "finished" pilots include both successful and failed exits — the Harvester API does not separate them.

---

## Live job statistics (`panda_jobs_query`)

Questions about live job counts, failure rates, or error breakdowns from the ingestion database.

```
How many jobs failed at BNL?
What is the job failure rate at AGLT2?
How many jobs are running at CERN right now?
Show me the top errors at SWT2_CPB.
How many jobs finished at MWT2 today?
Which jobs are failing at BNL?
What is the job status breakdown at TRIUMF?
How many jobs failed at IN2P3-CC in the last hour?
What is the pilot error rate at BNL?
```

> **Expected routing:** `route=FAST_PATH`, tool = `panda_jobs_query`, with `queue=<SITE>` from the question.

---

## Site health — pilots AND jobs (`panda_harvester_workers` + `panda_jobs_query`)

Questions that mention both pilots and jobs at a site trigger the two-tool site-health fast-path.

```
What are the pilot and job failure rates at BNL?
What are the pilot and job failure rates at MWT2?
How many pilots and how many failed jobs are there at BNL right now?
Give me a BNL site health summary — pilots and jobs.
What is the pilot count and job error rate at AGLT2?
How are pilots and jobs doing at CERN?
How many running pilots and job failures at SWT2_CPB?
What is the site status at TRIUMF — pilots and jobs?
Pilot health and job status at IN2P3-CC.
```

> **Expected routing:** `route=FAST_PATH`, tools = `panda_harvester_workers` + `panda_jobs_query`, both scoped to the same site.
> **Check with `/inspect`:** the evidence should show two labelled sections (Pilots and Jobs).
> **Check with `/json`:** confirms `site=<X>` was passed to harvester and `queue=<X>` to jobs query.

---

## PanDA server health (`panda_server_health`)

Questions about whether the PanDA server itself is alive and responding.
Requires `PANDA_MCP_BASE_URL` to be set; returns a graceful error if not.

```
Is the PanDA server alive?
Is PanDA OK?
Is the PanDA server up?
Is PanDA running?
What is the PanDA server status?
PanDA server health check.
Is PanDA available?
Is PanDA responding?
Is PanDA down?
```

> **Expected routing:** `route=FAST_PATH`, tool = `panda_server_health` — no topic guard, no LLM planning call.
> **Evidence keys:** `is_alive` (bool), `raw_response` (first 500 chars from the server), `error` (null on success).
> **If `PANDA_MCP_BASE_URL` is unset:** the tool returns `is_alive: false` with an error message explaining the server is not connected — no crash.
> **Check with `/inspect`:** should show `"is_alive": true` and the raw server response.

### Setup

**Minimum config (known working endpoint):**

```bash
export PANDA_MCP_BASE_URL="https://aipanda120.cern.ch:8443/mcp/"
# PANDA_MCP_USE_SSE should NOT be set — this server uses streamable-HTTP
```

**Authentication:** The PanDA MCP server requires an OIDC token. Obtain one with `get-panda-token` from the `panda-mcp-client` package (requires `uv`):

```bash
uvx --from panda-mcp-client get-panda-token
```

Follow the browser prompt. The token is saved to `~/.panda_id_token`. Bamboo reads the `id_token` field from this file automatically at startup — no further configuration needed. Token renewal will be handled by a Bamboo MCP agent service; for now, re-run `get-panda-token` roughly once a month when the token expires.

**At CERN / lxplus:** The CERN Grid CA is in the system store. No TLS configuration needed — just set `PANDA_MCP_BASE_URL`, run `get-panda-token` once, and start Bamboo.

**Outside CERN (e.g. Mac laptop):** Python's `httpx` uses the `certifi` bundle which does not include the CERN Grid CA. Append it once (repeat after `pip upgrade certifi`):

```bash
curl -o /tmp/cern-root-ca.pem \
  "https://cafiles.cern.ch/cafiles/certificates/CERN%20Root%20Certification%20Authority%202.crt"
cat /tmp/cern-root-ca.pem >> $(python3 -c "import certifi; print(certifi.where())")
```

Or, for development/testing only, disable TLS verification:

```bash
export PANDA_MCP_TLS_VERIFY=0
```

---

## Queue / site configuration (`panda_queue_info`)

```
What are the settings for queue BNL-ATLAS?
Show me the configuration for site AGLT2.
What resources does CERN_PROD support?
Is BNL accepting MCORE jobs?
What is the maxtime for queue SWT2_CPB?
```

> **Expected routing:** `route=FAST_PATH`, tool = `panda_queue_info`.

---


## CRIC queuedata (`cric_query`)

Questions about ATLAS computing queue status, copytools, and site resources
from the CRIC Computing Resource Information Catalogue.

```
Which queues are not online?
How many queues are in each status?
Which queues are using the rucio copytool?
Which queues are NOT using the rucio copytool?
Which queues are using the objectstore copytool?
What copytools are in use?
Which queues are active at BNL?
Which queues at CERN-PROD are online?
What is the status of all queues at BNL?
Is BNL-PTEST online?
Which MCORE queues are online at BNL?
How many CPU cores are pledged at CERN-PROD?
What sites are available in CRIC?
When was the CRIC database last updated?
Which queues are brokeroff?
List queues in test status.
```

> **Expected routing:** `route=FAST_PATH`, tool = `cric_query`.
> **Check with `/inspect`:** the evidence shows `sql`, `columns`, `rows`,
> `row_count`, `truncated`, and `error` (null = success).
> **Tip:** ask "What sites are available?" first to see the exact `atlas_site`
> values — site names include suffixes like `BNL-ATLAS`, `CERN-PROD`, `CERN-T0`.

### Multi-turn CRIC follow-ups

After a CRIC response, short status-check follow-ups route back to CRIC
automatically without needing explicit CRIC vocabulary.

```
# Turn 1
Which queues are using the objectstore copytool?

# Turn 2 (routes to cric_query via contextual follow-up detection)
Is BNL-PTEST active?

# Turn 3
What about CERN-PTEST?
```

### Disambiguation

Some questions match both the jobs DB and CRIC — the router asks which you
mean.  You can reply with just `"cric"` or `"jobs"` and the original question
will be re-executed against the chosen database.

```
# Ambiguous — triggers clarification
Which queues have the most failed jobs?

# Clearly CRIC — routes directly without asking
Which queues use the rucio copytool?
Which queues are not online?
What is the queue status at BNL?
```

## CGSim simulation database (`cgsim.sim_query`)

Questions about the results of a CGSim simulation run.  Requires
`ASKPANDA_PLUGIN=cgsim` and `CGSIM_DB_PATH` to be set.

### Job timing

```
How long did job J-001 take to execute?
What was the total wall-clock time for job J-001?
Why did job J-001 spend so long queuing?
How much of job J-001's time was spent waiting for file transfers?
How much time did job J-001 spend waiting for a compute slot?
```

> **Expected routing:** `route=RETRIEVE` → `cgsim.sim_query` (via LLM planner or deterministic path).
> **Check with `/inspect`:** evidence shows `sql`, `columns`, `rows`, `summary`.
> **Units:** all duration values are in **seconds**.

### Site and resource analysis

```
Which site had the most jobs allocated to it?
What was the average execution time per site?
Which site had the highest CPU utilisation?
Was the grid under heavy load when job J-001 ran?
How many jobs ran at each site?
```

> **Expected SQL pattern:** `GROUP BY json_extract(METADATA, '$.site')` with
> `EVENT='JobAllocation'` or `EVENT='JobExecution'` and `STATE='Finished'`.

### Network congestion and file transfers

```
Were any file transfers affected by network congestion?
Which file transfers had the highest link load?
What was the average file transfer speed?
Which source/destination site pair had the most transfers?
What was the total data transferred during the simulation?
```

> **High link_load** (close to 1.0) signals congestion.  `link_load` is in
> the `FileTransfer/Started` METADATA.

### I/O bottleneck analysis

```
Which disk was the I/O bottleneck?
What was the average disk read throughput?
Which host had the slowest disk reads?
How much data was read from disk per job?
```

> **Disk throughput** = `size / duration` from `FileRead/Finished`.
> Compare against `disk_read_bw` to see how close to the device limit jobs ran.

### Job health

```
Did jobs retry frequently?
How many jobs succeeded on the first attempt?
Which jobs retried the most?
What is the retry rate distribution?
```

> **`retries = 0`** means the job succeeded without a retry.  Available on
> `JobExecution/Finished`.

### Full job timeline

```
Show me all events for job J-001.
What happened to job J-001 from start to finish?
List the file transfers for job J-001.
What files did job J-001 read from disk?
```

> Filter by `JOB_ID = 'J-001'` with no `EVENT` filter to see the full timeline.
> The `ORDER BY TIME ASC` clause is automatically applied.

---

## Documentation / RAG (`panda_doc_search` + `panda_doc_bm25`)

General PanDA/ATLAS knowledge questions route to the vector + BM25 retrieval pipeline.

```
What is PanDA?
How does the pilot system work in ATLAS?
What is a Harvester worker?
What does piloterrorcode 1301 mean?
How does JEDI schedule tasks?
What is the difference between a task and a job in PanDA?
What causes stage-in failures?
How does Harvester communicate with the grid?
What is a MCORE job?
What is the role of the taskbuffer in PanDA?
How are jobs retried in PanDA?
What is gshare and how does it affect job priority?
```

> **Expected routing:** `route=RETRIEVE`, tools = `panda_doc_search` + `panda_doc_bm25`.
> **Note:** answers are only as good as the indexed knowledge base. Check the RAG hit count in `/tracing`.

---

## Multi-turn follow-up questions

Test context memory and pronoun resolution across turns.

```
# Turn 1
What is the status of task 49428233?

# Turn 2 (should resolve "it" to task 49428233)
How many jobs failed in it?

# Turn 3
What were the top error codes?
```

```
# Turn 1
How many pilots are running at BNL right now?

# Turn 2
How does that compare to AGLT2?
```

> **Check with `/history`** to see which turns are in context.
> **Check with `/tracing`** to confirm the task/job ID was resolved from history.

---

## Social / greeting intercepts (zero LLM cost)

These are caught before any tool call or LLM call.

```
Hello
Hi there
Thanks
Thank you
OK
Got it
```

> **Expected routing:** no tool call, no LLM call — instant canned response.
> **Verify:** `/tracing` should show no spans at all (or only a `tool_call` span with 0 ms).

---

## TUI diagnostic workflow

A suggested sequence after any question to fully inspect the response:

```
/tracing       — timing, which tools ran, token counts
/costs         — estimated USD cost broken down by LLM call
/inspect       — compact evidence dict (what the LLM received)
/json          — raw BigPanDA API response for the last query
/history       — conversation turns currently in context
```

---

## Fast-path vs LLM planner comparison

To compare deterministic routing against the LLM planner for the same question:

```
# 1. Ask with fast-path ON (default)
What are the pilot and job failure rates at BNL?
/tracing    ← should show FAST_PATH, no bamboo_plan span
/costs      ← one LLM call (synthesis only)

# 2. Switch to LLM planner
/fastpath off

# 3. Ask the same question
What are the pilot and job failure rates at BNL?
/tracing    ← should show bamboo_plan span + synthesis
/costs      ← two LLM calls (planning + synthesis), higher token count

# 4. Restore fast-path
/fastpath on
```

> **What to look for:** the planner should independently select the same two tools (`panda_harvester_workers` + `panda_jobs_query`) and pass the same `site=` / `queue=` arguments. If it doesn't, the planner routing guidance in `planner.py` needs updating.

---

## Edge cases worth testing

```
# Site with separator in name
How many pilots are running at SLAC-SCS?

# Site with underscore
What is the job failure rate at SWT2_CPB?

# Site in queue=X form
Pilots for queue CERN_PROD

# Unknown site (not in fallback list — tests regex extraction)
How many pilots are running at MWT2?

# No site specified (should query all sites)
How many pilots are running right now?

# Time window extraction
How many pilots ran at BNL in the last 6 hours?
How many pilots failed at AGLT2 since yesterday?
What were the pilot counts at CERN today?

# Task keyword exclusion (should NOT trigger site-health)
How many pilots and failed jobs are there for task 29871234?

# Pure pilot question with jobs-like phrasing (should NOT trigger site-health)
How many pilots failed at BNL in the last hour?

# PanDA health — should NOT match site/job questions that mention panda
What is the status of task 29871234 on the panda server?
How many panda jobs failed at BNL?
```

---

## PanDA server health edge cases

```
# Plain liveness — should route to panda_server_health, not RAG
Is PanDA alive?
Is the PanDA server OK?

# Synonym forms
Is PanDA up?
Is PanDA running?
Is PanDA available?
Is PanDA down?

# Job/task questions that mention panda incidentally — must NOT route to panda_server_health
How many panda jobs failed at BNL?
What is the status of panda task 29871234?
```

> **Verify false-negative:** `/fastpath off` then ask "Is PanDA alive?" — the planner should still select `panda_server_health`.
> **Verify false-positive guard:** "How many panda jobs failed at BNL?" must route to `panda_jobs_query`, not `panda_server_health`.

---

## Pilot source code query (`code_query`) — superuser only

These questions fetch and analyse pilot source files directly from GitHub.
Requires superuser mode to be active in the UI (`/superuser <pw>` in the TUI,
or the Unlock button in the Streamlit sidebar).

### Full module questions

```
Look at pilot/util/processes.py and tell me if it handles missing UIDs safely.
Can a problem in pilot/util/filehandling.py cause this failure?
What does pilot/common/exception.py contain?
Review pilot/util/timing.py for potential threading issues.
```

> **Expected routing:** `tool = code_query`, full module source returned.
> The LLM will scan the full file (up to 12 000 characters) and answer directly.

### Function-targeted questions

```
Explain how the is_looping function in pilot/control/job.py works.
Show me the get_job function in pilot/control/job.py.
What does list_processes_and_threads do in pilot/util/psutils.py?
```

> **Tip:** mentioning a specific function name in the question allows the planner
> to pass `function_name` to the tool and retrieve only that function, avoiding
> the 12 000-character truncation limit for large modules.

### Diagram-generating questions

These questions describe algorithms or flows, which prompts the LLM to include
a Mermaid diagram. In Streamlit, the diagram renders inline after the text answer.

```
Explain how the looping job detection algorithm works in pilot/control/job.py
Show me a diagram of how the pilot handles job state transitions.
Walk me through the flow of the get_pilot_job function in pilot/control/pilot.py
Explain the retry logic in pilot/util/https.py as a flowchart.
```

> **Expected output (Streamlit):** prose explanation followed by a rendered
> Mermaid diagram (flowchart TD or stateDiagram-v2).
> **Expected output (TUI):** prose only — Mermaid blocks are stripped.

### Combined bug investigation (with prior log analysis)

```
# Step 1: run log analysis on a failed pilot_monitoring_error job
Why did job 7099503721 fail?
/tracing   ← verify tool = panda_log_analysis

# Step 2: ask about the specific file involved
Can this failure be due to a bug in pilot/util/psutils.py?

# Step 3: drill into a specific function
Show me the list_processes_and_threads function in pilot/util/psutils.py
and explain why it raises a KeyError for missing UIDs.
```

> **Note:** for step 2 and 3, superuser mode must be active. The planner
> routes to `code_query` (direct file query). `pilot_source_analysis`
> handles traceback extraction automatically — use it for step 1.

---

## Mermaid diagram verification

Use these questions to confirm that Mermaid diagram generation and rendering
are working correctly in the Streamlit UI.

```
Explain how the looping job detection works in pilot/control/job.py
Show me a diagram of PanDA job states (use your general knowledge).
Explain the Harvester brokerage flow as a sequence diagram.
```

> **What to look for (Streamlit):** a rendered diagram appears below the text
> answer. The raw ` ```mermaid ``` ` block should NOT appear as text.
> **What to look for (TUI):** the answer is text-only; no raw Mermaid syntax
> in the output.
> **Check history integrity:** ask a follow-up question; the conversation
> history should not contain raw Mermaid blocks.

---

## Superuser mode verification

### Streamlit

1. Start the Streamlit UI without `BAMBOO_SUPERUSER_PASSWORD` set → verify
   that "Developer access" is absent from the sidebar.
2. Restart with `BAMBOO_SUPERUSER_PASSWORD=test123` set → verify the section
   appears.
3. Enter the wrong password → verify "Incorrect password." appears.
4. Enter the correct password → verify 🔓 **Superuser mode active** and the
   Lock button.
5. Lock the session → verify the section returns to the password input.
6. Unlock again → ask a `code_query` question (e.g. `"Look at pilot.py"`) → verify the Evidence and
   Raw JSON expanders are visible.
7. Lock → ask the same question again → verify the expanders are hidden.

### TUI

```
/superuser wrongpassword     ← should print "Incorrect password."
/superuser yourpassword      ← should print "🔓 Superuser mode unlocked..."
/help                        ← verify /superuser appears in the command list
```

---

## Asking code review questions effectively

The `code_query` tool fetches real source code and passes it to the LLM, but
LLMs have inherent limitations as static analysis engines.  The question you
ask determines whether you get a reliable, useful answer.

### Core principle: targeted questions beat open-ended ones

The LLM reasons well about specific, bounded questions.  It reasons poorly
about exhaustive searches across a whole file — and when asked to "find all X",
it may invent findings to appear thorough.

| Question type | Reliability | Example |
|---|---|---|
| "Is X used in this file?" | ✓ High | "Is `Any` used anywhere in pilot.py?" |
| "Can function F raise exception E?" | ✓ High | "Can `set_environment_variables` raise a `KeyError`?" |
| "Explain algorithm A in function F" | ✓ High | "Explain the retry logic in `pilot/util/https.py`" |
| "Does F handle case C?" | ✓ High | "Does `get_job` handle `None` job_id?" |
| "Find all unused imports" | ✗ Low | (will hallucinate) |
| "Find all bugs" | ✗ Low | (will invent findings) |
| "Is this code correct?" | ✗ Low | (too broad) |

### Verifying a specific claim

If the LLM returns a finding you want to verify — e.g. *"X is unused"* —
ask directly:

```
Is 'Any' actually used anywhere in pilot.py?
Show me every line in pilot.py that uses 'Any'.
```

The LLM will trace the usages and give you a reliable answer to that
specific question.

### Open-ended reviews as a starting point

Open-ended reviews (*"look at pilot.py and find problems"*) are useful as a
**starting point** for investigation, not as a final verdict.  The LLM may
produce a mix of genuine observations and hallucinated findings.

Workflow:
```
# Step 1: open-ended review to get a list of candidate areas
Look at pilot.py and give me a high-level review

# Step 2: follow up on specific findings to verify them
Is the OIDC token renewal failure actually swallowed?
Show me the relevant code around that exception handler.

# Step 3: drill into the most interesting ones
Can the OIDC failure cause the job to fail silently?
```

### Function-targeted review

For large files, always specify `function_name` to focus the analysis:

```
# Open-ended on a large file — may be truncated or unfocused
Look at pilot/control/job.py and find any problems

# Targeted — reliable and focused
Does the get_job function in pilot/control/job.py handle missing fields safely?
Show me the is_looping function and explain what it does.
Can the wrap_up function in pilot.py raise an unhandled exception?
```

### Choosing a model for code analysis

Some models are more prone to hallucinated static analysis findings than others.
If you see repeated false findings (unused imports that are used, missing
functions that exist, etc.), try a different model:

```bash
# More conservative about definitive claims:
export LLM_DEFAULT_PROVIDER="anthropic"
export LLM_DEFAULT_MODEL="claude-sonnet-4-5"

# Or:
export LLM_DEFAULT_PROVIDER="openai"
export LLM_DEFAULT_MODEL="gpt-4o"
```

### Following up after a code review

After any `code_query` response, these follow-ups are automatically re-routed
to the same tool without needing to repeat the file path:

```
Yes please            # affirmative
Continue              # request more
Verify the full function
Show me the complete source
Get the rest of the file
Can you check the set_environment_variables function specifically?
```

If the follow-up doesn't route correctly (ends up in RAG or topic guard),
repeat the file path explicitly:

```
Look at pilot.py again — is 'Any' actually used?
```
