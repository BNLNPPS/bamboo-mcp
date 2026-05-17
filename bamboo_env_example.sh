#!/usr/bin/env bash
#
# Example environment configuration for AskPanDA LLM support
# Copy this file, remove `_example`, and fill in the API keys as needed.
# Remember to add this file to your .gitignore to avoid committing sensitive information.

########################################
# PANDA RELATED
########################################

export PANDA_BASE_URL="https://bigpanda.cern.ch"
export ASKPANDA_PANDA_RETRIES="2"
export ASKPANDA_PANDA_BACKOFF_SECONDS="0.8"

# Path to the DuckDB file written by the ingestion agent.
# Used by the panda_jobs_query tool (atlas.jobs_query).
# Defaults to "jobs.duckdb" in the current working directory if unset.
export PANDA_DUCKDB_PATH="jobs.duckdb"

# Optional: maximum rows returned by panda_jobs_query (default: 500).
# export PANDA_JOBS_QUERY_MAX_ROWS="500"

# Path to the CRIC queuedata DuckDB file written by the cric_agent.
# Used by the cric_query tool (atlas.cric_query).
# Defaults to "cric.duckdb" in the current working directory if unset.
export CRIC_DUCKDB_PATH="${HOME}/.askpanda/cric.duckdb"

# Optional: maximum rows returned by cric_query (default: 200).
# export CRIC_QUERY_MAX_ROWS="200"

########################################
# LLM PROFILE SELECTION
########################################

# Which profile names the selector will use
export LLM_DEFAULT_PROFILE="default"
export LLM_FAST_PROFILE="fast"
export LLM_REASONING_PROFILE="reasoning"

########################################
# DEFAULT PROFILE (used if nothing else matches)
########################################

export LLM_DEFAULT_PROVIDER="mistral"
export LLM_DEFAULT_MODEL="mistral-large-latest"

########################################
# FAST PROFILE (classification, routing, lightweight tasks)
########################################

export LLM_FAST_PROVIDER="mistral"
export LLM_FAST_MODEL="mistral-large-latest"

########################################
# REASONING PROFILE (log analysis, synthesis, RAG answers)
########################################

export LLM_REASONING_PROVIDER="mistral"
export LLM_REASONING_MODEL="mistral-large-latest"

########################################
# MISTRAL CONFIGURATION
########################################

# Required when using provider="mistral"
export MISTRAL_API_KEY=""

# Optional concurrency / retry tuning
export ASKPANDA_MISTRAL_CONCURRENCY="4"
export ASKPANDA_MISTRAL_RETRIES="3"
export ASKPANDA_MISTRAL_BACKOFF_SECONDS="1.0"

########################################
# OPENAI CONFIGURATION
########################################

# Required when using provider="openai" or provider="openai_compat".
# Install: pip install -r requirements-openai.txt
export OPENAI_API_KEY=""

# Optional tuning for the OpenAI provider.
# export ASKPANDA_OPENAI_CONCURRENCY="8"
# export ASKPANDA_OPENAI_RETRIES="3"
# export ASKPANDA_OPENAI_BACKOFF_SECONDS="1.0"

########################################
# ANTHROPIC CONFIGURATION
########################################

# Required when using provider="anthropic".
# Install: pip install -r requirements-anthropic.txt
export ANTHROPIC_API_KEY=""

# Optional tuning for the Anthropic provider.
# export ASKPANDA_ANTHROPIC_CONCURRENCY="4"
# export ASKPANDA_ANTHROPIC_RETRIES="3"
# export ASKPANDA_ANTHROPIC_BACKOFF_SECONDS="1.0"

########################################
# GEMINI CONFIGURATION
########################################

# Required when using provider="gemini".
# Install: pip install -r requirements-gemini.txt
export GEMINI_API_KEY=""

# Optional tuning for the Gemini provider.
# export ASKPANDA_GEMINI_CONCURRENCY="4"
# export ASKPANDA_GEMINI_RETRIES="3"
# export ASKPANDA_GEMINI_BACKOFF_SECONDS="1.0"

########################################
# OPENAI-COMPATIBLE ENDPOINT (Llama / Mistral via vLLM, Ollama, etc.)
########################################

# Required when using provider="openai_compat".
# Uses the same openai SDK as the OpenAI provider.
# Install: pip install -r requirements-openai.txt
export ASKPANDA_OPENAI_COMPAT_BASE_URL=""
export OPENAI_COMPAT_API_KEY=""

# Optional tuning.
# export ASKPANDA_OPENAI_COMPAT_CONCURRENCY="8"
# export ASKPANDA_OPENAI_COMPAT_RETRIES="3"
# export ASKPANDA_OPENAI_COMPAT_BACKOFF_SECONDS="1.0"

########################################
# PLUGIN SELECTION
########################################

# Active experiment plugin for the TUI and Streamlit interfaces.
# Controls which ui_manifest tool is loaded and which banner/accent is shown.
# Options: atlas | epic | cgsim   (default: atlas)
# Must be explicitly exported (not commented out) so that sourcing this file
# clears any stale value left in the shell from a previous cgsim/epic session.
export ASKPANDA_PLUGIN="atlas"

########################################
# RAG / CHROMADB (doc_search / doc_bm25 tools)
########################################

# Path to the ChromaDB persistent directory created by the ingestion script.
export BAMBOO_CHROMA_PATH="./chroma_db"

# Name of the ChromaDB collection to query.
# Each plugin has its own default collection name so multiple plugins can
# coexist in the same ChromaDB directory:
#   atlas_docs  — ATLAS / PanDA documentation  (askpanda_atlas default)
#   epic_docs   — ePIC / EIC documentation     (askpanda_epic default)
#   cgsim_docs  — CGSim / SimGrid documentation (cgsim default)
# Set this explicitly to override the plugin default.
export BAMBOO_CHROMA_COLLECTION="atlas_docs"

########################################
# DEBUG / SAFETY
########################################

# Fast-path routing: deterministic regex-based routing for task/job/pilot
# questions (faster, no LLM planner call).  Set to 0 to always use the LLM
# planner instead — useful when experimenting with new plugins like CGSim
# where the fast-path rules are not yet tuned for the domain.
# Options: 1 (on, default when unset) | 0 (off — use LLM planner)
export BAMBOO_FAST_PATH="0"

# Uncomment for verbose debug logs if needed
# export ASKPANDA_DEBUG="1"

########################################
# SUPERUSER / DEVELOPER MODE
########################################

# Plain-text password to unlock superuser mode in the Streamlit and TUI
# interfaces.  When set, a "Developer access" section appears in the
# Streamlit sidebar and the /superuser <password> command is active in
# the TUI.  Superuser mode exposes developer tools such as pilot_code_query.
# Leave unset (or empty) to disable superuser mode entirely.
# export BAMBOO_SUPERUSER_PASSWORD="changeme"

# Additional tool names to treat as superuser-gated (comma-separated).
# The defaults (code_query, atlas.code_query) are always included.
# Example: bamboo_code_query,atlas.bamboo_code_query
# export BAMBOO_SUPERUSER_TOOLS=""

# Additional routing-signal regex patterns for the pre-dispatch superuser
# guard (comma-separated, Python re syntax, case-insensitive).
# Any question matching one of these patterns is blocked until the user
# authenticates as a superuser.  The defaults cover *.py filenames and
# code-inspection verb + keyword combinations.
# Example: bamboo/.*\.py,core/bamboo/.*
# export BAMBOO_SUPERUSER_PATTERNS=""

########################################
# CODE QUERY (superuser tool)
########################################

# GitHub repository to fetch source files from.
# Format: owner/repo  (default: PanDAWMS/pilot3 — override for any other codebase)
# export BAMBOO_CODE_QUERY_REPO="PanDAWMS/pilot3"

# Branch or tag to fetch from (default: master).
# Example: export BAMBOO_CODE_QUERY_REPO="my-org/my-repo"
# export BAMBOO_CODE_QUERY_BRANCH="master"

########################################
# TRACING
########################################

# Set to 1 to enable structured request/response tracing.
# When BAMBOO_TRACE_FILE is set, spans are written only to that file (stderr
# is left clean — required when running under the Textual TUI).
# When BAMBOO_TRACE_FILE is not set, spans are written to stderr instead.
# See docs/tracing.md for the full event schema and jq recipes.
# export BAMBOO_TRACE="1"
# export BAMBOO_TRACE_FILE="/tmp/bamboo_trace.jsonl"

# OpenTelemetry export (optional — requires pip install -r requirements-otel.txt).
# When set, spans are also exported via OTLP/gRPC to the given endpoint
# (Jaeger, Grafana Tempo, Honeycomb, Datadog, etc.) as a parent/child tree.
# export BAMBOO_OTEL_ENDPOINT="http://localhost:4317"
# export BAMBOO_OTEL_SERVICE_NAME="bamboo"   # default: bamboo
# export BAMBOO_OTEL_INSECURE="1"            # set to 0 to enable TLS

# Set to 1 to redirect the server's stderr to /dev/null.
# The Textual TUI sets this automatically when launching via stdio transport.
# Useful if running the server as a background subprocess in other contexts.
# export BAMBOO_QUIET="1"

# ---------------------------------------------------------------------------
# Context memory (multi-turn chat history)
# ---------------------------------------------------------------------------
# Maximum number of user+assistant turn *pairs* to keep in context per session.
# Each pair = 1 user message + 1 assistant reply (2 messages total).
# Default: 10 pairs (20 messages).  Set lower to reduce LLM token usage.
# History is held in-memory in the TUI only; the server is always stateless.
# export BAMBOO_HISTORY_TURNS="10"

# Maximum tokens for LLM synthesis responses.
# Raise these for longer, more detailed answers — at the cost of higher latency.
# export BAMBOO_SYNTHESIS_MAX_TOKENS="2048"   # fresh questions (default: 2048)
# export BAMBOO_FOLLOWUP_MAX_TOKENS="600"     # follow-up expansions (default: 600)

echo "AskPanDA LLM environment variables loaded (example configuration)."

########################################
# HTTP SERVER (bamboo.entrypoints.http)
########################################

# Bind host for the HTTP MCP server (python -m bamboo.server_http).
# 127.0.0.1  → localhost only (default, safe for local development)
# 0.0.0.0    → all interfaces (required for remote clients)
# export BAMBOO_HTTP_HOST="127.0.0.1"

# Bind port (default: 8000)
# export BAMBOO_HTTP_PORT="8000"

# Uvicorn log level: debug | info | warning | error (default: info)
# export BAMBOO_HTTP_LOG_LEVEL="info"

# Bearer token auth — leave unset for open access (local/testbed use).
# Option A: tokens file (one entry per line, format: client_id: token)
# export BAMBOO_MCP_TOKENS_FILE="/etc/bamboo/tokens.txt"
# Option B: inline comma-separated client_id:token pairs
# export BAMBOO_MCP_TOKENS="alice:token-abc,bob:token-xyz"

########################################
# STREAMLIT / HTTP CLIENT
########################################

# Default MCP server URL for the Streamlit app and TUI in HTTP transport mode.
# export MCP_URL="http://localhost:8000/mcp"

# Bearer token for authenticating to a Bamboo HTTP server.
# export MCP_BEARER_TOKEN=""

# Timeout in seconds for MCP tool calls in the Streamlit sync client.
# Large task status fetches can take 60-90 s for tasks with thousands of jobs.
# export BAMBOO_MCP_CLIENT_TIMEOUT="120"
