#!/usr/bin/env sh
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
#
# Bamboo MCP container entrypoint.
#
# Dispatches to one of three runtime modes based on the first argument:
#
#   server  (default)
#       Start the Bamboo MCP HTTP server via uvicorn on 0.0.0.0:8000.
#       Suitable for Kubernetes deployments and Docker Compose.
#       Responds to SIGTERM for graceful shutdown.
#
#   tui
#       Launch the Textual TUI in an interactive terminal session.
#       Requires: docker run -it ...
#       The TUI connects to the HTTP server specified by MCP_URL, or
#       starts its own stdio sub-process if MCP_URL is unset.
#
#   stdio
#       Start the bamboo.server stdio transport.
#       Suitable for Claude Desktop integration:
#         docker run -i --env-file bamboo.env bamboo-mcp stdio
#
#   Any other argument is passed directly to the Python interpreter,
#   allowing one-off commands:
#       docker run bamboo-mcp python -m bamboo tools list

set -e

MODE="${1:-server}"

case "$MODE" in

    server)
        echo "[bamboo] Starting HTTP MCP server on 0.0.0.0:8000 ..."
        exec uvicorn bamboo.entrypoints.http:app \
            --host 0.0.0.0 \
            --port 8000 \
            --workers 1 \
            --log-level info
        ;;

    tui)
        echo "[bamboo] Launching Textual TUI ..."
        # The TUI reads MCP_URL to connect to a remote HTTP server.
        # If unset it falls back to spawning its own stdio sub-process.
        exec python -m interfaces.textual.chat
        ;;

    stdio)
        echo "[bamboo] Starting stdio MCP server ..." >&2
        exec python -m bamboo.server
        ;;

    *)
        # Pass-through: allows arbitrary commands, e.g.
        #   docker run bamboo-mcp python -m bamboo tools list
        exec "$@"
        ;;

esac
