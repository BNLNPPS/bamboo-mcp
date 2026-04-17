# Docker deployment guide for Bamboo MCP

## Overview

The Bamboo MCP container supports three runtime modes:

| Mode | Command | Use case |
|---|---|---|
| `server` (default) | `docker run bamboo-mcp` | Kubernetes, Docker Compose — HTTP MCP server on port 8000 |
| `tui` | `docker run -it bamboo-mcp tui` | Interactive Textual TUI for end users |
| `stdio` | `docker run -i bamboo-mcp stdio` | Claude Desktop integration |

The default LLM provider is **Google Gemini** (`gemini-2.0-flash`). The CERN
Grid CA is appended to the certifi bundle at build time so that TLS
verification works for the PanDA MCP server at `aipanda120.cern.ch` without
disabling certificate checking.

---

## Building the image

```bash
# Default build (Gemini + CERN CA):
docker build -f docker/Dockerfile -t bamboo-mcp:latest .

# Add Anthropic and OpenAI as well:
docker build -f docker/Dockerfile \
  --build-arg INSTALL_ANTHROPIC=true \
  --build-arg INSTALL_OPENAI=true \
  -t bamboo-mcp:latest .

# Add RAG (ChromaDB) and OpenTelemetry:
docker build -f docker/Dockerfile \
  --build-arg INSTALL_RAG=true \
  --build-arg INSTALL_OTEL=true \
  -t bamboo-mcp:latest .

# Skip CERN CA (air-gapped build, or CERN lxplus where the system CA suffices):
docker build -f docker/Dockerfile \
  --build-arg INSTALL_CERN_CA=false \
  -t bamboo-mcp:latest .
```

### Build arguments

| Argument | Default | Description |
|---|---|---|
| `INSTALL_GEMINI` | `true` | Google Generative AI SDK (`requirements-gemini.txt`) |
| `INSTALL_ANTHROPIC` | `false` | Anthropic SDK (`requirements-anthropic.txt`) |
| `INSTALL_OPENAI` | `false` | OpenAI SDK / openai-compat (`requirements-openai.txt`) |
| `INSTALL_RAG` | `false` | ChromaDB + BM25 (`requirements-rag.txt`) |
| `INSTALL_OTEL` | `false` | OpenTelemetry OTLP exporter (`requirements-otel.txt`) |
| `INSTALL_CERN_CA` | `true` | Append CERN Root CA 2 + CERN Grid CA 2 to certifi |

The Textual TUI (`requirements-textual.txt`) is always installed so that
interactive use with `docker run -it ... tui` works without a rebuild.

---

## Environment variables

All configuration is passed via environment variables. The full reference is in
`bamboo_env_example.sh`. Key variables for container deployment:

```bash
# ---- LLM (required) --------------------------------------------------------
GEMINI_API_KEY=your-key-here

# ---- Data paths (match the volume mounts below) ----------------------------
PANDA_DUCKDB_PATH=/data/jobs/jobs.duckdb
CRIC_DUCKDB_PATH=/data/cric/cric.db

# ---- PanDA MCP (optional) --------------------------------------------------
PANDA_MCP_BASE_URL=https://aipanda120.cern.ch:8443/mcp/
# Outside CERN, the CERN CA is in the certifi bundle (INSTALL_CERN_CA=true),
# so TLS verification works. On lxplus the system CA is used automatically.
# Only set this if you genuinely cannot verify the certificate:
# PANDA_MCP_TLS_VERIFY=0

# ---- Tracing (optional) ----------------------------------------------------
BAMBOO_TRACE=1
BAMBOO_TRACE_FILE=/data/trace/bamboo_trace.jsonl
```

### Converting bamboo_env.sh for Docker

Docker Compose's `env_file` expects plain `KEY=VALUE` lines without `export`
and without comments. Convert your existing `bamboo_env.sh`:

```bash
grep '^export ' bamboo_env.sh \
  | sed 's/^export //' \
  | grep -v '^#' \
  > bamboo.env.docker
```

Then add `bamboo.env.docker` to your `.gitignore`.

---

## Running with Docker

### HTTP server (standalone)

```bash
# Create local data directories for the DuckDB files:
mkdir -p /tmp/bamboo-data/{jobs,cric,trace}
# Copy or symlink your DuckDB files:
cp ~/Development/tmp/jobs.duckdb /tmp/bamboo-data/jobs/
cp ~/Development/tmp/cric.db     /tmp/bamboo-data/cric/

docker run --rm \
  --env GEMINI_API_KEY=your-key \
  --env ASKPANDA_ENABLE_REAL_LLM=1 \
  --env ASKPANDA_ENABLE_REAL_PANDA=1 \
  -v /tmp/bamboo-data/jobs:/data/jobs:ro \
  -v /tmp/bamboo-data/cric:/data/cric:ro \
  -v /tmp/bamboo-data/trace:/data/trace \
  -p 8000:8000 \
  bamboo-mcp:latest
```

### Interactive TUI

```bash
docker run --rm -it \
  --env GEMINI_API_KEY=your-key \
  --env ASKPANDA_ENABLE_REAL_LLM=1 \
  --env MCP_URL=http://localhost:8000/mcp \
  bamboo-mcp:latest tui
```

If the server is running in another container (e.g. via Docker Compose),
use the container network name instead of `localhost`:

```bash
--env MCP_URL=http://bamboo-server:8000/mcp
```

### stdio server (Claude Desktop)

Add this to your Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bamboo": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env", "GEMINI_API_KEY=your-key",
        "--env", "ASKPANDA_ENABLE_REAL_LLM=1",
        "--env", "ASKPANDA_ENABLE_REAL_PANDA=1",
        "-v", "/path/to/jobs.duckdb:/data/jobs/jobs.duckdb:ro",
        "-v", "/path/to/cric.db:/data/cric/cric.db:ro",
        "bamboo-mcp:latest",
        "stdio"
      ]
    }
  }
}
```

---

## Running with Docker Compose

```bash
# 1. Create your env file (see "Converting bamboo_env.sh" above):
grep '^export ' bamboo_env.sh | sed 's/^export //' > bamboo.env.docker

# 2. Set host paths for your DuckDB files (or accept the /tmp defaults):
export PANDA_DUCKDB_HOST_PATH=/path/to/your/jobs-directory
export CRIC_DUCKDB_HOST_PATH=/path/to/your/cric-directory

# 3. Start the HTTP server:
docker compose -f docker/docker-compose.yml up bamboo-server

# 4. In another terminal, launch the TUI (connects to the running server):
docker compose -f docker/docker-compose.yml run --rm bamboo-tui
```

---

## Kubernetes

See `docker/kubernetes/bamboo-mcp.yaml` for a complete skeleton including
Deployment, Service, ConfigMap, and PersistentVolumeClaims.

Quick start:

```bash
# Create the API key secret:
kubectl create secret generic bamboo-llm-secrets \
  --from-literal=GEMINI_API_KEY=your-key

# Apply the manifest (adjust namespace, image, storage class first):
kubectl apply -f docker/kubernetes/bamboo-mcp.yaml

# Watch the rollout:
kubectl rollout status deployment/bamboo-mcp

# Tail logs:
kubectl logs -f deployment/bamboo-mcp
```

### Session affinity note

The HTTP server holds in-process MCP session state. If you scale to more
than one replica, configure your Ingress with session affinity (sticky
sessions) so that follow-up requests from the same client land on the same
pod. Example for nginx-ingress:

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/affinity: "cookie"
    nginx.ingress.kubernetes.io/session-cookie-name: "bamboo-session"
    nginx.ingress.kubernetes.io/session-cookie-expires: "172800"
```

---

## Health check

The server exposes `GET /healthz` returning HTTP 200 `ok`. This is used by
the Docker `HEALTHCHECK` directive, the Compose `healthcheck`, and the
Kubernetes liveness/readiness probes.

```bash
curl http://localhost:8000/healthz
# ok
```

---

## CERN Grid CA

When `INSTALL_CERN_CA=true` (the default), the build stage tries to download:

- **CERN Root Certification Authority 2** — root CA
- **CERN Grid Certification Authority 2** — intermediate CA

Both are fetched in DER format from `cafiles.cern.ch`, converted to PEM, and
appended to the certifi CA bundle inside the image. The build tries several
known filename variants because CERN has changed the filenames on that server
before (e.g. spaces in filenames vs. underscores vs. URL-encoded spaces).

**The CA step is non-fatal.** If every URL variant returns a 404 or the host
is unreachable, the build completes successfully and prints a warning. The
image will work normally on CERN machines (system CA store is used) but will
need `PANDA_MCP_TLS_VERIFY=0` outside CERN for the PanDA MCP connection.

### If the download keeps failing: supply PEM files locally

Place your own PEM copies in the build context before running `docker build`:

```bash
# On lxplus or any CERN machine:
cp /etc/pki/tls/certs/CERN-bundle.pem docker/certs/cern-root-ca.pem
cp /etc/pki/tls/certs/CERN-bundle.pem docker/certs/cern-grid-ca.pem
```

Or extract directly from the live server:

```bash
openssl s_client -connect aipanda120.cern.ch:8443 -showcerts 2>/dev/null </dev/null \
  | awk '/BEGIN CERTIFICATE/,/END CERTIFICATE/' \
  > docker/certs/cern-grid-ca.pem
```

The build script checks for these files before attempting downloads and uses
them instead if they exist.

### Finding the correct current URL

The exact filenames on `cafiles.cern.ch` can be confirmed from a CERN machine:

```bash
curl -s https://cafiles.cern.ch/cafiles/certificates/ | grep -i 'cern.*\.crt'
```

If the filenames have changed again and all variants in the Dockerfile fail,
set `INSTALL_CERN_CA=false` and supply the PEM files manually as above.

On **lxplus** and CERN machines the CERN CA is already in the system store;
the certifi override is not needed but is harmless.
