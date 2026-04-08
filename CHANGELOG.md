# Changelog

All notable changes to Bamboo are documented here.

---

## [Unreleased]

---

## 2026-04-08

### Added

- **Docker container support.** Bamboo MCP can now be built and distributed
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
