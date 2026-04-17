# `bamboo_last_evidence`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.bamboo_executor`
**Type:** Orchestration — evidence inspection

---

## Purpose

`bamboo_last_evidence` exposes the evidence dict from the most recent data tool call, without re-fetching from BigPanDA or OpenSearch. It is used by the TUI's `/inspect`, `/json`, and CRIC table commands to let the user examine what data underpinned the last answer.

The tool reads from an in-process module-level store (`_last_evidence_store`) that is populated by `execute_plan` after every successful tool call.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `mode` | `"evidence"` \| `"raw"` \| `"table"` | No (default `"evidence"`) | Which representation to return. |
| `tool` | string | No | Tool name to retrieve evidence for (e.g. `"panda_task_status"`). Defaults to the most recently called tool. |

### Mode values

| Mode | Returns |
|---|---|
| `evidence` | Compact LLM-facing evidence dict (fields like `job_id`, `failure_type`, `log_excerpt`, etc.). |
| `raw` | Verbatim BigPanDA API payload as JSON. |
| `table` | Pre-formatted CRIC full-list table text (only populated after a `cric_query` with many rows). |

---

## Output

A single text content block containing the JSON-serialised evidence dict or table string.

---

## The `bamboo_executor` execution layer

`bamboo_last_evidence` is implemented in `bamboo_executor.py`, which also contains the core plan execution logic. Key responsibilities of that module:

**`execute_plan(plan, question, history)`**

1. Iterates `plan.tool_calls` in order.
2. Resolves each tool via the core `TOOLS` registry or the plugin entry-point loader.
3. Calls `await tool.call(args)` and unpacks JSON evidence with `unpack_tool_result`.
4. Selects the appropriate synthesis system prompt based on which tools were called (e.g. `_SYSTEM_LOG_ANALYSIS` for log analysis calls).
5. Calls the LLM via `call_llm` to produce a natural-language answer.
6. For `panda_log_analysis` responses: strips any LLM-invented `Links:` section and appends the canonical links block built from programmatic URLs (`_strip_llm_links_section` + `_log_analysis_links_md`).
7. Appends a "Database last updated" footnote for DB-backed tools (`_db_footnote`).

**Evidence store**

After each successful tool call the evidence dict is stored under the tool name key. The `last_tool` key tracks which tool ran most recently. Fields in `_STORE_STRIP` (`pandaid_list`) and `_LLM_STRIP` (`raw_payload`, `pandaid_list`) are excluded from the LLM evidence to keep prompts compact.

**Direct-format bypass**

For `cric_query` results that are too large to pass through the macOS 8 KB stdio pipe buffer, `execute_plan` detects a large result and writes the formatted table to a temp file, returning a short sentinel string (`__CRIC_TABLE_READY__:<N>`) that the TUI intercepts to fetch the table via a second `bamboo_last_evidence(mode="table")` call.

---

## TUI commands backed by this tool

| TUI command | Mode | Description |
|---|---|---|
| `/inspect` | `evidence` | Shows the compact evidence dict for the last call. |
| `/json` | `raw` | Shows the verbatim API response JSON. |
| CRIC table sentinel | `table` | Retrieves the pre-formatted CRIC queue table. |

---

## See also

- [`bamboo_answer`](bamboo_answer.md) — calls `execute_plan` for all data tool routing
- [`bamboo_plan`](bamboo_plan.md) — produces the Plan that `execute_plan` consumes
