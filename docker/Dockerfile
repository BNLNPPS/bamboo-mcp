# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# Authors
# - Paul Nilsson, paul.nilsson@cern.ch, 2026

# =============================================================================
# Bamboo MCP — multi-stage Docker image
#
# Build arguments
# ---------------
# INSTALL_ANTHROPIC   (default: false)  Install Anthropic SDK
# INSTALL_OPENAI      (default: false)  Install OpenAI SDK
# INSTALL_GEMINI      (default: true)   Install Google Generative AI SDK
# INSTALL_OTEL        (default: false)  Install OpenTelemetry exporter
# INSTALL_RAG         (default: false)  Install ChromaDB + BM25 for doc search
# INSTALL_CERN_CA     (default: true)   Append CERN Grid CA to certifi bundle
#
# Usage — HTTP server (Kubernetes / Docker Compose):
#   docker run --env-file bamboo.env -p 8000:8000 bamboo-mcp
#
# Usage — interactive TUI:
#   docker run -it --env-file bamboo.env bamboo-mcp tui
#
# Usage — stdio MCP server (Claude Desktop):
#   docker run -i --env-file bamboo.env bamboo-mcp stdio
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder
# Installs all Python packages into /opt/venv so the final stage can copy
# just the venv without build tools.
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ARG INSTALL_ANTHROPIC=false
ARG INSTALL_OPENAI=false
ARG INSTALL_GEMINI=true
ARG INSTALL_OTEL=false
ARG INSTALL_RAG=false
ARG INSTALL_CERN_CA=true

# Build-time system dependencies only (gcc needed by some Python packages).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        openssl \
    && rm -rf /var/lib/apt/lists/*

# Create the virtual environment that the final stage will reuse.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip/setuptools in the venv once, before any package installs.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy the full source tree.
WORKDIR /build
COPY . .

# ---- Core installs (always present) ----------------------------------------
# Install bamboo-core, the ATLAS plugin, and the root bamboo package.
# Editable installs are not appropriate for production images; use regular
# installs so the packages land cleanly inside the venv's site-packages.
RUN pip install --no-cache-dir ./core
RUN pip install --no-cache-dir ./packages/askpanda_atlas
RUN pip install --no-cache-dir .

# ---- Runtime dependencies (base) -------------------------------------------
RUN pip install --no-cache-dir \
        uvicorn[standard]>=0.29 \
        requests>=2.31.0 \
        pydantic>=2.0.0 \
        duckdb>=0.10 \
        sqlglot>=23.0

# ---- TUI (always installed — supports interactive use) ----------------------
RUN pip install --no-cache-dir -r requirements-textual.txt

# ---- LLM providers (build-arg controlled) -----------------------------------
# Gemini is the default provider so it is installed unless explicitly disabled.
RUN if [ "$INSTALL_GEMINI" = "true" ]; then \
        echo "Installing Gemini SDK..." && \
        pip install --no-cache-dir -r requirements-gemini.txt; \
    fi

RUN if [ "$INSTALL_ANTHROPIC" = "true" ]; then \
        echo "Installing Anthropic SDK..." && \
        pip install --no-cache-dir -r requirements-anthropic.txt; \
    fi

RUN if [ "$INSTALL_OPENAI" = "true" ]; then \
        echo "Installing OpenAI SDK..." && \
        pip install --no-cache-dir -r requirements-openai.txt; \
    fi

# ---- Optional extras --------------------------------------------------------
RUN if [ "$INSTALL_RAG" = "true" ]; then \
        echo "Installing RAG dependencies..." && \
        pip install --no-cache-dir -r requirements-rag.txt; \
    fi

RUN if [ "$INSTALL_OTEL" = "true" ]; then \
        echo "Installing OpenTelemetry exporter..." && \
        pip install --no-cache-dir -r requirements-otel.txt; \
    fi

# ---- CERN Grid CA -----------------------------------------------------------
# Append the CERN Root CA 2 and CERN Grid CA intermediate to the certifi
# bundle so that httpx (used by MCP) can verify the PanDA MCP server TLS
# certificate (signed by the CERN Grid CA) without disabling verification.
#
# The helper function _fetch_cern_cert() tries a list of known URL variants
# for each certificate, because CERN has changed the filenames on cafiles.cern.ch
# at least once (space-encoded names → plain names).  All attempts are best-effort:
# if every URL for a given cert returns a non-200 response the build continues
# without that cert, printing a clear warning.  The image is still valid in that
# case but PANDA_MCP_TLS_VERIFY=0 will be needed at runtime outside CERN.
#
# To supply the CAs yourself (e.g. air-gapped build), place PEM files in the
# build context at docker/certs/cern-root-ca.pem and docker/certs/cern-grid-ca.pem
# before running docker build — the script will use them instead of downloading.
RUN if [ "$INSTALL_CERN_CA" = "true" ]; then \
    CERTIFI_BUNDLE=$(python -c "import certifi; print(certifi.where())") && \
    echo "Certifi bundle: $CERTIFI_BUNDLE" && \
    \
    _append_pem() { \
        label="$1"; pemfile="$2"; \
        if [ -s "$pemfile" ]; then \
            echo "" >> "$CERTIFI_BUNDLE"; \
            echo "# $label" >> "$CERTIFI_BUNDLE"; \
            cat "$pemfile" >> "$CERTIFI_BUNDLE"; \
            echo "  [OK] Appended $label"; \
        else \
            echo "  [WARN] PEM file empty or missing for $label — skipping"; \
        fi; \
    }; \
    \
    _fetch_der_to_pem() { \
        label="$1"; outpem="$2"; shift 2; \
        for url in "$@"; do \
            echo "  Trying $url ..."; \
            if curl -fsSL --retry 2 --retry-delay 2 "$url" -o /tmp/_cern_cert.der 2>/dev/null; then \
                if openssl x509 -inform DER -in /tmp/_cern_cert.der -out "$outpem" 2>/dev/null; then \
                    echo "  [OK] Downloaded $label"; \
                    rm -f /tmp/_cern_cert.der; \
                    return 0; \
                fi; \
            fi; \
        done; \
        rm -f /tmp/_cern_cert.der; \
        echo "  [WARN] All URLs failed for $label"; \
        return 1; \
    }; \
    \
    echo "--- CERN Root CA 2 ---" && \
    if _fetch_der_to_pem "CERN Root CA 2" /tmp/cern-root.pem \
        "https://cafiles.cern.ch/cafiles/certificates/CERN_Root_Certification_Authority_2.crt" \
        "https://cafiles.cern.ch/cafiles/certificates/CERNRootCertificationAuthority2.crt" \
        "https://cafiles.cern.ch/cafiles/certificates/CERN%20Root%20Certification%20Authority%202.crt"; \
    then _append_pem "CERN Root Certification Authority 2" /tmp/cern-root.pem; fi && \
    \
    echo "--- CERN Grid CA 2 ---" && \
    if _fetch_der_to_pem "CERN Grid CA 2" /tmp/cern-grid.pem \
        "https://cafiles.cern.ch/cafiles/certificates/CERN_Grid_Certification_Authority_2.crt" \
        "https://cafiles.cern.ch/cafiles/certificates/CERNGridCertificationAuthority2.crt" \
        "https://cafiles.cern.ch/cafiles/certificates/CERN%20Grid%20Certification%20Authority%202.crt"; \
    then _append_pem "CERN Grid Certification Authority 2" /tmp/cern-grid.pem; fi && \
    \
    rm -f /tmp/cern-root.pem /tmp/cern-grid.pem; \
    echo "CERN CA step complete (see warnings above if any certs were skipped)"; \
else \
    echo "Skipping CERN CA installation (INSTALL_CERN_CA=false)"; \
fi

# -----------------------------------------------------------------------------
# Stage 2: final image
# Copies only the venv — no build tools, no source tree.
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS final

LABEL org.opencontainers.image.title="Bamboo MCP" \
      org.opencontainers.image.description="Lightweight MCP-based runtime for AI-assisted PanDA/ATLAS workflows" \
      org.opencontainers.image.authors="Paul Nilsson <paul.nilsson@cern.ch>" \
      org.opencontainers.image.source="https://github.com/PanDAWMS/bamboo" \
      org.opencontainers.image.licenses="Apache-2.0"

# Runtime system dependencies only.
# libgomp1   — required by some chromadb / numpy wheels on slim images
# ca-certificates — needed for TLS connections in general
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy the fully-populated venv from the builder stage.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the application source (needed for interfaces/ and scripts/ at runtime).
# We do NOT copy tests/ or docs/ to keep the image lean.
WORKDIR /app
COPY --chown=root:root core/ ./core/
COPY --chown=root:root packages/ ./packages/
COPY --chown=root:root interfaces/ ./interfaces/
COPY --chown=root:root scripts/ ./scripts/
COPY --chown=root:root bamboo_env_example.sh ./

# Re-install the packages in the final image so their entry points are
# registered against the /app source tree (editable-style path references
# from the builder stage pointed to /build, which no longer exists here).
RUN pip install --no-cache-dir --no-deps ./core ./packages/askpanda_atlas

# Create a non-root user for security (important for Kubernetes PSP/PSA).
RUN useradd --system --uid 1000 --gid 0 --home /app --shell /sbin/nologin bamboo

# Well-known mount points for external data volumes.
# These directories are created and owned by the bamboo user so that
# volume mounts work without requiring root or extra init containers.
RUN mkdir -p /data/jobs /data/cric /data/chroma /data/trace && \
    chown -R bamboo:0 /data && \
    chmod -R g=u /data

# Default environment — overridden at runtime via --env or --env-file.
# These match the defaults described in bamboo_env_example.sh.
ENV PANDA_BASE_URL="https://bigpanda.cern.ch" \
    PANDA_DUCKDB_PATH="/data/jobs/jobs.duckdb" \
    CRIC_DUCKDB_PATH="/data/cric/cric.db" \
    BAMBOO_CHROMA_PATH="/data/chroma" \
    BAMBOO_TRACE_FILE="/data/trace/bamboo_trace.jsonl" \
    LLM_DEFAULT_PROVIDER="gemini" \
    LLM_DEFAULT_MODEL="gemini-2.0-flash" \
    LLM_FAST_PROVIDER="gemini" \
    LLM_FAST_MODEL="gemini-2.0-flash" \
    LLM_REASONING_PROVIDER="gemini" \
    LLM_REASONING_MODEL="gemini-2.0-flash" \
    ASKPANDA_ENABLE_REAL_PANDA="1" \
    ASKPANDA_ENABLE_REAL_LLM="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

USER bamboo

EXPOSE 8000

# The entrypoint script dispatches to the correct mode based on CMD.
# CMD ["server"]  → uvicorn HTTP server  (default, for Kubernetes)
# CMD ["tui"]     → Textual TUI          (interactive: docker run -it ... tui)
# CMD ["stdio"]   → stdio MCP server     (Claude Desktop: docker run -i ...)
COPY --chown=bamboo:0 docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" \
        || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["server"]
