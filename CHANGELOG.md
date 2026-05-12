# Changelog

All notable changes to Bamboo are documented here.

---

## [Unreleased]

---

## 2026-05-12

### Fixed

- **`panda_jobs_query`: site-scoped queries returned 0 rows (bamboo_answer.py,
  jobs_query_impl.py, jobs_query_schema.py).** Two bugs combined to produce
  empty results for any site-scoped jobs query such as "Show me 10 jobs at BNL
  that failed with pilot error code 1324".

  Bug 1 (bamboo_answer.py): the solo `panda_jobs_query` fast-path never
  extracted the site name from the question and never populated the `queue`
  argument, even though the combined site-health path (panda_harvester_workers
  + panda_jobs_query) already did this correctly. The fix calls
  `_extract_site_from_question()` and sets `jobs_args["queue"] = site`
  in the fast-path, mirroring the site-health path.

  Bug 2 (jobs_query_schema.py): the SQL system prompt examples used exact
  equality (`_queue = 'BNL'`) for site filtering, but the actual `_queue`
  column values are full queue names such as `BNL_ATLAS_TIER1` and
  `BNL_ATLAS_TIER1-condor`. The LLM faithfully followed the examples and
  generated non-matching WHERE clauses. Fixed by updating all prompt examples
  and rules to use `ILIKE 'SITE%'` prefix matching, and by changing the queue
  hint appended in `jobs_query_impl.call()` from `(focus on queue: SITE)` to
  the explicit SQL instruction `(filter _queue ILIKE 'SITE%')`.

- **`panda_jobs_query`: site error counts were wrong when querying
  `errors_by_count` for site-scoped questions (jobs_query_schema.py,
  docs/jobs-database.md).** `errors_by_count` is populated from a separate
  BigPanDA summary endpoint and its `count` values do not match `COUNT(*)`
  on the `jobs` table. For example, "most common failures at BNL" via
  `errors_by_count` reported pilot:1150 as 7 jobs, while aggregating the
  `jobs` table directly found 42.

  Fixed by updating the SQL system prompt to always use `COUNT(*) GROUP BY`
  on the `jobs` table for site-scoped failure frequency questions, and to
  reserve `errors_by_count` only for global cross-queue rankings (no site
  filter). New example queries for "most common failures at SITE" and "top
  errors at SITE" now use `jobs` with `GROUP BY piloterrorcode, exeerrorcode`.
  The fallback schema description for `errors_by_count.count` is updated to
  document the separate-source semantics.

- **`panda_jobs_query`: "most common failures" questions routed to RAG instead
  of the jobs DB (bamboo_answer.py).** Phrases like "most common job failures
  at BNL" and "top failures at AGLT2" were not in `_JOBS_DB_SIGNALS` so they
  fell through to RAG retrieval, returning documentation text instead of live
  DB results. Added `"failures at"`, `"top failures"`, `"job failure"`,
  `"job failures"`, `"job error"`, `"job errors"`, `"common failure"`, and
  `"common error"` to both `_JOBS_DB_SIGNALS` and `_JOBS_DB_SPECIFIC_SIGNALS`.

---

## 2026-05-11

### Fixed
- ChromaDB RAG tools (panda_doc_search, panda_doc_bm25, and their ePIC and
  CGSim equivalents) now work on systems with SQLite < 3.35.0, such as CERN
  lxplus (AlmaLinux 9 / RHEL 9). A new compatibility shim
  (bamboo/tools/_sqlite_compat.py) monkey-patches pysqlite3-binary into
  sys.modules before ChromaDB is imported when the system SQLite is too old.
  The fix is a no-op on systems where the system SQLite is already sufficient.
  Add pysqlite3-binary to your environment: pip install -r requirements-rag.txt

## 2026-04-29

### Added

- **CGSim plugin (`packages/cgsim/`).** A new Bamboo MCP plugin for the
  CGSim / SimGrid distributed computing simulator. CGSim is a SimGrid-based
  framework for simulating large-scale computing grids such as the WLCG; it
  ingests historical PanDA job records for calibration and is designed to
  simulate infrastructures managed by PanDA.

  Entry points registered under `bamboo.tools`:

  | Entry point | Tool name | Description |
  |---|---|---|
  | `cgsim.doc_search` | `cgsim.doc_search` | ChromaDB vector similarity search over CGSim / SimGrid documentation |
  | `cgsim.doc_bm25` | `cgsim.doc_bm25` | BM25 keyword search over the same corpus |
  | `cgsim.ui_manifest` | `cgsim.ui_manifest` | TUI branding: block-letter banner, green accent, "Bamboo – CGSim" display name |

  The default ChromaDB collection name is `cgsim_docs`, distinct from
  `atlas_docs` and `epic_docs` so all three corpora can coexist in the same
  ChromaDB directory. Tool names use dot notation throughout (matching the
  entry point key), which is a requirement for all Bamboo plugins — using
  underscores in `get_definition()["name"]` causes "Unknown tool" errors
  because core overwrites the name with the entry point key.

  Future tools are stubbed and commented out in `pyproject.toml`:
  `cgsim.sim_query`, `cgsim.site_status`, `cgsim.calibration_results`,
  `cgsim.event_monitor` — all planned as read-only SQLite interfaces to the
  CGSim simulation output database.

- **`cgsim.sim_query` security model documented.** The planned SQLite tool
  will enforce read-only access at four independent layers: SQLite URI
  `mode=ro` flag, `PRAGMA query_only = ON`, sqlglot AST validation against a
  CGSim table allow-list, and local-only filesystem access via `CGSIM_DB_PATH`.
  This mirrors the security pattern of `panda_jobs_query` (DuckDB) but uses
  SQLite since that is what CGSim produces.

- **Plugin-aware synthesis prompts.** `bamboo_executor.py` now selects
  synthesis system prompts based on the active plugin (`ASKPANDA_PLUGIN`).
  Three CGSim-specific prompts were added: `_SYSTEM_RAG_CGSIM`,
  `_SYSTEM_RAG_NO_CONTEXT_CGSIM`, and `_SYSTEM_GENERIC_CGSIM`. These identify
  the assistant as Bamboo (not AskPanDA), state that CGSim/PanDA correlation
  questions are explicitly in scope, and instruct the LLM not to deflect
  cross-domain questions. The `plugin_id` parameter is now threaded through the
  full call chain: `bamboo_answer.call()` -> `_route()` ->
  `_build_deterministic_plan()` -> `execute_plan()` ->
  `_build_synthesis_prompt()` -> `_pick_synthesis_prompt()`.

- **Plugin-aware identity in `templates.py`.** `get_bamboo_system_prompt()`
  now accepts a `plugin_id` parameter and returns a plugin-appropriate identity
  string from `_PLUGIN_IDENTITY`. For CGSim the identity names the assistant
  Bamboo, describes the CGSim/SimGrid/PanDA domain, and explicitly welcomes
  PanDA/CGSim correlation questions. `llm_passthrough.py` reads
  `ASKPANDA_PLUGIN` and passes it through.

- **Plugin-aware doc tool routing.** `_PLUGIN_DOC_TOOLS` and
  `_DEFAULT_DOC_TOOLS` in `bamboo_executor.py` are now ordered lists (not
  sets) mapping plugin IDs to their doc tool pair, ensuring stable plan
  ordering (vector search always before BM25). `_build_deterministic_plan()`
  uses the plugin-appropriate doc tools for the fallback RAG route.

- **`BAMBOO_FAST_PATH` environment variable.** Fast-path routing can now be
  enabled or disabled at startup via the `BAMBOO_FAST_PATH` env var. Set to
  `0`, `off`, or `false` to start with the LLM planner handling all routing;
  any other value (or unset) leaves fast-path on. Both the Textual TUI and
  Streamlit interface read this at startup. The default in
  `bamboo_env_example.sh` is `0` (off), recommended for CGSim where fast-path
  intercepts are tuned for PanDA/ATLAS patterns.

- **`ASKPANDA_PLUGIN` environment variable documented.** Added to
  `bamboo_env_example.sh` with `atlas`, `epic`, and `cgsim` as documented
  choices. Added to env var tables in `docs/interfaces.md` and `CLAUDE.md`.

- **CGSim topic guard terms.** `topic_guard.py` now includes CGSim and
  SimGrid terms in `_ALLOW_TERMS` (`cgsim`, `simgrid`, `assignjob`,
  `getresourceinformation`, `onjobend`, `onsimulationend`, `netzone`,
  `calibration`, `job wall time`, `job queue time`, `simulation`, `simulator`,
  `computing grid`, `distributed computing`). The rejection message and LLM
  classifier system prompt were updated to name CGSim and SimGrid as in-scope
  domains.

- **Dynamic banner height in the Textual TUI.** `_render_banner()` and
  `_render_banner_placeholder()` now set the `#banner` container height
  programmatically after rendering using `len(banner_lines) + 4` (2 Panel
  borders + 2 CSS padding rows). This ensures the bottom border is never
  clipped regardless of plugin banner height. The CGSim block-letter banner is
  6 lines tall vs the 5-line ATLAS/ePIC banners, which triggered the bug.

- **`python -m bamboo.server_http` entry point** (`core/bamboo/server_http.py`).
  A dedicated HTTP server launcher that reads `BAMBOO_HTTP_HOST` (default
  `127.0.0.1`), `BAMBOO_HTTP_PORT` (default `8000`), and
  `BAMBOO_HTTP_LOG_LEVEL` (default `info`) from environment variables or CLI
  flags, and prints a startup banner to stderr showing the MCP endpoint URL,
  health check URL, worker count, and auth status. This replaces the need to
  memorise the `uvicorn bamboo.entrypoints.http:app` invocation.

- **`requirements-http.txt`** — `uvicorn>=0.29` and `starlette>=0.36`
  extracted as a named dependency group for the HTTP server transport.

- **`GET /healthz` documented.** The existing liveness endpoint in
  `bamboo.entrypoints.http` is now prominently documented in
  `docs/http-server.md`, `README.md`, `CLAUDE.md`, and `bamboo_env_example.sh`.
  Suitable for Kubernetes liveness/readiness probes (`httpGet: path: /healthz`),
  load balancer health checks, and `curl --fail` monitoring scripts.

- **Plugin-aware tool list filtering (`core/bamboo/core.py`).** The
  `list_tools` MCP handler now only exposes tools whose entry-point namespace
  matches the active plugin (`ASKPANDA_PLUGIN`). Core tools in the `TOOLS`
  dict (`bamboo_health`, `bamboo_answer`, etc.) are always included.

  Before this change, all installed plugins' tool descriptions were sent to the
  LLM on every call — an ATLAS user was paying token cost for CGSim tool
  descriptions and vice versa. With three plugins at roughly three tools each,
  this was approximately nine wasted tool descriptions per call.

  The filtering applies only to `list_tools`. `call_tool` is unaffected — all
  plugin tools remain callable regardless of `ASKPANDA_PLUGIN`. The namespace
  used for filtering is the part of the entry-point key before the first dot
  (`atlas.task_status` → namespace `atlas`). This means the namespace in the
  entry-point key must exactly match the value set in `ASKPANDA_PLUGIN`; if
  they differ the plugin's tools will never appear in `list_tools`.

- **`tests/test_plugin_tool_filter.py`** — 10 tests covering the filtering
  logic: correct tools included per plugin, cross-plugin tools excluded,
  unknown plugin returns empty, env var drives filter, default is `atlas`.

- **Streamlit plugin selectbox extended.** The sidebar plugin selector now
  includes `cgsim` alongside `atlas` and `epic`. The default index is derived
  dynamically from `ASKPANDA_PLUGIN` rather than a hardcoded position.

### Changed

- **`_PLUGIN_DOC_TOOLS` and `_DEFAULT_DOC_TOOLS` changed from sets to lists.**
  Python sets have no guaranteed iteration order; using `list(set)[0]` to pick
  doc tools produced non-deterministic plan ordering. Both constants are now
  ordered lists with vector search (`doc_search`) always at index 0 and BM25
  (`doc_bm25`) at index 1.

- **CGSim synthesis prompts updated to welcome PanDA/CGSim correlation.**
  The initial CGSim prompts instructed the LLM to avoid framing answers in
  terms of PanDA or ATLAS. This was over-cautious: CGSim ingests PanDA job
  records for calibration and users legitimately ask about the integration.
  All three CGSim synthesis prompts and the `_PLUGIN_IDENTITY["cgsim"]` string
  in `templates.py` now explicitly state that CGSim/PanDA correlation questions
  are in scope and should be answered directly.

- **`bamboo_env_example.sh` RAG section updated.** The default
  `BAMBOO_CHROMA_COLLECTION` value changed from `document_monitor_agent` to
  `atlas_docs`, matching the ATLAS plugin default. A new comment lists all
  three per-plugin defaults (`atlas_docs`, `epic_docs`, `cgsim_docs`).

### Fixed

- **All plugins' tool descriptions sent to LLM on every call (token waste).**
  `list_tools` was returning entry-point tools from all installed plugins
  regardless of `ASKPANDA_PLUGIN`. With ATLAS, ePIC, and CGSim all installed,
  every LLM call received approximately nine extra tool descriptions it would
  never use. Fixed by filtering in `list_tools` to the active plugin's
  namespace only.

- **"Unknown tool" errors for CGSim doc tools.** `get_definition()["name"]`
  in `cgsim/doc_rag.py` and `cgsim/doc_bm25.py` returned underscore names
  (`cgsim_doc_search`, `cgsim_doc_bm25`). Core overwrites the definition name
  with the entry point key (dot notation: `cgsim.doc_search`,
  `cgsim.doc_bm25`), so the LLM was trying to call the underscore names while
  the server only exposed the dot names. Fixed by aligning `get_definition()`
  to return dot-notation names matching the entry point keys.

- **PanDA/ATLAS framing in CGSim answers.** Synthesis prompts in
  `bamboo_executor.py` were hardcoded for PanDA/ATLAS regardless of the active
  plugin, causing the LLM to begin every CGSim answer with "in the context of
  PanDA/ATLAS workflows". Fixed by making `_build_synthesis_prompt()`,
  `_pick_synthesis_prompt()`, and `execute_plan()` plugin-aware, and by adding
  CGSim-specific prompt constants.

- **CGSim questions rejected by topic guard.** "How does CGSim work?" reached
  the LLM classifier stage and was denied because `cgsim` and `simgrid` were
  not in `_ALLOW_TERMS`. Fixed by adding a CGSim/SimGrid keyword section to
  the allow list.

- **Banner bottom border clipped for CGSim.** The `#banner` CSS rule had a
  hardcoded `height: 9` sized for the 5-line ATLAS/ePIC banners. The CGSim
  block-letter banner is 6 lines, causing the bottom border to be cut off.
  Fixed by computing the height dynamically in `_render_banner()`.

### New files

| File | Purpose |
|---|---|
| `packages/cgsim/cgsim/__init__.py` | CGSim plugin package |
| `packages/cgsim/cgsim/doc_rag.py` | `cgsim.doc_search` tool |
| `packages/cgsim/cgsim/doc_bm25.py` | `cgsim.doc_bm25` tool |
| `packages/cgsim/cgsim/ui_manifest.py` | `cgsim.ui_manifest` tool |
| `packages/cgsim/cgsim/banner.txt` | 6-line block-letter CGSim banner |
| `packages/cgsim/pyproject.toml` | Plugin entry points and metadata |
| `packages/cgsim/tests/test_cgsim_plugin.py` | 30 tests covering all three tools |
| `core/bamboo/server_http.py` | `python -m bamboo.server_http` entry point |
| `requirements-http.txt` | HTTP server dependencies (uvicorn, starlette) |
| `tests/test_prompt_templates.py` | 9 tests for plugin-aware system prompts |
| `tests/test_plugin_tool_filter.py` | 10 tests for plugin-aware tool list filtering |
| `docs/tools/cgsim_doc_search.md` | Per-tool reference for `cgsim.doc_search` |
| `docs/tools/cgsim_doc_bm25.md` | Per-tool reference for `cgsim.doc_bm25` |

---



## 2026-04-08

### Added Bamboo MCP can now be built and distributed
  as a Docker image, enabling deployment on Kubernetes and easy distribution
  to users who want a self-contained environment.

  The image supports three runtime modes selected via the container command:

  | Command | Mode | Use case |
  |---|---|---|
  | *(default)* `server` | HTTP MCP server on port 8000 | Kubernetes, Docker Compose |
  | `tui` | Interactive Textual TUI | `docker run -it` for end users |
  | `stdio` | stdio MCP server | Claude Desktop integration |

  The Textual TUI is always installed in the image so that interactive use
  requires no separate build variant.

- **Multi-stage `Dockerfile`** (`docker/Dockerfile`). A `builder` stage
  installs all packages into `/opt/venv`; the `final` stage copies only the
  venv (no build tools, no source tree). Key properties:

  - Base image: `python:3.11-slim`.
  - Non-root user `bamboo` (UID 1000) for Kubernetes PSA compliance.
  - Well-known volume mount points at `/data/jobs`, `/data/cric`,
    `/data/chroma`, and `/data/trace`.
  - Default LLM provider set to **Google Gemini** (`gemini-2.0-flash`) for
    all three profiles (default, fast, reasoning).
  - `HEALTHCHECK` via `GET /healthz` (the existing endpoint in
    `bamboo.entrypoints.http`).

- **Build arguments** for optional dependency groups:

  | Argument | Default | Controls |
  |---|---|---|
  | `INSTALL_GEMINI` | `true` | Google Generative AI SDK |
  | `INSTALL_ANTHROPIC` | `false` | Anthropic SDK |
  | `INSTALL_OPENAI` | `false` | OpenAI SDK |
  | `INSTALL_RAG` | `false` | ChromaDB + BM25 |
  | `INSTALL_OTEL` | `false` | OpenTelemetry OTLP exporter |
  | `INSTALL_CERN_CA` | `true` | CERN Grid CA appended to certifi |

- **CERN Grid CA baked into the image.** When `INSTALL_CERN_CA=true` (the
  default), the builder stage downloads the CERN Root CA 2 and CERN Grid CA 2
  from `cafiles.cern.ch`, converts them from DER to PEM, and appends both to
  the certifi bundle. This allows `httpx` to verify the PanDA MCP server
  (`aipanda120.cern.ch:8443`) without setting `PANDA_MCP_TLS_VERIFY=0`.
  If `cafiles.cern.ch` is unreachable during the build (air-gapped
  environment), the build continues and the CA step is silently skipped.

- **`docker/entrypoint.sh`** — dispatch script that maps the container
  command to the correct Python invocation (`uvicorn`, Textual TUI, or
  `bamboo.server` stdio). Unknown commands fall through to `exec "$@"` for
  one-off debugging (e.g. `docker run bamboo-mcp python -m bamboo tools list`).

- **`docker/docker-compose.yml`** — local development and integration testing
  configuration. Defines two services: `bamboo-server` (HTTP server, always
  started) and `bamboo-tui` (interactive TUI, under the `tui` Compose
  profile). The TUI service connects to the server via `MCP_URL`. Host paths
  for DuckDB files are configured via `PANDA_DUCKDB_HOST_PATH` and
  `CRIC_DUCKDB_HOST_PATH` environment variables.

- **`docker/kubernetes/bamboo-mcp.yaml`** — Kubernetes deployment skeleton
  including Deployment, Service, ConfigMap, and PersistentVolumeClaims for
  the jobs and CRIC DuckDB volumes. The manifest uses the existing `/healthz`
  endpoint for both liveness and readiness probes. Includes a note on
  sticky-session requirements when scaling beyond one replica (the HTTP server
  holds in-process MCP session state).

- **`docker/docs/docker.md`** — usage documentation covering build arguments,
  all three runtime modes, Docker Compose workflow, Kubernetes quick-start,
  the CERN CA setup, and a one-liner for converting `bamboo_env.sh` to a
  Docker-compatible `bamboo.env.docker` file.

- **`.dockerignore`** — excludes test artefacts, `__pycache__`, secrets
  (`bamboo_env.sh`, `*.env`), DuckDB/ChromaDB files, docs, and log files
  from the build context.

### New files

| File | Purpose |
|---|---|
| `docker/Dockerfile` | Multi-stage container image definition |
| `docker/entrypoint.sh` | Runtime mode dispatcher |
| `docker/docker-compose.yml` | Local development / integration testing |
| `docker/kubernetes/bamboo-mcp.yaml` | Kubernetes Deployment + Service + PVCs |
| `docker/docs/docker.md` | Usage documentation |
| `.dockerignore` | Build context filter |


---

## 2026-04-07

### Added

- **ASCII charts in the Textual TUI.** Pilot/Harvester answers now
  automatically display two chart panels below the text response.

  - **Status bar** (`pilot chart`) — horizontal bar chart of worker counts
    per status (running, submitted, finished, failed, etc.) with the time
    window and grand total. Rendered from the existing
    `panda_harvester_workers` snapshot evidence; no extra API call.

  - **Timeseries** (`pilot timeseries (<status>)`) — vertical bar chart
    showing Harvester worker update events per bucket over the query time
    window. Status and time window are extracted from the user's question
    automatically. Bars fill the full terminal width. Rendered via the new
    `panda_harvester_timeseries` tool (see below).

  > **Note on timeseries counts:** the timeseries shows *update events per
  > bucket* — workers that reported a status change in that window — not the
  > total number of active pilots. The OpenSearch index is a stream of change
  > events, not a snapshot. The status bar remains the authoritative source
  > for total pilot counts.

  Both charts are suppressed when only one status is present. The `/chart`
  slash command re-displays the most recent chart after scrolling. Charts
  degrade gracefully when OpenSearch is unavailable.

- **`panda_harvester_timeseries` MCP tool** (`atlas.harvester_timeseries`).
  Queries the OpenSearch `atlas_harvesterworkers-*` index for per-bucket
  worker counts. Bucket interval is derived automatically from the query
  window (≤30 min → `1m`, ≤3 h → `5m`, ≤12 h → `15m`, else `1h`).
  Requires `ASKPANDA_OPENSEARCH` and CERN network access (VPN or lxplus).
  Gracefully skipped when `opensearch-py`/`opensearch-dsl` are not installed.

- **New slash command `/chart`** — re-displays the ASCII pilot chart for
  the last Harvester query.

- **`docs/harvester-workers.md`** — reference documentation for the
  `panda_harvester_workers` tool.

- **New environment variables** for OpenSearch connectivity:

  | Variable | Purpose |
  |---|---|
  | `ASKPANDA_OPENSEARCH` | Password for OpenSearch HTTP Basic auth. Required for timeseries charts. |
  | `ASKPANDA_OPENSEARCH_HOST` | OpenSearch cluster URL (default: `https://os-atlas.cern.ch/os`) |
  | `ASKPANDA_OPENSEARCH_USER` | HTTP auth username (default: `pilot-monitor-agent`) |
  | `ASKPANDA_OPENSEARCH_CA` | Path to CA bundle (default: `/etc/pki/tls/certs/CERN-bundle.pem`) |
  | `ASKPANDA_OPENSEARCH_VERIFY_CERTS` | Set to `false` to disable TLS verification for local dev |

### Fixed

- **Linux TUI banner** — the banner panel was collapsing to zero height on
  Linux before the first render due to `height: auto` not measuring multiline
  content correctly before layout. Fixed with `height: 9; min-height: 9`.

### New files

| File | Location |
|---|---|
| `chart_utils.py` | `packages/askpanda_atlas/askpanda_atlas/` |
| `harvester_timeseries_impl.py` | `packages/askpanda_atlas/askpanda_atlas/` |
| `harvester_timeseries.py` | `packages/askpanda_atlas/askpanda_atlas/` |
| `test_chart_utils.py` | `packages/askpanda_atlas/tests/` |
| `test_harvester_timeseries.py` | `packages/askpanda_atlas/tests/` |
| `harvester-workers.md` | `docs/` |

### Dependencies

```bash
pip install opensearch-py opensearch-dsl
```

Required for timeseries charts. Optional — the TUI starts normally without
them and timeseries charts are silently skipped.

### Configuration

Add to `packages/askpanda_atlas/pyproject.toml`:

```toml
[project.entry-points."bamboo.tools"]
"atlas.harvester_timeseries" = "askpanda_atlas.harvester_timeseries:panda_harvester_timeseries_tool"
```

## Fix for read-only DuckDB connections

`cric_query_impl.py` and `jobs_query_impl.py` now open on-disk DuckDB files with `read_only=True` (via `database=` keyword), allowing the MCP query tools to coexist with the agent writer processes without triggering DuckDB's single-writer lock. In-memory connections (`:memory:`) remain read-write for tests. Three call sites updated: `_execute_query` in both files, `_probe_table_names` in `cric_query_impl`. Docstrings updated to document the policy. Flake8 clean.
