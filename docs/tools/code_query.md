# `code_query`

**Module:** `bamboo.tools.code_query`
**Registered as:** `code_query` (built-in, `core.py` `TOOLS` dict)
**Type:** Developer / superuser — on-demand source code fetch and analysis
**Access:** All MCP clients; Evidence panels gated by superuser mode in the UIs.

---

## Purpose

`code_query` fetches an arbitrary source file from any configured GitHub
repository and returns it as structured evidence for LLM analysis.  The
developer specifies a file path and asks any question about the code — from
open-ended review to targeted bug hypothesis to algorithm explanation.

Unlike `pilot_source_analysis` — which is driven by a job traceback and
automatically selects the relevant functions — `code_query` is fully
*query-driven*: you specify exactly which file (and optionally which function)
you want examined.

Typical uses:

- **Open-ended review:** *"Look at pilot.py and tell me if there are any problems."*
- **Bug investigation:** *"Can this failure be due to a problem in pilot/util/processes.py?"*
- **Algorithm explanation:** *"Explain how the looping job detection works in pilot/control/job.py."*
- **Targeted function review:** *"Does get_job handle missing fields safely?"*
- **Diagram generation:** *"Show me the state machine for pilot job states."*

When the question describes an algorithm or flow, the LLM may include a Mermaid
diagram in its response.  The Streamlit UI renders this inline; the TUI displays
the text portion only.

This tool is tagged `superuser`.  The Streamlit and TUI interfaces gate access
behind the `BAMBOO_SUPERUSER_PASSWORD` unlock; the tool is always registered on
the MCP server regardless of UI authentication state.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `file_path` | string | **Yes** | Relative path to the source file within the repository, e.g. `pilot/util/processes.py` or `pilot.py`. |
| `question` | string | **Yes** | The question to answer about the code, e.g. `"Are there any error handling gaps?"` or `"Explain the retry logic"`. |
| `function_name` | string | No | Name of a specific function to extract. When omitted, the full module source is returned. Useful for very large files where only one function is relevant. |

---

## Output

A JSON-serialised dict with `evidence` and `text` keys.

### `evidence` keys

| Key | Type | Description |
|---|---|---|
| `file_path` | string | The requested file path. |
| `github_url` | string | Browse URL for the file on GitHub (`/blob/<branch>/...`). |
| `repo` | string | Repository fetched from (e.g. `PanDAWMS/pilot3`). |
| `branch` | string | Branch fetched from (e.g. `master`). |
| `function_name` | string \| null | The requested function name, or `null` when the full module was requested. |
| `source` | string \| null | Source text (full module or extracted function). `null` when the fetch failed. Passed to the LLM separately from the JSON metadata — see [Evidence pipeline](#evidence-pipeline). |
| `truncated` | boolean | `true` when the source was cut at the line-boundary limit. Rare for typical files — the limit is 150 000 characters (~4 000 lines). |
| `fetch_error` | string | Empty on success. HTTP status or error message on failure. Also non-empty when a requested `function_name` was not found (falls back to full module with a note). |

### `text`

A one-line summary included in the synthesis prompt, e.g.:
```
Fetched pilot.py from PanDAWMS/pilot3 (master). GitHub: https://github.com/PanDAWMS/pilot3/blob/master/pilot.py
```

---

## How it works

### 1. Repository resolution

The repository and branch are resolved in this order:

1. `BAMBOO_CODE_QUERY_REPO` and `BAMBOO_CODE_QUERY_BRANCH` environment variables.
2. Built-in defaults: `PanDAWMS/pilot3`, `master`.

### 2. File fetch

The file is fetched from GitHub's raw content API:

```
https://raw.githubusercontent.com/<repo>/<branch>/<file_path>
```

No git clone, no checkout — a single HTTP GET per tool call.  Timeout: 20 s.
Connection errors and HTTP errors (404 etc.) are recorded in `fetch_error`;
`source` is `null`.

### 3. Optional function extraction

When `function_name` is provided, the full module source is parsed with Python's
`ast` module.  The extraction includes decorator lines, handles `async def`,
class methods, and nested functions.  If the function is not found, the full
module source is returned with a note in `fetch_error`.

### 4. Truncation

Source is capped at **150 000 characters** at the last complete line before that
limit.  When truncation occurs, `truncated` is `true` and a comment is appended:

```python
# --- TRUNCATED: showing 312 of 847 lines (535 lines omitted, 43,291 chars total) ---
```

For typical Python files (a few hundred lines) truncation never occurs.  For
very large files, request a specific `function_name` to stay within the limit.

### 5. Evidence pipeline

Source files are too large to pass through `_compact_json` (which has a 12 000-
character limit used for all other tools).  `code_query` bypasses this:

- The metadata fields (`file_path`, `truncated`, `github_url`, etc.) are
  serialised via `_compact_json` — they are small and always pass intact.
- The `source` field is extracted and appended as a plain fenced Python block
  after the metadata, with no character limit applied to it.

This means the LLM always receives the complete metadata (including the
`truncated` flag) and the full source separately, regardless of file size.

### 6. Synthesis

The `_SYSTEM_CODE_QUERY` synthesis prompt instructs the LLM to:

- Answer the question directly and precisely, quoting source lines verbatim.
- Identify the exact function and line number when diagnosing a bug.
- Include the GitHub URL for direct navigation.
- Emit a Mermaid diagram when the answer describes an algorithm or flow.
- **Not** report a finding as definitive unless it can be demonstrated from
  the source (guards against hallucinated "unused import" style findings).
- **Not** mention truncation when `truncated` is `false`.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `BAMBOO_CODE_QUERY_REPO` | `PanDAWMS/pilot3` | GitHub repository to fetch from (`owner/name`). Change for non-pilot codebases. |
| `BAMBOO_CODE_QUERY_BRANCH` | `master` | Branch or tag to fetch from. |
| `BAMBOO_SUPERUSER_PASSWORD` | *(unset)* | Enables the superuser UI gate in Streamlit and TUI. |
| `BAMBOO_SUPERUSER_TOOLS` | *(unset)* | Comma-separated additional tool names to treat as superuser-gated. |
| `BAMBOO_SUPERUSER_PATTERNS` | *(unset)* | Comma-separated extra regex patterns for the pre-dispatch guard. |

---

## Routing

`code_query` has a deterministic fast-path in `_build_deterministic_plan`.
The fast-path fires when the question contains any of:

- A `*.py` filename or path reference (e.g. `pilot.py`, `pilot/util/processes.py`).
- An inspection verb (`look at`, `download`, `fetch`, `explain`, `review`,
  `verify`, …) combined with a repository keyword (`source`, `code`, `file`,
  `pilot`, `module`, …).

The fast-path extracts the first `*.py` token from the question and passes it
as `file_path`.  Questions without a `*.py` token but with verb+keyword signals
are routed to `code_query` with the question as context and no `file_path` —
in that case the tool will return an error asking for a specific path.

**Priority:** `code_query` routing fires after all ID-driven rules (job/task
analysis) but before the general documentation RAG fallback, so a question
like *"why did job 123 fail? I see pilot.py in the traceback"* still routes
to `panda_log_analysis`.

**Follow-up routing:** after a `code_query` response, content-free affirmatives
(`"yes please"`, `"ok"`, `"continue"`) and code-review continuation phrases
(`"verify the full file"`, `"show the remaining code"`, `"get the complete source"`)
are automatically re-routed to `code_query` with the same file path recovered
from history.  This bypasses the topic guard so these natural follow-ups are
not blocked as off-topic.

---

## LLM analysis quality and known limitations

### What works well

- **Targeted questions** about specific functions, algorithms, or constructs
  produce reliable, precise answers.
- **Bug hypotheses** — *"could this cause a race condition?"* — are well-handled
  because the LLM can trace the relevant code paths.
- **Algorithm explanation with diagrams** is a strong use case; Mermaid output
  is reliable when the question explicitly asks for a diagram or describes a flow.

### What to watch for

**Open-ended reviews invite hallucination.**  Questions like *"find all problems"*
or *"list unused imports"* ask the LLM to do static analysis it cannot reliably
perform.  The model may report findings (e.g. *"Any is imported but unused"*)
that are simply false.  The synthesis prompt includes guards against definitive
false claims, but they are not foolproof.

**Use targeted questions for reliability:**

| Instead of | Ask |
|---|---|
| *"Find all unused imports"* | *"Is `Any` used anywhere in this file?"* |
| *"Are there any bugs?"* | *"Can `set_environment_variables()` raise an unhandled exception?"* |
| *"Review the error handling"* | *"Does the OIDC token renewal failure get escalated beyond a warning?"* |
| *"Is this code correct?"* | *"Does `get_job` handle the case where `job_id` is None?"* |

**Model choice matters.**  Some LLMs are more prone to hallucinated static
analysis findings than others.  If you see repeated false findings, try a
different model via `LLM_DEFAULT_PROVIDER` / `LLM_DEFAULT_MODEL`.  Claude
(Sonnet/Opus) and GPT-4o tend to be more conservative about definitive claims
than Gemini Flash.

**Branch freshness.**  The tool always fetches the current HEAD of the configured
branch.  If you are running a patched or unreleased version, the fetched source
may not match the deployed code.

---

## Example sessions

### Open-ended review

```
# Ensure superuser mode is active first
/superuser yourpassword

Download pilot.py and tell me if there are any problems
```

The planner extracts `pilot.py` and calls:
```json
{ "file_path": "pilot.py", "question": "tell me if there are any problems" }
```

> **Tip:** treat open-ended review findings as starting points for
> investigation, not definitive bugs.  Follow up with targeted questions
> to verify specific claims.

### Targeted function review

```
Does the set_environment_variables function in pilot.py handle missing env vars safely?
```

```json
{
  "file_path": "pilot.py",
  "function_name": "set_environment_variables",
  "question": "Does set_environment_variables handle missing env vars safely?"
}
```

This is more reliable than an open-ended review because the LLM analyses a
specific, bounded piece of code.

### Algorithm explanation with diagram (Streamlit)

```
Explain how the looping job detection algorithm works in pilot/control/job.py
```

The LLM walks through the algorithm step by step and emits a `flowchart TD`
Mermaid diagram rendered inline in Streamlit.

### Bug hypothesis after log analysis

```
# Step 1
Why did job 7099503721 fail?

# Step 2 — after seeing pilot_monitoring_error in the traceback
Can this be due to a bug in pilot/util/processes.py?

# Step 3 — drill into a specific function
Show me the list_processes_and_threads function in pilot/util/psutils.py
```

### Follow-up review

```
Download pilot.py and give me a high-level review

# After the initial answer:
Verify the set_environment_variables function specifically
Show me the complete source of that function
Does it handle PILOT_STAGEOUT_ATTEMPTS correctly?
```

All follow-ups are re-routed to `code_query` automatically.

---

## Differences from `pilot_source_analysis`

| Aspect | `pilot_source_analysis` | `code_query` |
|---|---|---|
| **Trigger** | After `panda_log_analysis` returns `pilot_monitoring_error` | Direct query — any question with a file path or code-inspection signal |
| **File selection** | Parsed automatically from Python traceback `File "..."` lines | Specified by the user (`file_path` parameter) |
| **Function selection** | All functions named in the traceback | Optional: single named function, or full module |
| **Repository** | Always `PanDAWMS/pilot3` (hardcoded) | Configurable via `BAMBOO_CODE_QUERY_REPO` |
| **Use case** | Explain why a specific job failure occurred | General code review, algorithm explanation, bug hypothesis, any codebase |
| **Mermaid diagrams** | No | Yes (`_MERMAID_GUIDANCE` in synthesis prompt) |
| **Follow-up routing** | Not specialised | Automatic re-routing for continuation phrases |
| **Access control** | Standard (no superuser gate) | Superuser-gated in UI |

---

## See also

- [`pilot_source_analysis`](pilot_source_analysis.md) — traceback-driven, fully
  automatic source analysis for `pilot_monitoring_error` jobs
- [`panda_log_analysis`](panda_log_analysis.md) — job failure diagnosis; triggers
  `pilot_source_analysis` for pilot errors
- [`docs/interfaces.md`](../interfaces.md) — superuser mode setup
- [`docs/question-cheatsheet.md`](../question-cheatsheet.md) — example questions
  and guidance on asking code review questions effectively
