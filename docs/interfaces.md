# Interfaces

Bamboo provides multiple user interfaces built on top of the same MCP server.
All interfaces are thin clients. Tool orchestration, routing, and LLM selection
are handled server-side by the Bamboo MCP server.

---

# 1. Streamlit Web UI

The Streamlit interface provides a browser-based chat experience suitable for
demos, collaborative workflows, and shared deployments.

## Install

```bash
pip install -r requirements-ui.txt
pip install -e .   # required so `interfaces` is importable
```

## Run

```bash
streamlit run interfaces/streamlit/chat.py
```

Streamlit opens a browser tab at `http://localhost:8501`.

## Sidebar controls

| Control | Description |
|---|---|
| **Transport** | `http` — connect to a running uvicorn server. `stdio` — Streamlit spawns its own server subprocess. |
| **Server URL** | MCP endpoint, e.g. `http://hostname:8000/mcp`. Reads `MCP_URL` env var as default. |
| **Bearer token** | Optional auth token. Reads `MCP_BEARER_TOKEN` env var as default. Sent as `Authorization: Bearer <token>`. |
| **Experiment / plugin** | Selects `atlas`, `epic`, or `cgsim`. Loads display name from `<plugin>.ui_manifest`. |
| **Fast-path routing** | ON (default) — deterministic routing for task/job/pilot questions. OFF — all questions go through the LLM planner. |
| **Reconnect** | Clears the cached MCP connection and reconnects. |
| **Clear chat** | Clears conversation history. |
| **Tools registered on server** | Expandable list of all tools on the connected server. |

## Response detail panels

After each assistant response, four expandable panels appear below the answer:

| Panel | Contents |
|---|---|
| **⏱ Tracing** | Span table with event type, tool name, duration, and detail (stdio only — see note below). |
| **💰 Estimated cost** | Per-call LLM token counts and USD cost estimate (stdio only). |
| **🔬 Evidence (inspect)** | Compact evidence dict from `bamboo_last_evidence` — task/job metadata, job counts, site breakdown. Populated only for task/job queries. |
| **📄 Raw JSON** | Verbatim BigPanDA API response from `bamboo_last_evidence`. Populated only for task/job queries. |

> **Tracing in HTTP mode:** when connecting to a remote uvicorn server, the
> server writes trace spans to its own file — the Streamlit client cannot read
> them remotely. The Tracing and Cost panels will explain this and show the
> `tail` command to run on the server. Switch to **stdio transport** to get
> full tracing locally.

## Environment variables

| Variable | Purpose |
|---|---|
| `MCP_URL` | Default server URL (overridden by the Server URL field) |
| `MCP_BEARER_TOKEN` | Default bearer token (overridden by the Bearer token field) |
| `ASKPANDA_PLUGIN` | Default plugin selection: `atlas`, `epic`, or `cgsim` (default: `atlas`) |
| `BAMBOO_FAST_PATH` | `0`/`off`/`false` to start with fast-path routing disabled (default: on). Togglable at runtime via the sidebar or `/fastpath`. |
| `BAMBOO_HISTORY_TURNS` | Max conversation turns in context (default: 10) |
| `BAMBOO_MCP_CLIENT_TIMEOUT` | Timeout in seconds for MCP tool calls (default: 120). Raise for large task fetches. |

## Connecting to a shared server (testbed)

```bash
export MCP_URL="http://your-server:8000/mcp"
export MCP_BEARER_TOKEN="your-token"
streamlit run interfaces/streamlit/chat.py
```

See [`docs/http-server.md`](http-server.md) for how to run the shared server.

---

# 2. Textual Terminal UI

The Textual interface provides a Copilot-style terminal experience.

It supports two transport modes:

- `stdio` (local MCP server subprocess)
- `http` (remote or local HTTP MCP endpoint)

## Run (stdio)

```bash
python interfaces/textual/chat.py --transport stdio --no-inline
```

## Run (HTTP)

```bash
python interfaces/textual/chat.py --transport http \
  --http-url http://localhost:8000/mcp \
  --token your-bearer-token \
  --no-inline
```

The `--token` flag sends `Authorization: Bearer <token>` on every request.
Alternatively set `MCP_BEARER_TOKEN` in the environment.

---

## Inline vs No-Inline (Textual)

The Textual TUI supports two rendering modes.

### --no-inline (Alternate Screen) — Recommended

Uses the terminal’s alternate screen buffer (like `vim`, `less`).

**Advantages:**

- Full terminal control
- Proper resizing
- Reliable scrolling
- Clean exit restores previous shell screen
- Most stable mode

Recommended for production terminal usage.

---

### --inline (Copy-Friendly Mode)

Renders inside the normal terminal scrollback.

**Advantages:**

- Easier mouse selection and copy/paste
- UI remains visible in shell history after exit

**Trade-offs:**

- Uses fixed inline height
- More sensitive to other processes writing to stdout
- Slightly less robust than alternate screen mode

Example:

```bash
python interfaces/textual/chat.py --transport stdio --inline
```

Optional: control inline height

```bash
export BAMBOO_TUI_INLINE_HEIGHT=60
```

---

## Slash Commands (Textual TUI)

| Command | Description |
|---|---|
| `/help` | Show all commands |
| `/tools` | List tools registered on the server |
| `/task <id>` | Shorthand for "summarise task \<id\>" |
| `/job <id>` | Shorthand for "analyse failure of job \<id\>" |
| `/json` | Show raw BigPanDA JSON for the last task/job query |
| `/inspect` | Show compact evidence dict (job counts, sites, errors) |
| `/chart` | Re-display the ASCII pilot chart for the last Harvester query |
| `/tracing` | Show timing and trace spans for the last request |
| `/costs` | Show estimated LLM token cost for the last request |
| `/history` | Show turns currently held in context memory |
| `/plugin <id>` | Switch active experiment plugin (e.g. `/plugin epic`) |
| `/fastpath on\|off` | Toggle deterministic fast-path routing (off → use LLM planner) |
| `/debug on\|off` | Toggle verbose tool call output |
| `/clear` | Clear transcript, context memory, and HTTP cache |
| `/exit` | Quit |

---

## Context Memory (Multi-Turn Chat)

The Textual TUI maintains an in-memory conversation history that is sent
to the server on every question.  This enables follow-up questions such as:

- *"Tell me more about the brokerage part."* (after a RAG answer)
- *"What about the failed jobs?"* (after a task query)
- *"Is that the same error as last time?"* (after a log analysis)

### How it works

- Each user question and its assistant reply are appended to `_history`.
- The full history is included as `messages` in every `bamboo_answer` call.
- The server extracts prior turns and injects them between the system prompt
  and the synthesised user message, so the LLM sees the conversation context.
- History uses **raw question/answer text**, not the synthesised prompts that
  embed task JSON or RAG excerpts — keeping token counts low.

### History cap

History is capped at `BAMBOO_HISTORY_TURNS` user+assistant pairs (default 10).
Older turns are discarded from the front of the list when the cap is reached.

```bash
export BAMBOO_HISTORY_TURNS=5   # keep only the last 5 question+answer pairs
```

### Resetting history

- `/clear` clears the transcript **and** resets the history.
- Restarting the TUI always starts with empty history (no persistence).

### Inspecting history

```
/history
```

Displays a table of the turns currently in context — role, character count,
and a truncated preview.  Useful for debugging follow-up resolution.

---

---

## Pilot Charts (Textual TUI)

After every answer that comes from `panda_harvester_workers`, two ASCII chart panels are automatically appended:

**Status bar** — shows total pilot counts per status (running, submitted, finished, failed, etc.) as a horizontal bar chart with proportional `█` bars, the time window, and the grand total.

**Timeseries** — shows per-bucket pilot counts for the status mentioned in the question (e.g. `finished` for *"how many pilots finished in the last 20 minutes?"*) over the requested time window, with the bucket interval derived automatically from the window duration. Requires `ASKPANDA_OPENSEARCH` to be set and the OpenSearch cluster to be reachable (CERN network / VPN).

The charts are only shown when `nworkers_by_status` contains more than one entry — a single-status result is not worth charting.

Use `/chart` to re-display the most recent pilot chart after scrolling past it.

### OpenSearch environment variables

| Variable | Purpose |
|---|---|
| `ASKPANDA_OPENSEARCH` | Password for OpenSearch HTTP Basic auth. Required for timeseries charts. |
| `ASKPANDA_OPENSEARCH_HOST` | OpenSearch cluster URL (default: `https://os-atlas.cern.ch/os`). |
| `ASKPANDA_OPENSEARCH_USER` | HTTP auth username (default: `pilot-monitor-agent`). |
| `ASKPANDA_OPENSEARCH_CA` | Path to CA certificate bundle (default: `/etc/pki/tls/certs/CERN-bundle.pem`). On macOS, copy from lxplus: `scp lxplus.cern.ch:/etc/pki/tls/certs/CERN-bundle.pem ~/cern-bundle.pem` |
| `ASKPANDA_OPENSEARCH_VERIFY_CERTS` | Set to `false` to disable TLS cert verification (local dev without CA bundle). |

The timeseries chart is silently skipped when OpenSearch is unreachable or the tool is not registered — it never disrupts the main answer.

---

Both Streamlit and Textual interfaces use:

```
interfaces/shared/mcp_client.py
```

Responsibilities:

- Support stdio and HTTP transports
- Manage connection lifecycle (lazy connect for TUI stability)
- Inherit environment variables (LLM configuration, plugin selection)
- Isolate subprocess output to avoid terminal corruption

### Timeout configuration

The synchronous `MCPClientSync._run()` wrapper has a default timeout of
**120 seconds** — large task status fetches from BigPanDA can take 60–90 s
for tasks with thousands of jobs.  Override with:

```bash
export BAMBOO_MCP_CLIENT_TIMEOUT=180   # seconds
```

---

# Architecture Overview

```
User Interface (Streamlit / Textual)
        ↓
Shared MCP Client
        ↓
Bamboo MCP Server
        ↓
Plugins + Tools (atlas, etc.)
        ↓
LLM Provider (OpenAI, Mistral, etc.)
```

All user interfaces are thin clients.
Server-side logic handles routing, planning, tool selection, and LLM execution.
