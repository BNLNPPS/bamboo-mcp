# Bamboo

**Bamboo** is a lightweight MCP-based runtime with a plugin architecture for
AI-assisted scientific tools, targeting PanDA/ATLAS and ePIC/EIC workflows.

LLMs are used for *summarisation and explanation*, not as sources of truth.
Structured evidence is always returned alongside natural-language answers.

> **Status (April 2026):** core infrastructure is stable. The ATLAS plugin
> includes `cric_query` for natural-language queries against the CRIC Computing
> Resource Information Catalogue. The CGSim plugin is newly added, providing
> documentation search (RAG + BM25) for the SimGrid-based CGSim distributed
> computing simulator. The current focus is multi-experiment support and
> orchestration using tool families and planning for complex multi-step prompts.

---

## Contributing

### Repository setup

The canonical repository is at **https://github.com/BNLNPPS/bamboo-mcp**. Development follows a standard fork-and-pull-request workflow.

**First-time setup:**

```bash
# Clone your fork
git clone https://github.com/<your-username>/bamboo-mcp.git
cd bamboo-mcp

# Add the canonical repo as upstream
git remote add upstream https://github.com/BNLNPPS/bamboo-mcp.git

# Verify
git remote -v
# origin    https://github.com/<your-username>/bamboo-mcp.git (fetch)
# origin    https://github.com/<your-username>/bamboo-mcp.git (push)
# upstream  https://github.com/BNLNPPS/bamboo-mcp.git (fetch)
# upstream  https://github.com/BNLNPPS/bamboo-mcp.git (push)
```

**Day-to-day workflow:**

```bash
# Push your changes to your fork
git push origin master

# Open a pull request from your fork to BNLNPPS/bamboo-mcp via GitHub

# Keep your fork in sync with upstream
git fetch upstream
git merge upstream/master
```

---

## Quick start

### 1. Create a virtual environment

```bash
python3 -m venv ~/Development/venv-bamboo
source ~/Development/venv-bamboo/bin/activate
```

### 2. Install the packages

```bash
# Core MCP server — required
pip install -r requirements.txt
pip install -e ./core

# ATLAS / PanDA plugin
pip install -e ./packages/askpanda_atlas

# ePIC / EIC plugin
pip install -e ./packages/askpanda_epic

# CGSim plugin
pip install -e ./packages/cgsim

# Root package — required for the TUI and Streamlit UI
pip install -e .

# TUI interface
pip install -r requirements-textual.txt

# RAG tools (ChromaDB vector search + BM25)
pip install -r requirements-rag.txt
```

Install **one** LLM provider (Mistral is the default):

```bash
pip install -r requirements-mistral.txt    # Mistral (default)
pip install -r requirements-openai.txt     # OpenAI / OpenAI-compatible
pip install -r requirements-anthropic.txt  # Anthropic
pip install -r requirements-gemini.txt     # Google Gemini
```

See [`docs/developer.md`](docs/developer.md) for the full list of optional
feature packages (tracing, Streamlit UI, etc.).

### 3. Configure environment

```bash
cp bamboo_env_example.sh bamboo_env.sh
# Edit bamboo_env.sh: set your API key and preferred provider/model
source bamboo_env.sh
```

The minimum you need to set:

```bash
export LLM_DEFAULT_PROVIDER="mistral"          # or openai, anthropic, gemini
export LLM_DEFAULT_MODEL="mistral-large-latest"
export MISTRAL_API_KEY="your-key-here"         # whichever provider you chose
```

### 4. Launch the TUI

```bash
# Alternate screen (recommended)
python interfaces/textual/chat.py --transport stdio --no-inline

# Inline — stays in terminal scrollback, easier copy/paste
python interfaces/textual/chat.py --transport stdio --inline
```

Type any question and press Enter.

---

## TUI slash commands

| Command | What it does |
|---|---|
| `/help` | Show all commands |
| `/task <id>` | Shorthand for "summarise task \<id\>" |
| `/job <id>` | Shorthand for "analyse failure of job \<id\>" |
| `/tracing` | Show timing and trace spans for the last request |
| `/costs` | Show estimated LLM token cost for the last request |
| `/json` | Show raw BigPanDA JSON for the last query |
| `/inspect` | Show compact evidence dict (what the LLM saw) for the last query |
| `/history` | Show turns currently held in context memory |
| `/fastpath on\|off` | Toggle deterministic fast-path routing (off → use LLM planner) |
| `/debug on\|off` | Toggle verbose tool call output |
| `/tools` | List tools registered on the server |
| `/links [N]` | List links from the last response; `/links N` opens link N in browser |
| `/clear` | Clear transcript, context memory, and HTTP cache |
| `/exit` | Quit |

`PageUp`/`PageDown` to scroll · `Ctrl+Q` to quit ·
Hold **Option** (macOS) or **Shift** (Linux/Windows) to select text with the mouse.

See [`docs/question-cheatsheet.md`](docs/question-cheatsheet.md) for ready-to-paste test questions.

---

## Key ideas

- **Tool-first** — tools are authoritative; LLMs only summarise their output
- **Plugin architecture** — experiment-specific logic lives in plugins, not in core
- **Narrow waist** — every tool returns `list[MCPContent]`; the MCP wire format is JSON-RPC 2.0
- **Context memory** — multi-turn chat history is maintained in the client and threaded into every LLM call
- **Deterministic routing** — `bamboo_answer` selects tools by regex, not by LLM

---

## Inspecting the server

```bash
# List available tools
python -m bamboo tools list

# Start the MCP server directly (stdio)
python -m bamboo.server

# Interactive inspection via MCP Inspector
npx @modelcontextprotocol/inspector python3 -m bamboo.server
```

---

## Documentation

| Doc | Contents |
|---|---|
| [`docs/developer.md`](docs/developer.md) | Full setup, editable installs, testing, linting |
| [`docs/http-server.md`](docs/http-server.md) | Running the HTTP server for shared/testbed deployments |
| [`docs/mcp.md`](docs/mcp.md) | MCP protocol, tool contracts, LLM roles, orchestration |
| [`docs/interfaces.md`](docs/interfaces.md) | TUI, Streamlit UI, HTTP transport, context memory |
| [`docs/plugins.md`](docs/plugins.md) | Writing and registering plugins |
| [`docs/jobs-database.md`](docs/jobs-database.md) | Live PanDA jobs DB queries — schema, examples, guard rules, routing |
| [`docs/cric-database.md`](docs/cric-database.md) | CRIC queuedata queries — schema, examples, guard rules, routing, disambiguation |
| [`docs/harvester-workers.md`](docs/harvester-workers.md) | Harvester pilot/worker counts — API, evidence structure, routing, time windows |
| [`docs/rag.md`](docs/rag.md) | RAG pipeline (ChromaDB + BM25) |
| [`docs/tracing.md`](docs/tracing.md) | Structured tracing and OpenTelemetry |
| [`docs/opensearch.md`](docs/opensearch.md) | OpenSearch integration — harvester timeseries, prompt logging, GDPR pseudonymisation |
| [`docs/security.md`](docs/security.md) | Authentication and token management |
| [`docs/question-cheatsheet.md`](docs/question-cheatsheet.md) | Ready-to-paste test questions for every tool and routing path |
| [`docs/tools/README-mcp_tools.md`](docs/tools/README-mcp_tools.md) | MCP tools reference — one document per tool, with inputs, outputs, routing, and design notes |

---

## Plugins

| Package | Status | Description |
|---|---|---|
| `askpanda_atlas` | Active | ATLAS / PanDA workflows |
| `askpanda_epic` | Active | ePIC / EIC experiment at BNL |
| `askpanda_verarubin` | Planned | Vera Rubin Observatory |
| `cgsim` | Active | CGSim / SimGrid distributed computing simulator |

### ATLAS plugin tools

| Entry point | Tool name | Description |
|---|---|---|
| `atlas.task_status` | `panda_task_status` | Task metadata and job-level detail |
| `atlas.log_analysis` | `panda_log_analysis` | Pilot/payload log download and failure classification |
| `atlas.doc_search` | `panda_doc_search` | Vector similarity search over ATLAS documentation |
| `atlas.doc_bm25` | `panda_doc_bm25` | BM25 keyword search over ATLAS documentation |
| `atlas.jobs_query` | `panda_jobs_query` | Natural language → SQL against the ingestion DuckDB |
| `atlas.harvester_workers` | `panda_harvester_workers` | Live Harvester pilot/worker counts |
| `atlas.harvester_timeseries` | `panda_harvester_timeseries` | Per-bucket pilot counts from OpenSearch (timeseries charts) |
| `atlas.panda_server_health` | `panda_server_health` | PanDA server liveness via PanDA MCP |
| `atlas.cric_query` | `cric_query` | Natural language → SQL against the CRIC queuedata DuckDB |
| `atlas.ui_manifest` | `atlas.ui_manifest` | TUI branding (banner, accent colour, display name) |

Set `BAMBOO_CHROMA_COLLECTION=atlas_docs` when running the ATLAS deployment to
point the doc tools at the ATLAS vector store.

### ePIC plugin tools

| Entry point | Tool name | Description |
|---|---|---|
| `epic.task_status` | `panda_task_status` | Task metadata and job-level detail |
| `epic.log_analysis` | `panda_log_analysis` | Pilot/payload log download and failure classification |
| `epic.doc_search` | `panda_doc_search` | Vector similarity search over ePIC documentation |
| `epic.doc_bm25` | `panda_doc_bm25` | BM25 keyword search over ePIC documentation |
| `epic.ui_manifest` | `epic.ui_manifest` | TUI branding (banner, accent colour, display name) |

Set `BAMBOO_CHROMA_COLLECTION=epic_docs` when running the ePIC deployment to
point the doc tools at the ePIC vector store.

### CGSim plugin tools

| Entry point | Tool name | Description |
|---|---|---|
| `cgsim.doc_search` | `cgsim_doc_search` | Vector similarity search over CGSim / SimGrid documentation |
| `cgsim.doc_bm25` | `cgsim_doc_bm25` | BM25 keyword search over CGSim / SimGrid documentation |
| `cgsim.ui_manifest` | `cgsim.ui_manifest` | TUI branding (banner, accent colour, display name) |

Set `BAMBOO_CHROMA_COLLECTION=cgsim_docs` and `ASKPANDA_PLUGIN=cgsim` when
running the CGSim deployment to point the doc tools at the CGSim vector store.
