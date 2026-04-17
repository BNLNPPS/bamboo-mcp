# `panda_server_health`

**Package:** `askpanda_atlas`
**Module:** `askpanda_atlas.panda_server_health`
**Type:** Operational data — PanDA server liveness

---

## Purpose

`panda_server_health` checks whether the PanDA server is alive and responding by calling the `is_alive` tool on the external PanDA MCP server. Use it for questions like:

- "Is the PanDA server alive?"
- "Is PanDA OK?"
- "Is the PanDA server running?"
- "What is the PanDA server status?"

This tool checks the PanDA server itself, not the Bamboo server. For Bamboo server health, use [`bamboo_health`](bamboo_health.md).

---

## Inputs

None. The tool takes no arguments.

---

## Data source

Delegates to the `is_alive` tool on the PanDA MCP server session, registered under the `"panda"` server name in the process-wide `MCPCaller`. The PanDA MCP session must be started at Bamboo server startup via `panda_mcp_session.run_panda_mcp_session()`. If no session is registered the tool returns a graceful error — it never raises.

---

## Output

A JSON-serialised evidence dict with the following keys:

| Key | Description |
|---|---|
| `alive` | Boolean — whether the PanDA server reported itself alive. |
| `raw_response` | The raw text response from the `is_alive` tool. |
| `error` | Error string if the delegate call failed, otherwise absent. |

---

## Liveness parsing

The `is_alive` response is parsed conservatively:
- An explicit `"True"` string or JSON `{"alive": true}` → alive.
- An explicit `"False"` string or JSON `{"alive": false}` → not alive.
- Any non-empty, unparseable response → treated as alive (conservative).
- Empty response → treated as not alive.

---

## Routing

`bamboo_answer` routes to this tool via the highest-priority fast-path intercept, before any site-health or pilot checks. The routing matches `_PANDA_HEALTH_RE`, which covers variations like "is panda alive", "is the panda server ok", "panda server status", and "panda server heartbeat" — while deliberately not matching task/job questions that merely mention "panda" incidentally.

---

## See also

- [`bamboo_health`](bamboo_health.md) — Bamboo MCP server health (version, LLM config)
- [`panda_harvester_workers`](panda_harvester_workers.md) — pilot activity at specific sites
