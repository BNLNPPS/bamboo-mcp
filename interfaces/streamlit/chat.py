"""AskPanDA Streamlit Chat UI.

Connects to an AskPanDA MCP server via:
  - Streamable HTTP transport (production): connects to ``http://host:port/mcp``
  - STDIO transport (development): spawns ``python -m bamboo.server``

Run:
  streamlit run interfaces/streamlit/chat.py

Key design decisions
--------------------
- ``bamboo_answer`` is always the answer tool — ``_guess_auto_tool`` is gone.
- Fast-path routing defaults to ON (matches server default).
- Bearer token, URL, and plugin are all user-visible sidebar controls.
- After each response, expanders show Tracing, Costs, Evidence (inspect),
  and Raw JSON — equivalent to TUI /tracing, /costs, /inspect, /json.
- LLM info and experiment display name are fetched on connect via
  ``bamboo_health`` and ``<plugin>.ui_manifest`` respectively.
- Tracing works in stdio mode (server writes to a temp file we read back).
  In HTTP mode, span data is not available client-side; the expanders
  explain this and show what information is available.
"""
# pylint: disable=no-member  # streamlit uses dynamic attributes
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from collections.abc import Sequence  # noqa: F401  (kept for type annotations in helpers)
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path bootstrap — makes ``interfaces`` importable when Streamlit runs this
# script directly (i.e. without ``pip install -e .`` at the repo root).
# Inserts the repo root (two levels up from this file) onto sys.path if it
# is not already present.
# ---------------------------------------------------------------------------
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import streamlit as st  # noqa: E402

from interfaces.shared.mcp_client import MCPClientSync, MCPServerConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HISTORY_TURNS = 10
try:
    _MAX_HISTORY_TURNS: int = int(os.getenv("BAMBOO_HISTORY_TURNS", str(_DEFAULT_HISTORY_TURNS)))
except ValueError:
    _MAX_HISTORY_TURNS = _DEFAULT_HISTORY_TURNS

_ANSWER_TOOL = "bamboo_answer"
_DEFAULT_PLUGIN = os.getenv("ASKPANDA_PLUGIN", "atlas")

# Prices in USD per 1 million tokens: (input_rate, output_rate).
# Verify against current provider docs; unknown models fall back to _DEFAULT_COST.
_MODEL_COST_PER_MTOK: dict[str, tuple[float, float]] = {
    # Mistral
    "mistral-large-latest": (2.00, 6.00),
    "mistral-large-2411": (2.00, 6.00),
    "mistral-small-latest": (0.20, 0.60),
    "mistral-small-2501": (0.20, 0.60),
    "open-mistral-nemo": (0.15, 0.15),
    # Anthropic
    "claude-opus-4-5": (15.00, 75.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    # Google
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
}
_DEFAULT_COST: tuple[float, float] = (1.00, 3.00)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _extract_text(content_items: Any) -> str:
    """Extract human-readable text from MCP content items.

    Args:
        content_items: Tool response — list of MCPContent dicts, a single
            dict, a string, or any object with a ``content`` attribute.

    Returns:
        Concatenated text content, or empty string.
    """
    if content_items is None:
        return ""
    if hasattr(content_items, "content"):
        content_items = getattr(content_items, "content")
    if isinstance(content_items, str):
        return content_items
    if isinstance(content_items, dict):
        if content_items.get("type") == "text":
            return str(content_items.get("text", ""))
        return json.dumps(content_items, indent=2)
    if isinstance(content_items, list):
        parts: list[str] = []
        for item in content_items:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, indent=2))
            elif hasattr(item, "type") and getattr(item, "type") == "text" and hasattr(item, "text"):
                parts.append(str(getattr(item, "text")))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p.strip())
    try:
        return json.dumps(content_items, indent=2)
    except Exception:  # pylint: disable=broad-exception-caught
        return str(content_items)


def _tool_names(tools_result: Any) -> list[str]:
    """Extract tool names from ``session.list_tools()`` results.

    Args:
        tools_result: Return value of MCP list_tools.

    Returns:
        Sorted list of tool name strings.
    """
    if hasattr(tools_result, "tools"):
        tools_result = getattr(tools_result, "tools")
    names: list[str] = []
    if tools_result is None:
        return names
    if isinstance(tools_result, list):
        for t in tools_result:
            if isinstance(t, dict) and "name" in t:
                names.append(str(t["name"]))
            elif hasattr(t, "name"):
                names.append(str(getattr(t, "name")))
    elif isinstance(tools_result, dict):
        inner = tools_result.get("tools")
        if isinstance(inner, list):
            return _tool_names(inner)
    return sorted(set(names))


def _cap_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Trim messages to at most ``_MAX_HISTORY_TURNS`` user+assistant pairs.

    Args:
        messages: Full message list.

    Returns:
        Trimmed list keeping the most recent turns.
    """
    max_msgs = _MAX_HISTORY_TURNS * 2
    return messages[-max_msgs:] if len(messages) > max_msgs else messages


def _estimate_cost(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate LLM cost from trace spans.

    Args:
        spans: Parsed trace span dicts from the trace file.

    Returns:
        Dict with ``calls``, ``total_input``, ``total_output``,
        ``total_tokens``, ``total_cost_usd``, and ``unknown_models``.
    """
    calls: list[dict[str, Any]] = []
    unknown_models: list[str] = []
    total_input = total_output = 0
    total_cost = 0.0

    for span in spans:
        if span.get("event") != "llm_call":
            continue
        model = str(span.get("model", "unknown"))
        provider = str(span.get("provider", ""))
        inp = int(span.get("input_tokens") or 0)
        out = int(span.get("output_tokens") or 0)
        duration_ms = float(span.get("duration_ms", 0.0))

        rate = _MODEL_COST_PER_MTOK.get(model) or _MODEL_COST_PER_MTOK.get(model.lower())
        if rate is None:
            unknown_models.append(model)
            rate = _DEFAULT_COST

        call_cost = (inp / 1_000_000) * rate[0] + (out / 1_000_000) * rate[1]
        calls.append({
            "provider": provider, "model": model,
            "input_tokens": inp, "output_tokens": out,
            "duration_ms": duration_ms, "cost_usd": call_cost,
            "rate_in": rate[0], "rate_out": rate[1],
        })
        total_input += inp
        total_output += out
        total_cost += call_cost

    return {
        "calls": calls,
        "total_input": total_input, "total_output": total_output,
        "total_tokens": total_input + total_output,
        "total_cost_usd": total_cost,
        "unknown_models": list(set(unknown_models)),
    }


def _read_spans(trace_file: str, from_pos: int) -> list[dict[str, Any]]:
    """Read bamboo trace spans written since ``from_pos``.

    Args:
        trace_file: Path to the NDJSON trace file.
        from_pos: Byte offset to start reading from.

    Returns:
        List of parsed span dicts.
    """
    spans: list[dict[str, Any]] = []
    try:
        with open(trace_file, "r", encoding="utf-8") as fh:
            fh.seek(from_pos)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get("bamboo_trace"):
                        spans.append(obj)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return spans


def _trace_file_size(trace_file: str) -> int:
    """Return current byte size of the trace file, or 0 if absent.

    Args:
        trace_file: Path to the trace file.

    Returns:
        File size in bytes.
    """
    try:
        return os.path.getsize(trace_file)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Cached MCP client (NO widget calls inside)
# ---------------------------------------------------------------------------

@st.cache_resource
def _get_mcp_client(
    transport: str,
    http_url: str,
    bearer_token: str,
    stdio_command: str,
    trace_file: str,
) -> MCPClientSync:
    """Create and cache an MCPClientSync.

    All parameters are plain scalars so Streamlit can hash them correctly.
    No widgets may be called inside a ``@st.cache_resource`` function.

    Args:
        transport: ``"http"`` or ``"stdio"``.
        http_url: MCP endpoint URL (HTTP transport).
        bearer_token: Bearer token for auth, or empty string.
        stdio_command: Python executable for stdio transport.
        trace_file: Trace file path injected into stdio server env.

    Returns:
        Connected MCPClientSync instance.
    """
    if transport == "http":
        headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        cfg = MCPServerConfig(transport="http", http_url=http_url, http_headers=headers)
    else:
        env = os.environ.copy()
        env["BAMBOO_TRACE"] = "1"
        env["BAMBOO_TRACE_FILE"] = trace_file
        env["BAMBOO_QUIET"] = "1"
        cfg = MCPServerConfig(
            transport="stdio",
            stdio_command=stdio_command,
            stdio_args=["-m", "bamboo.server"],
            stdio_env=env,
        )
    return MCPClientSync(cfg)


# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------

def _init_session() -> None:
    """Initialise all required session state keys on first run."""
    _fp_env = os.getenv("BAMBOO_FAST_PATH", "1").strip().lower()
    _fast_path_default: bool = _fp_env not in ("0", "off", "false")
    defaults: dict[str, Any] = {
        "messages": [],
        "fast_path": _fast_path_default,
        "tool_names": [],
        "display_name": "AskPanDA",
        "llm_info": "",
        "server_ok": False,
        "last_spans": [],
        "last_evidence": None,
        "last_raw": None,
        "trace_file": os.path.join(
            tempfile.gettempdir(), f"bamboo_streamlit_{os.getpid()}.jsonl"
        ),
        "pending_question": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def _connect(mcp: MCPClientSync, plugin_id: str) -> None:
    """Fetch tool list, LLM info and display name from the server.

    Args:
        mcp: Connected MCP client.
        plugin_id: Active plugin namespace (e.g. ``"atlas"``).
    """
    try:
        tools = _tool_names(mcp.list_tools())
        st.session_state["tool_names"] = tools
        st.session_state["server_ok"] = True
    except Exception as exc:  # pylint: disable=broad-exception-caught
        st.session_state["server_ok"] = False
        st.session_state["tool_names"] = []
        raise exc

    # LLM info via bamboo_health
    try:
        health_raw = mcp.call_tool("bamboo_health", {})
        health_text = _extract_text(health_raw)
        for line in health_text.splitlines():
            if "llm_info:" in line:
                st.session_state["llm_info"] = line.split(":", 1)[1].strip()
                break
    except Exception:  # pylint: disable=broad-exception-caught
        st.session_state["llm_info"] = ""

    # Display name and banner from ui_manifest
    manifest_tool = f"{plugin_id}.ui_manifest"
    if manifest_tool in tools:
        try:
            raw = mcp.call_tool(manifest_tool, {})
            manifest = json.loads(_extract_text(raw) or "{}")
            if isinstance(manifest, dict):
                st.session_state["display_name"] = str(
                    manifest.get("display_name") or f"AskPanDA – {plugin_id.upper()}"
                )
        except Exception:  # pylint: disable=broad-exception-caught
            st.session_state["display_name"] = f"AskPanDA – {plugin_id.upper()}"
    else:
        st.session_state["display_name"] = f"AskPanDA – {plugin_id.upper()}"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar() -> tuple[str, str, str, str, str]:
    """Render sidebar controls and return connection parameters.

    Returns:
        Tuple of ``(transport, http_url, bearer_token, plugin_id, stdio_command)``.
    """
    st.sidebar.title("AskPanDA")

    # --- Connection ---
    st.sidebar.header("Connection")
    transport = st.sidebar.selectbox(
        "Transport", ["http", "stdio"], index=0,
        help="HTTP: connect to a running server. stdio: spawn a local server.",
    )

    http_url = st.sidebar.text_input(
        "Server URL",
        value=os.getenv("MCP_URL", "http://localhost:8000/mcp"),
        disabled=(transport != "http"),
        help="MCP endpoint URL, e.g. http://hostname:8000/mcp",
    )

    bearer_token = st.sidebar.text_input(
        "Bearer token (optional)",
        value=os.getenv("MCP_BEARER_TOKEN", ""),
        type="password",
        disabled=(transport != "http"),
        help="Leave empty if the server has no auth configured.",
    )

    _plugin_options = ["atlas", "epic", "cgsim"]
    _plugin_default_index = (
        _plugin_options.index(_DEFAULT_PLUGIN)
        if _DEFAULT_PLUGIN in _plugin_options
        else 0
    )
    plugin_id = st.sidebar.selectbox(
        "Experiment / plugin",
        _plugin_options,
        index=_plugin_default_index,
        help="Selects the ui_manifest tool and display name.",
    )

    stdio_command = sys.executable  # not exposed to users; used internally

    # --- Server status ---
    st.sidebar.header("Status")
    if st.session_state.get("server_ok"):
        st.sidebar.success("Connected")
        llm_info = st.session_state.get("llm_info", "")
        if llm_info:
            st.sidebar.caption(f"🤖 {llm_info}")
        n_tools = len(st.session_state.get("tool_names", []))
        st.sidebar.caption(f"{n_tools} tools registered")
    else:
        st.sidebar.warning("Not connected")

    # --- Settings ---
    st.sidebar.header("Settings")
    st.sidebar.toggle(
        "Fast-path routing",
        key="fast_path",
        help=(
            "ON: deterministic routing for task/job/pilot questions (faster). "
            "OFF: all questions go through the LLM planner."
        ),
    )

    n_turns = len(st.session_state.get("messages", [])) // 2
    st.sidebar.caption(
        f"Context: {n_turns} / {_MAX_HISTORY_TURNS} turns in memory"
    )

    # --- Actions ---
    st.sidebar.header("Actions")
    if st.sidebar.button("🔄  Reconnect", use_container_width=True):
        st.cache_resource.clear()
        for key in ("server_ok", "tool_names", "display_name", "llm_info",
                    "last_spans", "last_evidence", "last_raw"):
            st.session_state.pop(key, None)
        st.rerun()

    if st.sidebar.button("🗑  Clear chat", use_container_width=True):
        st.session_state["messages"] = []
        st.session_state["last_spans"] = []
        st.session_state["last_evidence"] = None
        st.session_state["last_raw"] = None
        st.rerun()

    with st.sidebar.expander("Tools registered on server"):
        tools = st.session_state.get("tool_names", [])
        if tools:
            st.write("\n".join(f"- `{t}`" for t in tools))
        else:
            st.caption("Not connected yet.")

    return transport, http_url, bearer_token, str(plugin_id), stdio_command


# ---------------------------------------------------------------------------
# Response detail expanders
# ---------------------------------------------------------------------------

def _render_tracing_expander(
    spans: list[dict[str, Any]],
    transport: str,
) -> None:
    """Render tracing span data in a Streamlit expander.

    Args:
        spans: Trace spans collected for the last request.
        transport: ``"http"`` or ``"stdio"``.
    """
    with st.expander("⏱  Tracing", expanded=False):
        if not spans:
            if transport == "http":
                st.caption(
                    "Trace spans are not available for HTTP transport — the server "
                    "writes them to its own trace file. To inspect them, run on the server:\n\n"
                    "```bash\ntail -f $BAMBOO_TRACE_FILE | grep bamboo_trace | jq .\n```"
                )
            else:
                st.caption("No spans collected for this request.")
            return

        rows: list[dict[str, Any]] = []
        total_ms = 0.0
        for span in spans:
            event = str(span.get("event", ""))
            tool = str(span.get("tool", ""))
            duration_ms = float(span.get("duration_ms", 0.0))
            if event == "tool_call":
                total_ms = duration_ms
            # Build a short detail string
            detail_parts: list[str] = []
            if event == "llm_call":
                provider = span.get("provider", "")
                model = span.get("model", "")
                inp = span.get("input_tokens")
                out = span.get("output_tokens")
                detail_parts.append(f"{provider}/{model}")
                if inp is not None and out is not None:
                    detail_parts.append(f"tokens={inp}→{out}")
            elif event == "guard":
                allowed = span.get("allowed")
                reason = span.get("reason", "")
                detail_parts.append(f"allowed={allowed} reason={reason}")
            elif event == "retrieval":
                backend = span.get("backend", "")
                hits = span.get("hits")
                if backend:
                    detail_parts.append(f"backend={backend}")
                if hits is not None:
                    detail_parts.append(f"hits={hits}")
            elif event == "route":
                route = span.get("route", "")
                if route:
                    detail_parts.append(f"route={route}")
            rows.append({
                "event": event,
                "tool": tool,
                "ms": f"{duration_ms:.0f}",
                "detail": "  ".join(detail_parts),
            })

        rows.append({"event": "total", "tool": "", "ms": f"{total_ms:.0f}", "detail": "wall time"})
        st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_costs_expander(spans: list[dict[str, Any]]) -> None:
    """Render estimated LLM cost in a Streamlit expander.

    Args:
        spans: Trace spans collected for the last request.
    """
    with st.expander("💰  Estimated cost", expanded=False):
        if not spans:
            st.caption("No trace data — cost estimation requires tracing.")
            return

        est = _estimate_cost(spans)
        calls = est["calls"]
        if not calls:
            st.caption("No LLM calls found in spans (tracing may be disabled on the server).")
            return

        st.dataframe(
            [
                {
                    "model": c["model"],
                    "input tok": c["input_tokens"],
                    "output tok": c["output_tokens"],
                    "duration ms": f"{c['duration_ms']:.0f}",
                    "cost USD": f"${c['cost_usd']:.6f}",
                }
                for c in calls
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            f"**Total:** {est['total_input']:,} in + {est['total_output']:,} out = "
            f"{est['total_tokens']:,} tokens  |  **${est['total_cost_usd']:.6f}**"
        )
        if est["unknown_models"]:
            st.warning(
                f"Unknown model(s): {', '.join(est['unknown_models'])}. "
                f"Rates defaulted to ${_DEFAULT_COST[0]:.2f}/${_DEFAULT_COST[1]:.2f} per Mtok."
            )


def _render_evidence_expander(evidence: Any) -> None:
    """Render the compact evidence dict in a Streamlit expander.

    Args:
        evidence: Evidence dict from ``bamboo_last_evidence`` with ``mode='evidence'``.
    """
    with st.expander("🔬  Evidence (inspect)", expanded=False):
        if evidence is None:
            st.caption("No evidence stored — ask about a specific task or job first.")
            return
        st.json(evidence)


def _render_raw_expander(raw: Any) -> None:
    """Render the raw BigPanDA API response in a Streamlit expander.

    Args:
        raw: Raw payload from ``bamboo_last_evidence`` with ``mode='raw'``.
    """
    with st.expander("📄  Raw JSON", expanded=False):
        if raw is None:
            st.caption("No raw payload stored — ask about a specific task or job first.")
            return
        st.json(raw)


def _fetch_evidence(mcp: MCPClientSync) -> tuple[Any, Any]:
    """Fetch compact evidence and raw payload from the server.

    Calls ``bamboo_last_evidence`` twice — once for the compact evidence dict
    and once for the verbatim BigPanDA API response.

    Args:
        mcp: Connected MCP client.

    Returns:
        Tuple of ``(evidence_dict_or_None, raw_payload_or_None)``.
    """
    evidence = None
    raw = None
    try:
        ev_raw = mcp.call_tool("bamboo_last_evidence", {"mode": "evidence"})
        ev_text = _extract_text(ev_raw)
        if ev_text:
            parsed = json.loads(ev_text)
            inner = parsed.get("evidence", parsed)
            if inner and not (isinstance(inner, dict) and "error" in inner):
                evidence = inner
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        raw_result = mcp.call_tool("bamboo_last_evidence", {"mode": "raw"})
        raw_text = _extract_text(raw_result)
        if raw_text:
            parsed_raw = json.loads(raw_text)
            inner_raw = parsed_raw.get("evidence", parsed_raw)
            if inner_raw and not (isinstance(inner_raw, dict) and "error" in inner_raw):
                raw = inner_raw
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    return evidence, raw


# ---------------------------------------------------------------------------
# Main chat panel
# ---------------------------------------------------------------------------

def _render_chat(mcp: MCPClientSync, transport: str) -> None:
    """Render the main chat panel.

    Handles the two-rerun pattern required by Streamlit:
    - Rerun 1: append user message, set ``pending_question``, rerun.
    - Rerun 2: generate assistant response, clear ``pending_question``, rerun.

    Args:
        mcp: Connected MCP client.
        transport: ``"http"`` or ``"stdio"`` — affects tracing availability.
    """
    messages: list[dict[str, str]] = st.session_state["messages"]

    # Generate assistant response for a pending question
    if st.session_state.get("pending_question"):
        question: str = st.session_state["pending_question"]
        st.session_state["pending_question"] = None

        trace_file: str = st.session_state["trace_file"]
        pre_pos = _trace_file_size(trace_file) if transport == "stdio" else 0

        with st.spinner("Thinking…"):
            try:
                result = mcp.call_tool(
                    _ANSWER_TOOL,
                    {
                        "question": question,
                        "messages": list(messages),
                        "bypass_fast_path": not st.session_state.get("fast_path", True),
                    },
                )
                answer = _extract_text(result) or "*(No text output.)*"
            except Exception as exc:  # pylint: disable=broad-exception-caught
                answer = f"⚠️ Error: {exc}"

        # Collect spans (stdio only)
        spans: list[dict[str, Any]] = []
        if transport == "stdio":
            spans = _read_spans(trace_file, pre_pos)
        st.session_state["last_spans"] = spans

        # Fetch evidence from server store
        evidence, raw = _fetch_evidence(mcp)
        st.session_state["last_evidence"] = evidence
        st.session_state["last_raw"] = raw

        # Append assistant reply and cap history
        messages.append({"role": "assistant", "content": answer})
        st.session_state["messages"] = _cap_messages(messages)
        st.rerun()

    # Render chat history
    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Detail expanders below the last assistant reply
    if st.session_state["messages"] and st.session_state["messages"][-1]["role"] == "assistant":
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            _render_tracing_expander(st.session_state["last_spans"], transport)
        with col2:
            _render_costs_expander(st.session_state["last_spans"])
        with col3:
            _render_evidence_expander(st.session_state["last_evidence"])
        with col4:
            _render_raw_expander(st.session_state["last_raw"])

    # Chat input — must be the last widget
    question = st.chat_input("Ask AskPanDA…")
    if question:
        st.session_state["messages"].append({"role": "user", "content": question})
        st.session_state["pending_question"] = question
        st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Streamlit app entry point."""
    st.set_page_config(
        page_title="AskPanDA",
        page_icon="🐼",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
        <style>
        /* Base font size — increase from Streamlit default (14px) */
        html, body, [class*="css"] {
            font-size: 16px !important;
        }
        /* Chat messages */
        [data-testid="stChatMessage"] {
            font-size: 16px !important;
        }
        /* Sidebar */
        [data-testid="stSidebar"] {
            font-size: 15px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _init_session()

    transport, http_url, bearer_token, plugin_id, stdio_command = _render_sidebar()

    # Build (or retrieve cached) MCP client
    try:
        mcp = _get_mcp_client(
            transport=transport,
            http_url=http_url,
            bearer_token=bearer_token,
            stdio_command=stdio_command,
            trace_file=st.session_state["trace_file"],
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        st.error(f"Failed to create MCP client: {exc}")
        st.code(traceback.format_exc())
        st.stop()

    # Connect / refresh server metadata if not yet done
    if not st.session_state.get("server_ok"):
        with st.spinner("Connecting to server…"):
            try:
                _connect(mcp, plugin_id)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                st.error(f"Could not connect to MCP server: {exc}")
                st.info(
                    "Check that the server is running and the URL/token are correct, "
                    "then click **Reconnect** in the sidebar."
                )
                st.stop()

    # Page header
    display_name: str = st.session_state.get("display_name", "AskPanDA")
    st.title(display_name)
    llm_info = st.session_state.get("llm_info", "")
    if llm_info:
        st.caption(f"🤖 {llm_info}")

    _render_chat(mcp, transport)


if __name__ == "__main__":
    main()
