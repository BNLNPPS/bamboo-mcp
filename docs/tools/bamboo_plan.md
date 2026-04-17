# `bamboo_plan`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.planner`
**Type:** Orchestration — LLM-backed planner

---

## Purpose

`bamboo_plan` asks an LLM to decompose a user question into a machine-parseable execution plan that names which Bamboo tool(s) to call and with what arguments. It is used as the fallback when deterministic routing in `bamboo_answer` cannot resolve intent unambiguously.

The planner only *produces* a plan. It does not execute tools; that is handled by `bamboo_executor` (via `execute_plan`).

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | Yes | The user question to plan for. |
| `hints` | object | No | Structured hints from deterministic extraction (e.g. extracted task ID or job ID). |
| `namespaces` | array of string | No | Plugin namespaces to include in the tool catalogue (e.g. `["atlas"]`). |
| `temperature` | number | No (default `0.0`) | LLM sampling temperature. Keep low for deterministic plans. |
| `max_tokens` | integer | No (default `900`) | Max completion tokens for the plan response. |
| `execute` | boolean | No (default `false`) | If `true`, execute the plan and return a synthesised answer. If `false`, return the raw JSON plan for inspection. |
| `messages` | array of `{role, content}` | No | Full chat history; threaded into the synthesised answer when `execute=true`. |

---

## Output

**When `execute=false` (default):** A JSON object conforming to the `Plan` schema (see below).

**When `execute=true`:** A synthesised natural-language answer, identical to what `bamboo_answer` would return via its LLM planner fallback.

---

## Plan schema

```json
{
  "route": "FAST_PATH | PLAN | RETRIEVE",
  "confidence": 0.0–1.0,
  "tool_calls": [
    {
      "tool": "panda_task_status",
      "arguments": {"task_id": 12345678},
      "namespace": "atlas"
    }
  ],
  "retrieval_query": null,
  "reuse_policy": {
    "allow_final_answer_reuse": false,
    "allow_pattern_reuse": true,
    "requires_fresh_evidence": true
  },
  "explain": "Human-readable routing rationale."
}
```

`route` values:

- `FAST_PATH` — a single deterministic tool call; confidence typically ≥ 0.9.
- `PLAN` — one or more LLM-chosen tool calls.
- `RETRIEVE` — documentation retrieval via `panda_doc_search` + `panda_doc_bm25`.

---

## Key design notes

- The planner is invoked with temperature `0.0` to maximise determinism.
- The tool catalogue sent to the LLM is assembled from the core `TOOLS` registry plus any plugin entry points in the requested `namespaces`. This keeps prompts lean while still covering plugin tools.
- Plans are validated with Pydantic before execution. Invalid plans (malformed JSON, unknown tool names, out-of-range confidence) are rejected gracefully.
- In normal operation `bamboo_answer` calls the planner internally; external callers rarely need to use `bamboo_plan` directly. It is exposed as an MCP tool primarily for debugging and inspection via `execute=false`.

---

## See also

- [`bamboo_answer`](bamboo_answer.md) — primary entry point that calls this planner
- [`bamboo_last_evidence`](bamboo_last_evidence.md) — inspect evidence from the last plan execution
