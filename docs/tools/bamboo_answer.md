# `bamboo_answer`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.bamboo_answer`
**Type:** Orchestration — primary entry point

---

## Purpose

`bamboo_answer` is the single entry point for all PanDA/ATLAS questions. It accepts a natural-language question, routes it to the appropriate data tool or documentation index, and returns a synthesised natural-language answer. Clients (including the TUI) call only this tool; they do not call data tools directly.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | One of `question` or `messages` | The user's question. |
| `messages` | array of `{role, content}` | One of `question` or `messages` | Full chat history for multi-turn context. |
| `bypass_routing` | boolean | No (default `false`) | Skip all routing and delegate directly to the LLM. |
| `bypass_fast_path` | boolean | No (default `false`) | Skip deterministic fast-path intercepts; force the LLM planner. Useful for testing planner coverage. |
| `include_jobs` | boolean | No (default `true`) | Include job records when fetching task status (`?jobs=1`). |
| `include_raw` | boolean | No (default `false`) | Include raw tool result previews in the response when errors are detected. |

---

## Routing logic

Routing is performed in priority order. The first matching path wins.

1. **Social intercept** — standalone greetings (`hello`, `hi`) and acknowledgements (`thanks`, `ok`) are handled without an LLM call.
2. **Fast-path intercepts** (bypassed when `bypass_fast_path=true`):
   - PanDA server health question → `panda_server_health`
   - Pilot + jobs signals both present → `panda_harvester_workers` + `panda_jobs_query`
   - Pilot-only signals → `panda_harvester_workers`
   - Jobs DB or CRIC signals → `panda_jobs_query` or `cric_query`
3. **Topic guard** — an LLM call checks whether the question is on-topic. Off-topic questions receive a polite refusal without calling any data tool.
4. **Deterministic fast-path** (post-topic-guard, bypassed when `bypass_fast_path=true`):
   - Job ID + failure keywords → `panda_log_analysis`
   - Job ID (no task ID) → `panda_job_status`
   - Task ID → `panda_task_status`
5. **LLM planner fallback** — `bamboo_plan` is called to choose tools for ambiguous or multi-step questions. The plan is then executed by `bamboo_executor`.

---

## Output

A single text content block containing the synthesised natural-language answer. The exact content depends on which data tool was called.

---

## Key design notes

- `bamboo_answer` is the **only** tool the TUI calls; all data fetching is handled by downstream tools via `bamboo_executor`.
- Multi-turn context is passed as a `messages` list. The tool extracts conversation history internally and passes it to the LLM synthesiser.
- Fast-path intercepts bypass the topic guard for unambiguous operational questions, saving one LLM round-trip.
- The `_PILOT_SIGNALS` frozenset in the module controls which phrases trigger the pilot fast-path. It includes resource-type qualifiers such as `"mcore pilots"`, `"score pilots"`, and `"hcore pilots"`.

---

## See also

- [`bamboo_plan`](bamboo_plan.md) — LLM-backed planner used for ambiguous questions
- [`bamboo_executor`](bamboo_last_evidence.md) — plan execution and LLM synthesis layer
