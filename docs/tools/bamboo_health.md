# `bamboo_health`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.health`
**Type:** Infrastructure — server diagnostics

---

## Purpose

`bamboo_health` returns a brief status payload confirming the Bamboo MCP server is running. It reports the server name, version, configured LLM provider, and which optional integrations (real PanDA API, real LLM, RAG) are active. Use it to confirm the server is reachable, to check which LLM is selected, or to verify configuration after deployment.

The TUI calls this tool automatically at startup to display the LLM selection in the connection message.

---

## Inputs

None. The tool takes no arguments.

---

## Output

A plain-text status block, for example:

```
Bamboo MCP Server OK
- name: AskPanDA
- version: 1.0.1
- ENABLE_REAL_PANDA: True
- ENABLE_REAL_LLM: True
- llm_info: provider=mistral model=mistral-large-latest
```

| Field | Description |
|---|---|
| `name` | Server name from `Config.SERVER_NAME`. |
| `version` | Package version from `Config.SERVER_VERSION`. |
| `ENABLE_REAL_PANDA` | Whether live BigPanDA API calls are enabled. |
| `ENABLE_REAL_LLM` | Whether a real LLM provider is configured. |
| `llm_info` | Active LLM provider and model string, or `"not configured"`. |

---

## Key design notes

- `Config` is instantiated at call time (not import time) so environment variables changed after server startup are reflected correctly.
- `llm_info` is fetched lazily from the LLM selector; if the selector is not yet initialised it returns `"not configured"` without raising.

---

## See also

- [`bamboo_llm_answer`](bamboo_llm_answer.md) — direct LLM passthrough, also useful for connectivity testing
- [`panda_server_health`](panda_server_health.md) — checks PanDA server liveness (distinct from Bamboo server health)
