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
| **Clear chat** | Clears conversation history and any cached diagrams. |
| **Tools registered on server** | Expandable list of all tools on the connected server. |
| **Developer access** | Password-protected superuser unlock. Only shown when `BAMBOO_SUPERUSER_PASSWORD` is configured. See [Superuser mode](#superuser-mode). |

## Response detail panels

After each assistant response, any Mermaid diagrams embedded in the response
are rendered inline first, followed by up to four expandable detail panels.

| Panel | Contents |
|---|---|
| **⏱ Tracing** | Span table with event type, tool name, duration, and detail (stdio only — see note below). |
| **💰 Estimated cost** | Per-call LLM token counts and USD cost estimate (stdio only). |
| **🔬 Evidence (inspect)** | Compact evidence dict from `bamboo_last_evidence`. Hidden for superuser-only tools in non-superuser sessions. |
| **📄 Raw JSON** | Verbatim BigPanDA API response from `bamboo_last_evidence`. Hidden for superuser-only tools in non-superuser sessions. |

> **Tracing in HTTP mode:** when connecting to a remote uvicorn server, the
> server writes trace spans to its own file — the Streamlit client cannot read
> them remotely. The Tracing and Cost panels will explain this and show the
> `tail` command to run on the server. Switch to **stdio transport** to get
> full tracing locally.

---

## Mermaid diagram rendering

When the LLM determines that a diagram would significantly clarify its answer —
for example, when explaining an algorithm, a state machine, or a data flow — it
may embed a Mermaid diagram in its response.

The Streamlit UI automatically:

1. Detects ` ```mermaid ``` ` fenced blocks in the LLM response.
2. Strips them from the stored response text (so conversation history stays
   clean and free of raw Mermaid syntax).
3. Renders each diagram inline using `streamlit-mermaid`, immediately after
   the text portion of the answer.

Multiple diagrams per response are supported; each is captioned "Diagram N"
when more than one is present.

**When diagrams appear:** the LLM is instructed by the `_MERMAID_GUIDANCE`
prompt rule, which is included in the synthesis prompt for tools that produce
algorithmic or flow-based answers (currently `pilot_code_query`). The LLM
decides autonomously whether a diagram adds value; it will not emit one for
simple status lookups or factual queries.

**Diagram types used:**

| LLM choice | Best for |
|---|---|
| `flowchart TD` | Algorithms, decision trees, data flows |
| `sequenceDiagram` | Protocols, call sequences between components |
| `stateDiagram-v2` | Job or process state machines |

**Dependency:** `streamlit-mermaid>=0.2.0` (included in `requirements-ui.txt`).
If the package is not installed, diagram blocks are stripped silently and the
text answer is shown without the visual.

**TUI behaviour:** the TUI does not render Mermaid diagrams. Diagram blocks are
stripped before replies are stored in history, so they never appear as raw syntax
in the terminal.

---

## Superuser mode

Superuser mode unlocks developer tools (currently `pilot_code_query`) for
authenticated sessions. It is an **in-session UI gate** — it does not change
which tools are registered on the MCP server (all tools are always available to
any MCP client), but it controls what the Streamlit interface exposes.

### Configuring

Set `BAMBOO_SUPERUSER_PASSWORD` in `bamboo_env.sh`:

```bash
export BAMBOO_SUPERUSER_PASSWORD="yourpassword"
```

When this variable is set, a **Developer access** section appears at the bottom
of the Streamlit sidebar. When it is unset or empty, the section is hidden and
superuser features are inaccessible from the UI.

### Authenticating (Streamlit)

1. Enter the password in the **Password** field in the **Developer access** section.
2. Click **Unlock**.
3. On success the section shows 🔓 **Superuser mode active** and a **Lock** button.
4. To re-lock, click **Lock**.

The lock state is held in `st.session_state["superuser"]` for the lifetime of
the browser session. Refreshing the page resets it to locked.

### Authenticating (TUI)

```
/superuser yourpassword
```

On success, the terminal prints:
```
🔓 Superuser mode unlocked. Developer tools are now active.
```

The unlock persists for the TUI session. Restart the TUI to reset.

### What changes when unlocked

- The Evidence (inspect) and Raw JSON expanders become visible for
  superuser-only tools such as `pilot_code_query`.
- Future developer tools will be gated here as well.

### Security model

This is a **UI-level gate only**, not server-side authentication. The password
is compared using `hmac.compare_digest` (constant-time) and never sent to the
MCP server. Superuser tools are registered unconditionally on the server and are
callable by any MCP client (e.g. Claude Desktop, MCP Inspector) regardless of
UI lock state.

For network-level authentication, see [`docs/security.md`](security.md).

### Environment variables

| Variable | Purpose |
|---|---|
| `BAMBOO_SUPERUSER_PASSWORD` | Plain-text password to unlock developer mode. Leave unset to hide superuser features entirely. |

---

## Streamlit environment variables

| Variable | Purpose |
|---|---|
| `MCP_URL` | Default server URL (overridden by the Server URL field) |
| `MCP_BEARER_TOKEN` | Default bearer token (overridden by the Bearer token field) |
| `ASKPANDA_PLUGIN` | Default plugin selection: `atlas`, `epic`, or `cgsim` (default: `atlas`) |
| `BAMBOO_FAST_PATH` | `0`/`off`/`false` to start with fast-path routing disabled (default: on) |
| `BAMBOO_HISTORY_TURNS` | Max conversation turns in context (default: 10) |
| `BAMBOO_MCP_CLIENT_TIMEOUT` | Timeout in seconds for MCP tool calls (default: 120) |
| `BAMBOO_SUPERUSER_PASSWORD` | Password for Developer access panel |

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

### --no-inline (Alternate Screen) — Recommended

Uses the terminal's alternate screen buffer (like `vim`, `less`).

- Full terminal control, proper resizing, reliable scrolling
- Clean exit restores previous shell screen
- Most stable mode

### --inline (Copy-Friendly Mode)

Renders inside the normal terminal scrollback.

- Easier mouse selection and copy/paste
- UI remains visible in shell history after exit
- Uses fixed inline height; slightly less robust

```bash
python interfaces/textual/chat.py --transport stdio --inline
export BAMBOO_TUI_INLINE_HEIGHT=60   # optional: control height
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
| `/links [N]` | List links from the last response; `/links N` opens link N in browser |
| `/superuser <pw>` | Unlock developer mode (requires `BAMBOO_SUPERUSER_PASSWORD`) |
| `/clear` | Clear transcript, context memory, and HTTP cache |
| `/exit` | Quit |

`PageUp`/`PageDown` to scroll · `Ctrl+Q` to quit ·
Hold **Option** (macOS) or **Shift** (Linux/Windows) to select text with the mouse.

---

## Context Memory (Multi-Turn Chat)

The Textual TUI maintains an in-memory conversation history that is sent
to the server on every question.  This enables follow-up questions such as:

- *"Tell me more about the brokerage part."* (after a RAG answer)
- *"What about the failed jobs?"* (after a task query)
- *"Is that the same error as last time?"* (after a log analysis)

History is capped at `BAMBOO_HISTORY_TURNS` user+assistant pairs (default 10).
Mermaid diagram blocks are stripped before replies are stored, so raw diagram
syntax never accumulates in context.

Use `/history` to inspect current context and `/clear` to reset.

---

## Pilot Charts (Textual TUI)

After every answer from `panda_harvester_workers`, two ASCII chart panels are
automatically appended:

**Status bar** — total pilot counts per status as a horizontal `█` bar chart.

**Timeseries** — per-bucket counts for the queried status over the requested
window. Requires `ASKPANDA_OPENSEARCH` and reachability to the OpenSearch cluster.

Use `/chart` to re-display the most recent chart.

### OpenSearch environment variables

| Variable | Purpose |
|---|---|
| `ASKPANDA_OPENSEARCH` | Password for OpenSearch HTTP Basic auth. Required for timeseries charts. |
| `ASKPANDA_OPENSEARCH_HOST` | OpenSearch cluster URL (default: `https://os-atlas.cern.ch/os`). |
| `ASKPANDA_OPENSEARCH_USER` | HTTP auth username (default: `pilot-monitor-agent`). |
| `ASKPANDA_OPENSEARCH_CA` | Path to CA certificate bundle (default: `/etc/pki/tls/certs/CERN-bundle.pem`). |
| `ASKPANDA_OPENSEARCH_VERIFY_CERTS` | `false` to disable TLS cert verification (local dev). |

---

Both interfaces use `interfaces/shared/mcp_client.py` for transport, connection
lifecycle, and subprocess isolation. The default MCP call timeout is 120 s;
override with `BAMBOO_MCP_CLIENT_TIMEOUT`.

---

# 3. AI Agent (CLI)

The agent runs a multi-step Reason → Act → Observe → Evaluate loop, calling
MCP tools iteratively until the evidence is sufficient to synthesise an answer.
Intended for complex, multi-hop queries.

See [`docs/agent.md`](agent.md) for full documentation and testing instructions.

```bash
python scripts/bamboo_agent.py \
    --transport http \
    --http-url http://localhost:8000/mcp \
    --question "Your question here" \
    --verbose
```

---

# Architecture Overview

```
User Interface (Streamlit / Textual / Agent CLI)
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
