"""Bamboo MCP HTTP server entry point.

Runs the Bamboo MCP server over the Streamable HTTP transport using uvicorn.
This is the production entry point for shared deployments where multiple users
connect to a single server process over the network.

For single-user local use, the stdio transport (``python -m bamboo.server``)
is simpler — it spawns a private server subprocess per TUI session.

Usage::

    # Minimal — binds to localhost:8000
    python -m bamboo.server_http

    # Bind to all interfaces on a custom port
    python -m bamboo.server_http --host 0.0.0.0 --port 9000

    # Or drive directly with uvicorn for advanced options (workers, TLS, etc.)
    uvicorn bamboo.entrypoints.http:app --host 0.0.0.0 --port 8000

Routes
------
``POST /mcp``
    MCP Streamable HTTP endpoint.  This is the URL to give to TUI / Streamlit
    clients via ``MCP_URL`` or ``--http-url``.

``GET /healthz``
    Liveness probe.  Returns ``200 ok`` (plain text) when the server process
    is alive.  No authentication required.  Use this for::

        curl http://localhost:8000/healthz
        # → ok

Environment variables
---------------------
``BAMBOO_HTTP_HOST``
    Bind host (default: ``127.0.0.1``).  Set to ``0.0.0.0`` to accept
    connections from other machines.

``BAMBOO_HTTP_PORT``
    Bind port (default: ``8000``).

``BAMBOO_HTTP_LOG_LEVEL``
    Uvicorn log level: ``debug``, ``info`` (default), ``warning``, ``error``.

``BAMBOO_MCP_TOKENS_FILE``
    Path to a Bearer token allowlist file.  When set, all ``/mcp`` requests
    must include a valid ``Authorization: Bearer <token>`` header.

``BAMBOO_MCP_TOKENS``
    Inline comma-separated ``client_id:token`` pairs (alternative to the file).

All standard Bamboo env vars (LLM keys, ``PANDA_BASE_URL``, etc.) must also
be set — source ``bamboo_env.sh`` before starting the server.
"""
from __future__ import annotations

import argparse
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the HTTP server.

    Returns:
        Configured argument parser.
    """
    p = argparse.ArgumentParser(
        prog="python -m bamboo.server_http",
        description=(
            "Run the Bamboo MCP server over the Streamable HTTP transport.\n\n"
            "MCP endpoint : http://<host>:<port>/mcp\n"
            "Health check  : http://<host>:<port>/healthz"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--host",
        default=os.getenv("BAMBOO_HTTP_HOST", "127.0.0.1"),
        help=(
            "Bind host (default: %(default)s). "
            "Set to 0.0.0.0 to accept remote connections. "
            "Override via BAMBOO_HTTP_HOST."
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("BAMBOO_HTTP_PORT", "8000")),
        help="Bind port (default: %(default)s). Override via BAMBOO_HTTP_PORT.",
    )
    p.add_argument(
        "--log-level",
        default=os.getenv("BAMBOO_HTTP_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error", "critical"],
        help="Uvicorn log level (default: %(default)s). Override via BAMBOO_HTTP_LOG_LEVEL.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of uvicorn worker processes (default: 1). "
            "Each worker maintains its own LLM pool and PanDA MCP session. "
            "Use 1 during initial testing."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Start the Bamboo HTTP server.

    Parses CLI arguments, prints a startup banner to stderr, then hands off
    to uvicorn.  Never returns on success (uvicorn runs until SIGTERM/SIGINT).

    Args:
        argv: Optional argument list (defaults to sys.argv[1:]).

    Returns:
        Process exit code (non-zero on configuration error).
    """
    args = _build_parser().parse_args(argv)

    try:
        import uvicorn  # type: ignore[import-untyped]
    except ImportError:
        print(
            "uvicorn is not installed.  Run:\n"
            "  pip install -r requirements-http.txt\n"
            "or:\n"
            "  pip install uvicorn",
            file=sys.stderr,
        )
        return 1

    # Import here so the version string is available even if the module
    # was already imported (avoids a circular import at top-level).
    from bamboo.config import Config  # noqa: PLC0415

    # Auth status for the banner.
    tokens_file = os.getenv("BAMBOO_MCP_TOKENS_FILE", "")
    tokens_inline = os.getenv("BAMBOO_MCP_TOKENS", "")
    auth_status = "enabled" if (tokens_file or tokens_inline) else "disabled (open access)"

    print(
        f"Bamboo MCP HTTP server  v{Config.SERVER_VERSION}\n"
        f"  MCP endpoint : http://{args.host}:{args.port}/mcp\n"
        f"  Health check : http://{args.host}:{args.port}/healthz\n"
        f"  Workers      : {args.workers}\n"
        f"  Auth         : {auth_status}\n"
        f"  Log level    : {args.log_level}",
        file=sys.stderr,
    )

    uvicorn.run(
        "bamboo.entrypoints.http:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        workers=args.workers if args.workers > 1 else None,
    )
    return 0  # unreachable in normal operation


if __name__ == "__main__":
    raise SystemExit(main())
