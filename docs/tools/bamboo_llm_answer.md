# `bamboo_llm_answer`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.llm_passthrough`
**Type:** Infrastructure — LLM passthrough

---

## Purpose

`bamboo_llm_answer` forwards a question or full conversation history directly to the configured default LLM and returns the raw model response. It does not call any data tools or apply routing logic.

Primary use cases:

- Sanity-checking that LLM configuration (API keys, provider adapters, networking) works end-to-end through MCP.
- Open-ended questions or follow-ups that require no live PanDA data.
- An explicit "bypass reasoning engine" path when orchestration overhead is unwanted.

In normal operation this tool is called internally by `bamboo_executor` as the final synthesis step after data tools have fetched evidence. It is exposed as an MCP tool for direct access and testing.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `question` | string | One of `question` or `messages` | User question; wrapped as a user message. |
| `messages` | array of `{role, content}` | One of `question` or `messages` | Full chat history sent to the LLM as-is, prepended with the Bamboo system prompt. |
| `temperature` | number | No (default `0.2`) | Sampling temperature. |
| `max_tokens` | integer | No | Maximum completion tokens. If omitted the provider default applies. |

---

## Output

A single text content block containing the model's raw text response.

---

## System prompt

All calls prepend the Bamboo system prompt retrieved from `get_bamboo_system_prompt()`. The current prompt instructs the model to behave as AskPanDA and to prefer tool calls for factual data — though since this is a passthrough tool, no further tool calls are made.

---

## LLM selection

The tool always uses the **default LLM profile** from the registry (`selector.default_profile`). There is no mechanism to select a non-default profile at call time.

---

## Key design notes

- Token usage (input and output tokens) is recorded in the tracing spans so `/costs` in the TUI can account for passthrough calls alongside executor synthesis calls.
- Errors from the LLM provider (`LLMError`) are propagated as exceptions here; `bamboo_answer` catches them and returns a user-friendly error message.

---

## See also

- [`bamboo_health`](bamboo_health.md) — checks server and LLM configuration without sending a prompt
- [`bamboo_answer`](bamboo_answer.md) — full orchestration entry point that uses this tool internally for synthesis
