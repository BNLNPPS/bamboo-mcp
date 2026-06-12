#!/usr/bin/env python3
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

"""Bamboo AI Agent — standalone CLI.

Runs the :class:`~interfaces.agent.agent.BambooAgent` ReAct loop against a
live Bamboo MCP server and prints the synthesised answer.  Supports both
single-shot and interactive (REPL) modes.

Transport options mirror the existing TUI and Streamlit interfaces:

* **HTTP** (default, production): ``--transport http --http-url <url>``
* **STDIO** (development): ``--transport stdio``

Usage examples::

    # Single question via HTTP transport
    python scripts/bamboo_agent.py \\
        --question "Which ATLAS sites had the highest pilot failure rate this week?" \\
        --transport http \\
        --http-url http://localhost:8000/mcp \\
        --verbose

    # Pipe a question through stdin
    echo "Summarise ATLAS job failures at BNL in the last 24 hours" | \\
        python scripts/bamboo_agent.py --transport http

    # Interactive REPL mode
    python scripts/bamboo_agent.py --transport http --interactive

    # Dump full AgentResult as JSON (for scripting / notebooks)
    python scripts/bamboo_agent.py \\
        --question "What is the average job stagein time at SLAC this week?" \\
        --output-json

Environment variables
---------------------
``BAMBOO_AGENT_MAX_STEPS``     Override default max reasoning steps (6).
``BAMBOO_AGENT_CONFIDENCE``    Override default sufficiency threshold (0.80).
``BAMBOO_AGENT_MAX_TOKENS``    Override default synthesis token budget (2048).
``BAMBOO_MCP_HTTP_URL``        Default HTTP endpoint (overridden by --http-url).
``BAMBOO_MCP_TOKEN``           Bearer token for authenticated HTTP endpoints.

Exit codes
----------
0  Answer produced successfully (possibly truncated).
1  Connection or initialisation error.
2  Runtime error during the agent loop.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path bootstrap — makes repo-root packages importable when this script is
# run directly without ``pip install -e .``.
# The script lives in scripts/, so the repo root is one level up.
# ---------------------------------------------------------------------------
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from interfaces.agent.agent import AgentResult, BambooAgent  # noqa: E402
from interfaces.shared.mcp_client import MCPAsyncClient, MCPServerConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HTTP_URL: str = os.getenv("BAMBOO_MCP_HTTP_URL", "http://localhost:8000/mcp")
_REPL_PROMPT: str = "agent> "
_REPL_QUIT_CMDS: frozenset[str] = frozenset({"exit", "quit", "/exit", "/quit", "q"})


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_result_text(result: AgentResult, *, verbose: bool) -> str:
    """Format an :class:`AgentResult` as human-readable text.

    Args:
        result: The completed agent result.
        verbose: When ``True``, include the full step trace.

    Returns:
        Formatted string ready to print.
    """
    lines: list[str] = []

    if verbose and result.steps:
        lines.append("═" * 60)
        lines.append("REASONING TRACE")
        lines.append("═" * 60)
        for step in result.steps:
            lines.append(f"\n┌── Step {step.step_index}")
            if step.thought:
                lines.append(f"│  Thought: {step.thought}")
            if step.tool_name:
                args_str = json.dumps(step.tool_args, ensure_ascii=False)
                lines.append(f"│  Action:  {step.tool_name}({args_str})")
            if step.observation is not None:
                obs_preview = step.observation[:300].replace("\n", " ")
                lines.append(f"│  Obs:     {obs_preview}{'…' if len(step.observation) > 300 else ''}")
            if step.eval_sufficient is not None:
                lines.append(
                    f"│  Eval:    sufficient={step.eval_sufficient}  "
                    f"confidence={step.eval_confidence:.2f}"
                )
            lines.append("└" + "─" * 58)

    lines.append("")
    lines.append("═" * 60)
    lines.append("ANSWER")
    lines.append("═" * 60)
    lines.append(result.answer)
    lines.append("")

    meta_parts = [
        f"steps={len(result.steps)}",
        f"confidence={result.confidence:.2f}",
    ]
    if result.tool_names_used:
        meta_parts.append(f"tools=[{', '.join(result.tool_names_used)}]")
    if result.truncated:
        meta_parts.append("TRUNCATED")
    lines.append(f"({', '.join(meta_parts)})")

    return "\n".join(lines)


def _result_to_dict(result: AgentResult) -> dict[str, Any]:
    """Serialise an :class:`AgentResult` to a plain dict for JSON output.

    Args:
        result: The completed agent result.

    Returns:
        JSON-serialisable dict.
    """
    return {
        "answer": result.answer,
        "confidence": result.confidence,
        "truncated": result.truncated,
        "tool_names_used": result.tool_names_used,
        "steps": [dataclasses.asdict(s) for s in result.steps],
    }


# ---------------------------------------------------------------------------
# Core async runner
# ---------------------------------------------------------------------------


async def _run_question(
    question: str,
    *,
    cfg: MCPServerConfig,
    max_steps: int,
    confidence: float,
    max_tokens: int,
    verbose: bool,
    output_json: bool,
) -> int:
    """Connect to the MCP server, run the agent on ``question``, and print output.

    Args:
        question: The user's question string.
        cfg: MCP server connection configuration.
        max_steps: Maximum agent reasoning steps.
        confidence: Evaluator confidence threshold.
        max_tokens: Token budget for the synthesis call.
        verbose: Print full step trace.
        output_json: Emit JSON output instead of formatted text.

    Returns:
        Process exit code (0 = success, 2 = runtime error).
    """
    client = MCPAsyncClient(cfg)
    try:
        await client.connect()
        agent = BambooAgent(
            client,
            max_steps=max_steps,
            confidence_threshold=confidence,
            max_tokens=max_tokens,
            verbose=verbose,
        )
        result = await agent.run(question)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"[ERROR] Agent run failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.aclose()

    if output_json:
        print(json.dumps(_result_to_dict(result), indent=2, ensure_ascii=False))
    else:
        print(_format_result_text(result, verbose=verbose))

    return 0


async def _run_interactive(
    *,
    cfg: MCPServerConfig,
    max_steps: int,
    confidence: float,
    max_tokens: int,
    verbose: bool,
    output_json: bool,
) -> int:
    """Run an interactive REPL loop.

    Opens a single persistent MCP connection and processes questions until the
    user sends a quit command or EOF (Ctrl+D).

    Args:
        cfg: MCP server connection configuration.
        max_steps: Maximum agent reasoning steps per question.
        confidence: Evaluator confidence threshold.
        max_tokens: Token budget for the synthesis call.
        verbose: Print full step trace for each answer.
        output_json: Emit JSON output for each answer.

    Returns:
        Process exit code (0 = normal exit, 1 = connection error).
    """
    client = MCPAsyncClient(cfg)
    try:
        await client.connect()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"[ERROR] Could not connect to MCP server: {exc}", file=sys.stderr)
        return 1

    agent = BambooAgent(
        client,
        max_steps=max_steps,
        confidence_threshold=confidence,
        max_tokens=max_tokens,
        verbose=verbose,
    )

    print("Bamboo AI Agent  (interactive mode)  — type 'exit' or Ctrl+D to quit")
    print(f"Server: {cfg.http_url if cfg.transport == 'http' else 'stdio'}")
    print()

    try:
        while True:
            try:
                question = input(_REPL_PROMPT).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break

            if not question:
                continue
            if question.lower() in _REPL_QUIT_CMDS:
                print("Goodbye.")
                break

            try:
                result = await agent.run(question)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                print(f"[ERROR] {exc}", file=sys.stderr)
                continue

            if output_json:
                print(json.dumps(_result_to_dict(result), indent=2, ensure_ascii=False))
            else:
                print(_format_result_text(result, verbose=verbose))
            print()
    finally:
        await client.aclose()

    return 0


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser.

    Returns:
        Configured :class:`argparse.ArgumentParser`.
    """
    p = argparse.ArgumentParser(
        prog="bamboo_agent",
        description=(
            "Bamboo AI Agent — multi-step ReAct reasoning over MCP tools.\n\n"
            "Connects to a running Bamboo MCP server and runs an iterative\n"
            "Reason → Act → Observe → Evaluate loop to answer complex questions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Question source (mutually exclusive; falls back to stdin if neither given)
    q_group = p.add_mutually_exclusive_group()
    q_group.add_argument(
        "--question", "-q",
        metavar="QUESTION",
        help="Question to answer (use --interactive for REPL mode).",
    )
    q_group.add_argument(
        "--interactive", "-i",
        action="store_true",
        default=False,
        help="Start an interactive REPL session.",
    )

    # Transport
    p.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="MCP server transport (default: http).",
    )
    p.add_argument(
        "--http-url",
        metavar="URL",
        default=_DEFAULT_HTTP_URL,
        help=f"HTTP MCP server endpoint (default: {_DEFAULT_HTTP_URL}).",
    )
    p.add_argument(
        "--token",
        metavar="TOKEN",
        default=os.getenv("BAMBOO_MCP_TOKEN", ""),
        help="Bearer token for HTTP auth (or set BAMBOO_MCP_TOKEN env var).",
    )

    # Agent tuning
    p.add_argument(
        "--max-steps",
        type=int,
        default=int(os.getenv("BAMBOO_AGENT_MAX_STEPS", "6")),
        metavar="N",
        help="Maximum reasoning steps before forced synthesis (default: 6).",
    )
    p.add_argument(
        "--confidence",
        type=float,
        default=float(os.getenv("BAMBOO_AGENT_CONFIDENCE", "0.80")),
        metavar="F",
        help="Evaluator confidence threshold in [0, 1] (default: 0.80).",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.getenv("BAMBOO_AGENT_MAX_TOKENS", "2048")),
        metavar="N",
        help="Token budget for the synthesis LLM call (default: 2048).",
    )

    # Output
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Print full reasoning trace in addition to the answer.",
    )
    p.add_argument(
        "--output-json",
        action="store_true",
        default=False,
        help="Emit AgentResult as JSON (useful for scripting).",
    )

    # Logging
    p.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python logging level (default: WARNING).",
    )

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the agent.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    # Build MCPServerConfig.
    headers: dict[str, str] | None = None
    if args.token:
        headers = {"Authorization": f"Bearer {args.token}"}

    cfg = MCPServerConfig(
        transport=args.transport,
        http_url=args.http_url,
        http_headers=headers,
    )

    # Determine the question source.
    if args.interactive:
        return asyncio.run(
            _run_interactive(
                cfg=cfg,
                max_steps=args.max_steps,
                confidence=args.confidence,
                max_tokens=args.max_tokens,
                verbose=args.verbose,
                output_json=args.output_json,
            )
        )

    question: str = args.question or ""
    if not question:
        # Fall back to stdin (pipe mode).
        if sys.stdin.isatty():
            parser.print_help()
            print("\nError: provide --question, --interactive, or pipe a question via stdin.",
                  file=sys.stderr)
            return 1
        question = sys.stdin.read().strip()

    if not question:
        print("Error: empty question.", file=sys.stderr)
        return 1

    return asyncio.run(
        _run_question(
            question,
            cfg=cfg,
            max_steps=args.max_steps,
            confidence=args.confidence,
            max_tokens=args.max_tokens,
            verbose=args.verbose,
            output_json=args.output_json,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
