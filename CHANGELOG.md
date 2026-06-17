# Changelog

All notable changes to Bamboo are documented here.

---

## [Unreleased]

### Added
- **`bamboo_mcp` and `bamboo_services` topic keys**: two new built-in topic
  keys in `_chroma_routing.py` that map to dedicated logical collection names
  (`bamboo_mcp_docs` and `bamboo_services_docs` respectively).  Deployments
  that split the Bamboo documentation into two separate ChromaDB collections
  — one for the `bamboo-mcp` repository and one for `bamboo-mcp-services` —
  now get correctly isolated retrieval: questions about installing or
  configuring the MCP core never return Services agent documentation, and
  vice versa.  The legacy `"bamboo"` → `"bamboo_docs"` entry is retained for
  backward compatibility with single-collection deployments.
- **`_BAMBOO_SERVICES_SIGNALS`** frozenset (`bamboo_answer.py`): keyword
  phrases that unambiguously refer to the `bamboo-mcp-services` component
  (`"bamboo mcp services"`, `"bamboo-mcp-services"`, `"bamboo services"`,
  `"supervisor agent"`, `"ingestion agent"`, `"cric agent"`,
  `"document monitor"`, etc.).  Checked *before* `_BAMBOO_SIGNALS` so that
  the more specific match wins when both would match.
- **Expanded `_BAMBOO_SIGNALS`** (`bamboo_answer.py`): added
  `"bamboo install"`, `"install bamboo"`, `"bamboo plugin"`,
  `"bamboo interface"`, `"bamboo ui"`, `"bamboo tui"`, `"bamboo cli"`,
  `"bamboo streamlit"` — previously these fell through to `topic="atlas"`,
  causing the wrong collection to be queried.
- **`bamboo_env_example.sh` updated**: RAG collection map example now shows
  both the recommended two-collection layout (`bamboo_mcp` / `bamboo_services`)
  and the legacy single-collection layout as an alternative comment block.

### Added
- **Multi-collection RAG support**: `doc_rag.py` and `doc_bm25.py` now accept
  an optional `topic` parameter (e.g. `"panda"`, `"atlas"`, `"rucio"`,
  `"root"`, `"bamboo"`, `"epic"`, `"cgsim"`) that selects which ChromaDB
  collection to query, enabling the five separate collections produced by
  `bamboo-mcp-services` to be queried correctly per question domain.
- **`BAMBOO_CHROMA_COLLECTION_MAP`** env var: JSON object mapping topic keys
  to logical collection names (e.g.
  `'{"panda":"panda_docs","atlas":"atlas_docs","rucio":"rucio_docs"}'`).
  Adding a new collection requires only updating this string — no code
  changes needed.  Falls back to the existing `BAMBOO_CHROMA_COLLECTION`
  scalar, then to built-in per-topic defaults (`panda_docs`, `atlas_docs`,
  `bamboo_docs`, `rucio_docs`, `root_docs`, `epic_docs`, `cgsim_docs`).
- **`resolve_collection_for_topic()`** (`_chroma_routing.py`): new helper that
  maps a topic string → logical collection name (via
  `BAMBOO_CHROMA_COLLECTION_MAP`) → physical blue/green slot (via the
  existing `resolve_collection()`).  All RAG tools now route through this
  single function.
- **`_topic_for_question()`** (`bamboo_answer.py`): lightweight keyword
  classifier that infers the correct topic from the user question and active
  plugin (Rucio signals → `"rucio"`, ROOT signals → `"root"`, Bamboo meta
  → `"bamboo"`, atlas plugin → `"atlas"`, etc.).  Result is injected into
  both `panda_doc_search` and `panda_doc_bm25` plan tool call arguments by
  `_build_deterministic_plan()`.

### Changed
- **Subclass simplification**: `AtlasDocSearchTool`, `AtlasDocBM25Tool`,
  `EpicDocSearchTool`, `EpicDocBM25Tool`, `CgsimDocSearchTool`,
  `CgsimDocBM25Tool` — the full copy-paste `_ensure_collection()` /
  `_ensure_index()` overrides have been removed from all six package
  subclasses.  Each subclass now only overrides `get_definition()` and sets a
  `_default_topic` class attribute (`"atlas"`, `"epic"`, `"cgsim"`).  All
  collection resolution logic lives exclusively in the base class.
- **Reranking workaround removed**: the `_is_bamboo_internal()` source-priority
  reranking in `doc_rag.py` and `doc_bm25.py` (which deprioritised
  `PalNilsson/*` chunks) has been removed now that Bamboo-internal
  documentation lives in its own dedicated `bamboo_docs` collection.
- **`bamboo_env_example.sh`**: RAG section updated to document
  `BAMBOO_CHROMA_COLLECTION_MAP`; the old per-plugin collection comment block
  replaced with JSON map format.

### Tests
- `tests/test_chroma_routing.py`: `TestResolveCollectionForTopic` (9 tests)
  covering map lookup, blue/green sidecar traversal, scalar fallback,
  built-in defaults, unknown topics, case-insensitivity, corrupt map,
  and adding new collections via env only.
- `tests/test_doc_rag.py`: 4 new tests for `topic` argument passthrough,
  `_default_topic` class attribute, and `get_definition` schema shape.
- `tests/test_doc_bm25.py`: 4 new tests for `topic` argument passthrough,
  cache invalidation on topic change, and `get_definition` schema shape.
- `tests/test_bamboo_answer_rag.py`: 13 new tests for `_topic_for_question()`
  and `_build_deterministic_plan()` topic injection.


  that orchestrates MCP tool calls in a Reason → Act → Observe → Evaluate loop.
  Bypasses the single-pass `bamboo_answer`/`bamboo_executor` pipeline and is
  intended for complex, multi-hop queries.  Key types: `AgentMemory`,
  `AgentStep`, `AgentResult`, `BambooAgent`.  Uses `reasoning` LLM profile for
  tool selection and synthesis; `fast` profile for the per-step sufficiency
  evaluator.  All LLM calls are routed through the `bamboo_llm_answer` MCP tool,
  so no additional LLM initialisation is required.  Maximum steps (default 6),
  confidence threshold (default 0.80), and synthesis token budget (default 2048)
  are all configurable via `BAMBOO_AGENT_MAX_STEPS`, `BAMBOO_AGENT_CONFIDENCE`,
  and `BAMBOO_AGENT_MAX_TOKENS` environment variables.
- **Agent CLI** (`scripts/bamboo_agent.py`): standalone script wrapping
  `BambooAgent`.  Supports single-shot (`--question`), stdin-pipe, and
  interactive REPL (`--interactive`) modes.  Outputs formatted text (with
  optional `--verbose` trace) or machine-readable JSON (`--output-json`).
  Compatible with both HTTP and STDIO MCP transports.  Bearer token auth via
  `--token` or `BAMBOO_MCP_TOKEN`.
- **Agent tests** (`tests/test_agent.py`): full test coverage for
  `AgentMemory`, `_ToolSelection`, `_EvalResult`, `_extract_json_block`,
  `_truncate_observation`, `_observation_from_result`, and `BambooAgent`
  (single-step success, two-step completion, early `should_synthesise` flag,
  max-steps truncation, tool call failure, reasoning parse error, eval parse
  error, field type assertions, zero-tools edge case).
- **Agent prompt log stub**: `BambooAgent._synthesise` contains a fully
  commented-out `log_prompt` call (`# AGENT_LOG`) targeting the future
  `bamboomcp-agentlog-YYYY.MM.DD` index.  Uncomment once the index template
  is provisioned in OpenSearch.

### Fixed
- **Relative `from_dt`/`to_dt` expressions crash the Harvester API**:
  the LLM planner occasionally emits OpenSearch-style relative timestamps
  (`"now-6h"`, `"now/d"`) instead of absolute ISO-8601 strings.  The BigPanDA
  Harvester HTTP API does not understand these and returns an error, producing a
  zeroed-out evidence dict and a misleading "API unavailable" response.  Fixed by
  adding `_resolve_dt()` to `harvester_worker_impl.py`, which intercepts any
  non-ISO argument and resolves it to an absolute UTC timestamp before the HTTP
  call.  The planner routing prompt for `panda_harvester_workers` and
  `atlas.harvester_timeseries` also now explicitly instructs the LLM to use
  absolute ISO-8601 strings, not relative expressions.
- **Pilot failure-rate routing misclassification**: questions such as "which sites
  had pilot failures above 20% today?" were incorrectly routed to
  `panda_harvester_workers` (the BigPanDA HTTP snapshot tool) instead of
  `atlas.harvester_timeseries` (the OpenSearch time-series tool).  The planner
  routing guidance now has a dedicated rule for failure-rate and
  failure-percentage questions that explicitly selects `atlas.harvester_timeseries`
  with `status='failed'`, while the live-count rule is tightened to snapshot
  queries only ("how many pilots are running right now").
- **`atlas.harvester_timeseries` tool description**: the description previously
  read "used to render ASCII time-series charts in the TUI" — making the planner
  LLM treat it as an internal charting helper rather than a query tool.  The
  description now explicitly lists failure-rate and cross-site trend questions as
  primary use cases, with concrete examples.
- **`bamboo_executor._pick_synthesis_prompt`**: `atlas.harvester_timeseries` now
  selects `_SYSTEM_HARVESTER_TIMESERIES` (a new specialist prompt) instead of
  falling through to `_SYSTEM_GENERIC`.  The new prompt instructs the LLM to
  report absolute failed-pilot counts and trends, and to explain why it cannot
  compute a failure *percentage* without the total pilot count (cross-referencing
  `failed` vs `total` requires two queries).

### Added
- **`panda_job_timing` tool** (`packages/askpanda_atlas`): new OpenSearch-backed
  MCP tool that answers natural-language questions about PanDA job timing against
  the `atlas_panda_job_timing-*` index.  Uses a single LLM call to extract
  structured aggregation parameters (metric, field, filters, time range) from the
  user's question, then executes a single-value OpenSearch metric aggregation
  (`avg`, `sum`, `min`, `max`, `value_count`) and returns a compact evidence dict
  for Bamboo's central synthesiser.
- **`job_timing_schema.py`**: schema registry for the confirmed batch-1 fields
  (10 core identifier/status fields + 10 timing fields including all six parsed
  `pilottiming_*` sub-fields), field validation helpers, and the LLM prompt
  template for query-parameter extraction.
- **`job_timing_impl.py`**: full tool implementation with `fetch_job_timing()`
  (synchronous OpenSearch query, cached 120 s), `parse_llm_params()` (validates
  LLM JSON output against schema), `_default_window()` (24-hour look-back), and
  structured error/cannot-answer evidence constructors.
- **`job_timing.py`**: thin entry-point wrapper with `ImportError` fallback
  (mirrors `harvester_timeseries.py`).
- **`tests/test_job_timing.py`**: 34 tests covering schema constants, prompt
  builder, `parse_llm_params`, `_default_window`, error constructors,
  `fetch_job_timing` with mocked OpenSearch, and `PandaJobTimingTool.call()`
  end-to-end.
- `atlas.job_timing` entry point registered in `pyproject.toml`.
- `atlas_panda_job_timing-*` added to `_DEFAULT_ALLOWED_PATTERNS` in
  `core/bamboo/tools/opensearch_query.py` so the generic `opensearch_query`
  tool can also reach this index without config changes.

### Added

- **Blue/green ChromaDB slot routing — live re-resolution without server restart.**
  The `bamboo-mcp-services` document-monitor agent now stores vectors in two
  physical ChromaDB collections per logical name (`atlas_docs__a` /
  `atlas_docs__b`) and swaps between them atomically.  Bamboo MCP now resolves
  the logical collection name (e.g. `atlas_docs`) to the currently live
  physical slot on **every RAG query** by reading the routing sidecar
  (`<BAMBOO_CHROMA_PATH>/collection_routing.json`).  When the document-monitor
  agent completes an update cycle the next query automatically picks up the new
  slot with no server restart required.

  If the sidecar is absent or has no entry for the configured logical name
  Bamboo falls back to using the logical name directly, so deployments that
  have not yet upgraded to the blue/green agent are unaffected.

  **New module** `core/bamboo/tools/_chroma_routing.py` — standalone
  `resolve_collection(chroma_path, logical_name)` helper.  Does not import
  from `bamboo-mcp-services`; Bamboo MCP remains fully independent.

  **Changed:** `core/bamboo/tools/doc_rag.py` (`PandaDocSearchTool`) and
  all three plugin overrides (`askpanda_atlas`, `askpanda_epic`, `askcgsim`)
  — `_ensure_collection` now re-reads the sidecar on every call and
  invalidates the cached collection handle when the physical name changes.
  A new `_resolved_physical` attribute tracks the currently open physical
  slot; `_reset()` clears it alongside `_client` and `_collection`.

  **Scripts** `probe_rag.py` and `inspect_chroma.py` both resolve the
  logical name via the sidecar and print the resolved physical slot name in
  their output headers.

  **New tests** `tests/test_chroma_routing.py` — 11 tests covering
  `resolve_collection` (sidecar present, absent, corrupt, missing entry,
  mid-run update) and `PandaDocSearchTool` live re-resolution (correct slot
  opened, cache invalidated on swap, no unnecessary reopens, pre-blue/green
  fallback, `_reset` clears resolved name).

  **Docs** `docs/rag.md` — new *Blue/green slot routing* section explaining
  the sidecar format, live re-resolution, fallback behaviour, and how to
  diagnose the active slot with `inspect_chroma.py` and `probe_rag.py`.

 The spinner
  is rendered after the full chat history during the pending-question pass, so
  it appears at the bottom of the page just above the input box rather than
  at the top where it was invisible in long conversations.
- **RAG synthesis: prohibit general-knowledge fallback when excerpts are insufficient.**
  ``_SYSTEM_RAG`` now instructs the LLM to tell the user the documentation
  did not contain enough information rather than supplementing with general
  knowledge.  The previous wording ("supplement with your general knowledge
  but clearly distinguish...") gave the LLM a loophole to produce fully
  hallucinated answers dressed as general knowledge when the retrieved
  excerpts were topically adjacent but not actually relevant.
- **Streamlit: sidebar shows "Connected" immediately on startup.** Added
  ``st.rerun()`` after a successful first ``_connect()`` call so the
  sidebar status updates from "Not connected" to "Connected" as soon as
  the server handshake completes, without waiting for the first question.
- **Streamlit: remove Experiment/plugin selector from sidebar.** The plugin
  is now fixed to the ``ASKPANDA_PLUGIN`` environment variable (default
  ``atlas``).  Switching experiments requires restarting the server with a
  different env var rather than hot-switching in the UI, which avoids
  confusing mid-session state resets.
- **Streamlit: rating poll retries up to 3×0.5 s.** Replaces the
  single-retry flag with a tight loop that polls ``bamboo_promptlog_status``
  up to three times at 0.5 s intervals, stopping as soon as ``last_doc_id``
  is set.  Fixes intermittent missing rating buttons when OpenSearch flushes
  slowly.
- **Streamlit: rating widget retry on first question after restart.**
  If the deferred prompt-log poll fires before OpenSearch has flushed the
  background write (``last_doc_id`` still ``None``), a ``retry_promptlog``
  flag triggers one additional poll after a 0.5 s sleep on the following
  render cycle.  Fixes the missing rating buttons on the first response
  after a server restart.
- **Streamlit: one-shot rating widget.** After a user submits a star rating,
  the five rating buttons are replaced by a static confirmation caption for
  the remainder of the session, preventing duplicate votes on the same
  response.
- **Streamlit: retry prompt-log poll for first-question rating.** If the
  deferred `poll_promptlog` pass completes before the OpenSearch background
  write finishes (`last_doc_id` still `None`), a `retry_promptlog` flag
  is set and a second poll runs 0.5 s later on the next render cycle.
  This fixes the missing rating widget on the first question after a server
  restart.
- **`docs/remote-testing.md`:** Step-by-step guide for running the Bamboo MCP
  server and Streamlit UI on lxplus and accessing them from home via an SSH
  port-forwarding tunnel over the CERN VPN.  Covers SSH key setup, tunnel
  command, server and Streamlit startup, health-check verification, and a
  troubleshooting table for common failure modes.

### Fixed

- **`panda_job_status`: MCPCaller server name mismatch caused "server not
  connected" errors.** `job_status.py` used `_SERVER = "bigpanda-downloader"`
  but `panda_mcp_session.py` registers the session under
  `PANDA_MCP_SERVER_NAME = "panda"`.  The lookup always returned `None`,
  so every job-status query failed regardless of whether the PanDA MCP
  connection was healthy.  Fixed `_SERVER` to `"panda"` and updated the
  stale server name in `_mcp_caller.py` docstring,
  `docs/tools/panda_job_status.md`, and `docs/mcp_sequence_diagram.mmd`.

- **Streamlit: Mermaid diagram height and rendering.** Height estimation
  now uses non-empty line count (`line_count * 20 + 80`, capped at 800 px)
  instead of arrow count, which overcounted for state diagrams and produced
  oversized iframes that pushed nodes below the visible area.  Mermaid CDN
  bumped from `@10` to `@11` for improved state diagram rendering.
- **Streamlit: single-iframe mode no longer duplicates text.** The chat
  history render loop now skips ``st.markdown()`` for the last assistant
  message when ``BAMBOO_DIAGRAM_MODE=single-iframe`` and diagrams are
  present — ``_render_mermaid_single_iframe()`` renders both text and
  diagrams together.
- **Streamlit: classic mode sanitises edge labels with special chars.**
  A ``re.sub`` pass quotes unquoted Mermaid edge labels containing ``(``,
  ``)``, or ``<`` before rendering, preventing Mermaid v11 from tokenising
  them as separate nodes (e.g. ``(loop counter < max)``) .
- **Streamlit: dual Mermaid renderer via ``BAMBOO_DIAGRAM_MODE``.** The
  existing per-diagram ``components.html`` renderer is refactored into
  ``_render_mermaid_classic()`` and now uses ``mermaid.render()``
  (Promise API) instead of ``startOnLoad`` for precise post-render SVG
  attribute cleanup.  A new experimental ``_render_mermaid_single_iframe()``
  renderer (``BAMBOO_DIAGRAM_MODE=single-iframe``) places text and all
  diagrams into a single iframe with CSS layout: portrait diagrams float
  right at 38% width so prose wraps alongside them; landscape diagrams
  span full width below the text.  ``_render_mermaid_blocks()`` dispatches
  to the active mode; ``last_clean_answer`` is stored in session state so
  the single-iframe renderer can access the stripped markdown text.
- **Streamlit: Mermaid syntax errors show plain-text fallback.** Added a
  ``mermaid.parseError`` handler that hides the Mermaid error graphic and
  displays the raw diagram definition in a styled ``<pre>`` block instead,
  making it easy to see what the LLM generated without a jarring full-page
  error image.
- **Streamlit: Mermaid diagrams scale and auto-size correctly.** A
  ``MutationObserver`` strips Mermaid's inline ``width``/``height``
  attributes from the SVG after render (they override CSS and cause
  oversized output), then posts the actual rendered height to Streamlit
  so the iframe auto-resizes to fit.  A 600 ms fallback ``setTimeout``
  handles edge cases where the observer fires before layout settles.
  ``scrolling=False`` — no scroll bar needed once the iframe matches the
  diagram height.
- **Streamlit: Mermaid diagrams scale to fit iframe width.** Switched to
  ``useMaxWidth: true`` with ``width: 100% !important`` on the SVG so
  diagrams shrink to fit rather than rendering at natural size and pushing
  content off-screen.  Reduced node/rank spacing (40/50 px) and tightened
  the height estimate to 14 px per line (cap 600 px) so diagrams are
  compact.  Mermaid CDN bumped to v11.
- **Streamlit: `/rates` date-filtered queries no longer fail.** The
  ``/rates today``, ``/rates week``, and ``/rates month`` slash commands
  now pass a fully pre-built OpenSearch ``bool/must`` query with both the
  ``exists`` on ``rating`` and the ``range`` on ``@timestamp`` baked in,
  leaving nothing for the LLM to construct or modify.  Previously the LLM
  generated a malformed ``range`` query combining multiple fields.
- **Streamlit: `st.components.v1.html` deprecation noted.** `st.iframe`
  (the advertised replacement) accepts a URL `src`, not raw HTML, so it
  cannot replace `components.v1.html` for inline Mermaid rendering.
  A ``.. note::`` has been added to the docstring documenting this
  constraint.  The call site is unchanged pending a Streamlit fix or
  alternative approach.
- **Prompt log: suppress 403 index-template spam.** `_ensure_index_template`
  now detects `AuthorizationException(403)` responses, sets the
  ``_template_applied`` flag to prevent retries, and logs at ``INFO`` rather
  than ``WARNING``.  The OpenSearch ``pilot-monitor-agent`` user lacks
  ``indices:admin/index_template/put`` permission; retrying on every server
  start was pointless and noisy.  Document writes are unaffected.


- **PanDA MCP OIDC token file support.** `panda_mcp_session.py` now reads
  the `id_token` field from the OIDC token cache file written by
  `get-panda-token` (from the `panda-mcp-client` package).  Token resolution
  order: (1) `PANDA_MCP_TOKEN` env var, (2) `id_token` from the file at
  `PANDA_MCP_TOKEN_FILE` (default `~/.panda_id_token`), (3) no token for
  public endpoints.  A new `_read_token_file()` helper handles JSON parsing
  and all failure modes (missing file, malformed JSON, missing field) with
  WARNING-level log messages rather than crashes.  Token renewal will be
  handled by a forthcoming Bamboo MCP agent service.
- **`panda_server_health`: error diagnosis.** When `system_is_alive` returns
  an error string, a new `_diagnose_error()` helper maps known patterns to
  human-readable explanations included in the evidence (`error_explanation`
  field) and appended to the summary text.  Covers: server-side SSL failure
  on port 25443, Bamboo-side CA bundle issues, connection refused/timeout,
  and auth/token errors.  No second LLM call required — diagnosis is
  deterministic.
- **TLS docs — use system CA bundle via `SSL_CERT_FILE`**: The correct
  approach on lxplus is `export SSL_CERT_FILE=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`.
  Both `httpx` and `requests` honour this standard env var automatically.
  Modifying the certifi bundle is fragile (DER files or HTML redirect pages
  silently corrupt it; changes lost on `pip upgrade certifi`) and is no
  longer recommended.  Updated `CLAUDE.md`, `bamboo_env_example.sh`, and
  `docs/question-cheatsheet.md`.

### Fixed

- **`panda_server_health`: correct upstream tool name.** The tool name used
  to call the PanDA MCP server was `is_alive`; the actual tool name exposed
  by the server is `system_is_alive`.  Updated `_TOOL` constant and all
  docstring references accordingly.
- **`panda_mcp_session`: surface inner exception from `ExceptionGroup`.**
  The session failure handler previously logged only the top-level
  `ExceptionGroup` message, hiding the root cause.  It now iterates
  `exc.exceptions` and logs each inner exception with a full traceback via
  `exc_info=`.
- **PanDA MCP TLS on lxplus**: The certifi bundle in the virtualenv does not
  include the CERN Grid CA or CERN Root CA 2 even on lxplus, where the
  system CA store does.  `PANDA_MCP_BASE_URL` must omit the trailing slash
  (use `…/mcp` not `…/mcp/`) to avoid a 307 redirect that the MCP client
  does not follow.  Updated `bamboo_env_example.sh` and docs accordingly.

- **Mermaid diagram rendering in Streamlit.** The LLM can now return a
  ` ```mermaid ``` ` block alongside prose when a question calls for a diagram
  (algorithms, state machines, protocols, flows).  The Streamlit UI extracts
  the block before storing the response in chat history (keeping history
  clean), then renders it inline.  Multiple diagrams per response are
  supported.  The TUI strips diagram blocks before storing history.
  The `_MERMAID_GUIDANCE` constant is appended to `_SYSTEM_RAG`,
  `_SYSTEM_GENERIC`, `_SYSTEM_RAG_CGSIM`, `_SYSTEM_GENERIC_CGSIM`, and
  `_SYSTEM_CODE_QUERY` synthesis prompts.
  Added `streamlit-mermaid>=0.2.0` to `requirements-ui.txt`.

- **Mermaid rendering — CDN-based renderer replacing streamlit-mermaid.**
  `streamlit-mermaid` uses `svgPanZoom` which scales the entire diagram SVG
  down to fit the Streamlit column width, making node text unreadably small.
  Replaced with `st.components.v1.html` embedding Mermaid 10 from CDN with
  `useMaxWidth: false` and `htmlLabels: true` — no SVG scaling, nodes render
  at their natural size, wide diagrams scroll horizontally.
  `_wrap_mermaid_labels()` post-processor wraps long node labels using native
  Mermaid `\n` line breaks, splits on underscores, and hard-cuts tokens that
  exceed the limit, ensuring all text is readable regardless of LLM output.
  `_MERMAID_GUIDANCE` updated with strict node label rules (≤20 chars/line,
  `\n` not `<br/>`, no long identifiers in nodes), anti-hallucination rules
  for static analysis findings, and explicit Mermaid syntax rules per diagram
  type.  `streamlit-mermaid` is retained as an optional dependency.

- **Superuser / developer mode.** A password-protected developer tier is now
  available in both the Streamlit and TUI interfaces.

  - Set `BAMBOO_SUPERUSER_PASSWORD` in `bamboo_env.sh` to enable the feature.
  - **Streamlit:** a "Developer access" section appears in the sidebar.  After
    entering the correct password, the session is flagged as a superuser and
    developer tools become active.  A 🔓/🔒 toggle controls the lock state.
  - **TUI:** `/superuser <password>` unlocks the session; listed in `/help`.
  - **Pre-dispatch guard:** questions that would route to superuser-only tools
    are blocked before the server call when the session is unauthenticated.
    Guard logic lives in `interfaces/shared/superuser_guard.py` and is shared
    by both interfaces.  Configurable via `BAMBOO_SUPERUSER_PATTERNS` and
    `BAMBOO_SUPERUSER_TOOLS` env vars.  Superuser tools are always registered
    on the MCP server; the guard is a UI-layer gate only.
  - Added `BAMBOO_SUPERUSER_PASSWORD`, `BAMBOO_SUPERUSER_TOOLS`, and
    `BAMBOO_SUPERUSER_PATTERNS` documentation to `bamboo_env_example.sh`.

- **`code_query` tool (superuser / developer).** A new built-in MCP tool that
  fetches an arbitrary source file from any configured GitHub repository and
  returns it for LLM analysis.  Replaces the earlier `pilot_code_query` design.

  - Input: `file_path` (e.g. `pilot.py`, `pilot/util/processes.py`), `question`,
    and optional `function_name` for targeted function extraction using AST.
  - Repository and branch configurable via `BAMBOO_CODE_QUERY_REPO` and
    `BAMBOO_CODE_QUERY_BRANCH` (defaults: `PanDAWMS/pilot3`, `master`).
  - Source limit raised to **150 000 characters** (from 12 000) with
    line-boundary truncation — rare for typical files.
  - Evidence pipeline fix: source is extracted from the evidence dict *before*
    `_compact_json` (12K limit) and appended as a plain fenced block, ensuring
    the `truncated` flag and all metadata always reach the LLM intact regardless
    of file size.
  - Dedicated synthesis prompt `_SYSTEM_CODE_QUERY` with rules against
    fabricating source lines, claiming identifiers are unused without tracing
    them, and inventing bugs not demonstrable from the source.
  - Tagged `superuser`; evidence expanders hidden from non-superuser sessions.
  - Registered as `"code_query"` in `core.py` `TOOLS` dict.
  - Full test coverage in `tests/test_code_query.py` (25 tests).
  - `docs/tools/code_query.md` — new tool reference documentation.
  - `docs/tools/pilot_code_query.md` — superseded; delete from repo.

- **`code_query` — fast-path routing.** `_build_deterministic_plan` now
  includes a rule (priority 6, after ID-driven rules, before RAG fallback) that
  routes to `code_query` when the question contains a `*.py` filename/path or an
  inspection verb combined with a repository keyword.  The first `*.py` token is
  extracted from the question and passed as `file_path`.

- **`code_query` — follow-up routing.** After a `code_query` response,
  content-free affirmatives (`"yes please"`, `"ok"`, `"continue"`) and
  code-review continuation phrases (`"verify the full file"`, `"show the
  remaining code"`, `"get the complete source"`) are automatically re-routed to
  `code_query` with the file path recovered from history.  Bypasses the topic
  guard.  Detection via `_last_tool_was_code_query` (scans prior assistant
  message for code-specific vocabulary) and `_is_code_review_continuation`
  (regex matching continuation words + repository keywords).

- **Streamlit — plugin display name fix.** Switching the plugin dropdown while
  connected now immediately re-fetches `ui_manifest` for the new plugin, so the
  display name updates correctly without requiring a reconnect.

- **Streamlit — generic inline plot.** After any tool response with flat tabular
  evidence (`columns` + `rows`), an interactive Plotly chart is rendered directly
  in the main chat area.  Chart type is chosen automatically based on column
  types.  Requires `plotly>=5.0` (added to `requirements-ui.txt`).

### Added

- **OpenSearch prompt-log UI notifications.** When ``BAMBOO_OPENSEARCH_PROMPTLOG``
  is set, write confirmations and errors from the OpenSearch indexing background
  task are now surfaced directly inside the running interface.

  Architecture: ``log_prompt()`` is called from ``call_llm()`` in
  ``bamboo_executor.py`` as ``asyncio.create_task`` (fire-and-forget).
  ``_write_document`` appends each outcome to a process-local ring buffer
  (``deque(maxlen=20)``) in ``prompt_log.py``.  A new built-in MCP tool
  ``bamboo_promptlog_status`` exposes the buffer via a destructive drain
  (events delivered exactly once per poll).  Both interfaces poll the tool
  after each response and display results in the UI.

  - ``core/bamboo/llm/prompt_log.py``: added ``_event_log`` ring buffer,
    ``drain_events()``, ``NotifyFn``, ``register_notify_callback()``,
    ``clear_notify_callback()``, and ``_notify()``.  ``_write_document``
    appends ``{"turn", "severity", "message"}`` events on success, warning,
    error, and ``ImportError`` (opensearch-py not installed).  The
    ``ImportError`` case now produces a visible ``"warning"`` event with an
    install instruction rather than silently logging at DEBUG.
  - ``core/bamboo/tools/bamboo_executor.py``: ``call_llm()`` gains a
    ``tools_used`` parameter and fires ``log_prompt`` via
    ``asyncio.create_task`` after each LLM response.  New
    ``BambooPromptLogStatusTool`` / ``bamboo_promptlog_status_tool`` calls
    ``drain_events()`` and returns events as JSON.
  - ``core/bamboo/core.py``: ``bamboo_promptlog_status`` registered in
    ``TOOLS``.
  - **TUI** (``interfaces/textual/chat.py``): ``_fetch_promptlog_events()``
    polls ``bamboo_promptlog_status`` after every response with a retry loop
    (6 × 0.5 s) to allow the background OpenSearch write to complete.
    ``"error"`` events render as error panels; ``"info"``/``"warning"`` as
    system panels.
  - **Streamlit** (``interfaces/streamlit/chat.py``):
    ``_poll_promptlog_events()`` polls on the render cycle *after* the
    response rerun (deferred via ``poll_promptlog`` session-state flag) so
    the background write has time to complete.  Events are pushed to
    ``promptlog_notices`` and rendered by ``_render_promptlog_notices()`` as
    ``st.error`` / ``st.warning`` / ``st.toast``.

  Requires ``pip install opensearch-py`` and write permission for the
  configured user on ``bamboomcp-promptlog-*`` in OpenSearch.

### Fixed

- **TUI — alternate-screen rendering on SSH (lxplus).** Textual's `--no-inline`
  (alternate screen) renderer uses absolute cursor positioning without erasing
  lines, relying on the terminal clearing the alternate screen buffer on entry.
  SSH pseudo-TTYs (e.g. lxplus accessed via Claude Code) do not reliably do
  this, causing every repaint to accumulate as ghost frames — visible as stacked
  bordered panels during long requests or a screen full of blue lines at
  startup.  Confirmed via debug instrumentation that `_write_panel` is called
  the correct number of times; the ghosting was a pure terminal rendering
  artifact.  This behaviour is consistent across all Textual versions tested
  (0.86–8.2.5) and cannot be fixed in application code without patching Textual.

  When an SSH session is detected (`SSH_CLIENT` / `SSH_TTY` /
  `SSH_CONNECTION` env vars, set automatically by OpenSSH), `--no-inline` is
  silently overridden and inline mode is used instead.  Inline mode uses delta
  updates (relative cursor movement) and renders correctly on all terminals.
  Set `BAMBOO_FORCE_NO_INLINE=1` to override the auto-switch for terminals
  known to handle alternate screen correctly over SSH.

- **`_compact_json` truncating `code_query` evidence.** `_compact_json` has a
  12 000-character limit applied to all evidence blobs.  A `code_query` result
  with a 23K source file produced a 25K JSON blob, truncated mid-source before
  the `"truncated": false` flag was reached.  The LLM received genuinely
  truncated content and correctly reported it.  Fixed in `_call_tool_and_collect`:
  `code_query` evidence is now handled specially — `source` is extracted before
  `_compact_json` and appended as a plain fenced block, so metadata always passes
  intact.

- **Diagram questions routing to `code_query` instead of RAG.**
  Questions like *"show me a diagram of the pilot states"* matched
  `_is_code_query_question` (verb `show me` + keyword `pilot`) and were
  routed to `code_query`, which returned an error because no `.py` file
  was present.  Fixed with a two-tier verb model: tier-1 source-access verbs
  (`download`, `fetch`, `look at`, …) match with any domain keyword; tier-2
  conceptual verbs (`show me`, `explain`, `describe`, …) only match with
  structural code keywords (`function`, `class`, `source`, …), not concept
  words like `pilot` alone.  Applied consistently in both
  `bamboo_answer.py` and `superuser_guard.py`.

- **LLM refusing to draw diagrams from partial RAG context.**
  `_SYSTEM_RAG`'s *"don't add unreferenced claims"* rule prevented the LLM
  from generating a Mermaid diagram when the documentation excerpts described
  states and transitions but didn't contain a pre-drawn diagram.  Added an
  explicit exception: when the user asks for a diagram and the LLM has enough
  knowledge to draw one, it should do so and label it as general knowledge.

- **Superuser gate not blocking questions without a file path.** The original
  guard regex required a `pilot/...py` path separator, so `"Look at pilot.py"`
  slipped through.  Replaced with a two-signal detector: any `*.py` token
  (bare filename or slash-path) OR an inspection verb combined with a repository
  keyword.  Both `bamboo_answer.py` and `superuser_guard.py` now use the same
  detection logic, kept in sync.

- **`"Look at pilot.py"` not routing to `code_query`.** Without a fast-path
  rule, the LLM planner failed to route bare filenames to `code_query` and fell
  back to monitoring evidence.  Fixed by adding `code_query` to the deterministic
  fast-path (see above).

- **Follow-up phrases hitting the topic guard.** After a `code_query` response,
  natural follow-ups such as `"yes please"`, `"please verify the full file"`, and
  `"download the full file"` were blocked by the topic guard as off-topic.
  Fixed by `_is_content_free_followup` pattern extension and the new
  `_is_code_review_continuation` interceptor in `_run_fast_path_intercepts`.

- **Streamlit — `stmermaid.mermaid()` AttributeError.** `streamlit-mermaid`
  0.3.0 exposes `st_mermaid(code=...)`, not `mermaid()`.  Fixed.

- **`ASKPANDA_PLUGIN` not cleared when sourcing env file.** The line was
  commented out in `bamboo_env_example.sh`, so sourcing it left a stale
  `cgsim` value from a previous session in the shell.  Now explicitly exported.

- **Plugin tool isolation — PanDA tools no longer visible to non-PanDA plugins.**
  `_build_deterministic_plan`, `_run_fast_path_intercepts`, and the LLM planner
  all gate PanDA-specific rules behind `plugin_id in _PANDA_PLUGINS`.

- **`cgsim.sim_query` — SQL generation token cap, enumeration, routing.**
  See v1.0.7 for full details; these fixes were backported into this release.

- **Streamlit `_fetch_evidence` — double-nesting and null-error guard.** Fixed.

- **`interfaces/shared/mcp_client.py` — mcp SDK 1.x compatibility.** Fixed.

- **`requirements-rag.txt` — `pysqlite3-binary` restricted to Linux.** Fixed.

### Changed

- **`pilot_code_query` → `code_query`.** The tool, module, test file, env vars,
  synthesis prompt, and all documentation have been renamed for generality.
  The tool now targets any GitHub repository (not just pilot3) and uses `file_path`
  (not `pilot_path`) as the input parameter.

  | Old | New |
  |---|---|
  | `pilot_code_query` | `code_query` |
  | `bamboo.tools.pilot_code_query` | `bamboo.tools.code_query` |
  | `PilotCodeQueryTool` | `CodeQueryTool` |
  | `fetch_pilot_code()` | `fetch_source_file()` |
  | `pilot_path` parameter | `file_path` parameter |
  | `BAMBOO_PILOT_REPO` | `BAMBOO_CODE_QUERY_REPO` |
  | `BAMBOO_PILOT_BRANCH` | `BAMBOO_CODE_QUERY_BRANCH` |
  | `_SYSTEM_PILOT_CODE_QUERY` | `_SYSTEM_CODE_QUERY` |
  | `tests/test_pilot_code_query.py` | `tests/test_code_query.py` |
  | `docs/tools/pilot_code_query.md` | `docs/tools/code_query.md` |


### Added

- **OpenSearch read-query tools.** Bamboo can now query any index on the CERN
  OpenSearch cluster directly from the TUI, Streamlit, or any MCP client —
  without needing the OpenSearch Dashboards web UI.

  - **`opensearch_query`** — general-purpose MCP tool that executes an
    OpenSearch DSL query (supplied as a JSON string) against any index pattern
    in a configurable allow-list.  Arguments: `index_pattern`, `query` (DSL
    JSON string), optional `max_hits` (1–100, default 10), optional
    `source_fields` projection.  Returns `{"hits": [...], "total": N,
    "took_ms": N, "aggregations": {...}}`.  Uses `ASKPANDA_OPENSEARCH`
    (shared read credential with harvester timeseries).  Allow-list controlled
    by `BAMBOO_OPENSEARCH_ALLOWED_INDICES` (default:
    `atlas_harvesterworkers-*,bamboomcp-promptlog-*`).

  - **`opensearch_promptlog_query`** — convenience wrapper pre-wired to
    `bamboomcp-promptlog-*` with the three large text fields
    (`system_prompt`, `user_prompt`, `response`) excluded from results by
    default.  Rich schema description in the tool definition lets the LLM
    construct useful queries without knowing the field names.  Supports
    session replay, tool-usage analytics, token cost comparisons, and
    per-provider breakdowns.

  - **`core/bamboo/llm/opensearch_client.py`** — new shared client factory
    (`create_os_client(password)`) used by all three OpenSearch paths (prompt
    log write, harvester timeseries read, general read).  Eliminates the
    duplicate connection logic previously duplicated between `prompt_log.py`
    and `harvester_timeseries_impl.py`.

  - Registered in `core.py` `TOOLS` dict.  25 new unit tests in
    `tests/test_opensearch_query.py` covering allow-list logic, error
    handling, max_hits clamping, aggregation passthrough, and the
    promptlog-query projection defaults.

  - `docs/opensearch.md` extended with a "Read queries from Bamboo" section:
    example DSL queries, architecture diagram, and "Adding a new index"
    instructions.

### Changed

- **`prompt_log._create_os_client` and
  `harvester_timeseries_impl.create_os_client` now delegate to the shared
  `bamboo.llm.opensearch_client.create_os_client` factory.**  Both functions
  are preserved at their original names for backward compatibility; no
  call-site or test changes are required.



### Added

- **OpenSearch prompt-log self-observability and analytics.**  Bamboo can now
  answer questions about its own usage — turn counts, session replay, FAQ
  analysis, tool call frequency, model/provider breakdowns — by querying the
  `bamboomcp-promptlog-*` index directly from the TUI or Streamlit.

  - **`bamboo_promptlog_rate` MCP tool.**  Rates a logged response (1–5 stars)
    by applying a partial `update` to the existing OpenSearch document.
    `prompt_log.py` gains `_last_doc_store` (deque maxlen=1),
    `get_last_doc_id()`, and `update_rating(index, doc_id, rating)`.
    The `rating` field (integer, nullable) is included in the index template
    mapping.  Uses the write credential (`BAMBOO_OPENSEARCH_PROMPTLOG`).

  - **`prompt_log.py` — index mapping and timestamp fixes.**
    `_ensure_index_template()` applies a `bamboomcp-promptlog` index template
    on the first write of each process, ensuring `@timestamp` is always mapped
    as `date` (not auto-detected as `text`).  This fixes date-range queries
    such as `gte:now/d` that silently returned zero results when the mapping
    was wrong.  Timestamps changed from `isoformat()+00:00` to explicit
    `Z`-suffix `strftime` format (`strict_date_optional_time` canonical form).
    Notification messages now include `session=` so the UUID is visible
    directly in the TUI system panel for use in session-replay queries.

  - **Promptlog fast-path routing in `bamboo_answer.py`.**
    `_is_promptlog_question()` detects self-observability queries
    (FAQ, session replay, tool-usage analytics, turn counts, model queries)
    and routes them directly to `opensearch_promptlog_query` via a new rule 7
    in `_build_deterministic_plan`, before the doc-search RAG fallback (which
    becomes rule 8).  `_build_promptlog_plan()` helper extracted consistent
    with `_build_code_query_plan`.  `# noqa: C901` added to
    `_build_deterministic_plan` (intentional dispatcher).

  - **Topic guard self-observability terms.**  `topic_guard.py` gains a
    `# Bamboo self-observability` block in `_ALLOW_TERMS` covering `session`,
    `turns`, `bamboo`, `opensearch`, `which model`, `tool usage`, `faq`, and
    related phrases.  These now fast-path to `keyword_allow` without invoking
    the LLM classifier, preventing prompt-log queries from being incorrectly
    rejected as off-topic.

  - **`opensearch_promptlog_query` description improvements.**  Accumulated
    fixes to the LLM-facing tool description across multiple iterations:
    OpenSearch date-math rules (`now/d`, `now-7d/d`); `size:0` rules
    (display queries must omit `size`; aggregation-only queries use
    `size:0 + source_fields=[]`); `total` field semantics (pre-size-limit
    document count, not value_count result); `session_id.keyword` fallback
    for indices created before the template; mandatory `user_prompt.keyword`
    for terms aggregations (without `.keyword` the field is tokenised,
    producing word-level buckets instead of full-question buckets);
    multi-user deployment note (cross-session queries must omit
    `session_id` filter); explicit FAQ examples.

- **LaTeX formula rendering in Streamlit.**  `_normalise_latex()` in
  `interfaces/streamlit/chat.py` converts common LLM LaTeX delimiter styles
  (`\[ \]`, `\( \)`, bare `[ ]` with a backslash in the content) to the
  `$$...$$` / `$...$` forms that Streamlit's built-in KaTeX renderer
  understands.  Applied to every assistant message before `st.markdown()`.
  No new dependencies — KaTeX is bundled in Streamlit.
  12 unit tests in `tests/test_normalise_latex.py`.

- **Slash commands — TUI.**  New commands added to `/help`:

  | Command | Action |
  |---|---|
  | `/faq [today\|week\|month]` | Most frequently asked questions from prompt logs; default scope is all time |
  | `/rate <1-5>` | Rate the most recent response; submits `bamboo_promptlog_rate` with the `(index, doc_id)` extracted from the last notification |

  `/rate` confirmation displays `★☆☆☆☆`–`★★★★★` stars inline.

- **Slash commands — Streamlit.**  `_expand_slash_command()` intercepts slash
  commands at the `st.chat_input` level before submission to the MCP server.
  `/help` and unknown commands render as inline assistant messages with no
  server round-trip.  Commands supported:

  | Command | Action |
  |---|---|
  | `/help`, `/?` | Formatted markdown command reference |
  | `/faq [today\|week\|month]` | Most frequently asked questions |
  | `/task <id>` | Summarise task status |
  | `/job <id>` | Analyse job failure |
  | `/rate <1-5>` | Rate the last response |

- **Star rating widget — Streamlit.**  `_render_rating_widget()` displays five
  colour-coded buttons below each assistant response: 🔴 1, 🟠 2, 🟡 3, 🟢 4,
  💚 5.  Clicking submits `bamboo_promptlog_rate` and reruns; the selected star
  is shown bold and a caption confirms the rating (e.g. "Your rating: ⭐⭐⭐⭐
  — Good (4/5)").  Widget is suppressed when `bamboo_promptlog_rate` is not
  registered on the server or no document has been indexed yet.
  `(index, doc_id)` is extracted from `bamboo_promptlog_status` notification
  events and stored in `st.session_state["last_doc_id"]`.

### Fixed

- **Prompt-log queries routing to RAG instead of OpenSearch.**  Questions such
  as *"show me the frequently asked questions"*, *"how many turns today?"*, and
  *"what was the last question I asked?"* were falling through to the doc-search
  fallback because `_build_deterministic_plan` had no promptlog routing rule.
  Fixed by the new rule 7 described above.

- **FAQ aggregation returning wrong counts.**  `terms` aggregations on the
  `user_prompt` field (a `text` type) bucket on individual tokens rather than
  full question strings, causing *"What is PanDA?"* (asked 4 times) to appear
  as three separate single-occurrence buckets.  Fixed by enforcing
  `user_prompt.keyword` in the tool description and in the `/faq` command
  question text.

- **Session replay and turn queries returning zero results.**  Two causes:
  (1) The `@timestamp` field was auto-mapped as `text` on indices created
  before the template fix, silently breaking `range`/date-math queries.
  Fixed by the index template.  (2) `term:{session_id:...}` queries returned
  zero on such indices because `session_id` was also mapped as `text`.
  `session_id.keyword` fallback documented in the tool description.

- **`test_superuser_guard.py` failures when run after `test_normalise_latex`.**
  `_import_normalise_latex` was leaving `interfaces.shared` stubbed as a plain
  `types.ModuleType` in `sys.modules`, causing subsequent imports of
  `interfaces.shared.superuser_guard` to return `MagicMock` objects.  Fixed by
  wrapping stub injection in a `try/finally` that restores `sys.modules` to its
  original state after the import.



### Added

- **`/script [filename]` TUI command.**  Extracts fenced code blocks from the
  last assistant response and writes them to the current working directory.
  Filename resolution order: (1) user-supplied argument, (2) label in the
  response body (`Script: foo.py`, `File: foo.py`, `Save the script as foo.C`,
  code fence with inline filename), (3) auto-generated from language + timestamp.
  Multiple blocks are written with numeric suffixes, each using its own detected
  language extension (`.py`, `.cpp`, `.sh`, `.C`, etc.).  If the user supplies
  a filename without an extension, the first block's language extension is appended
  automatically.  Added to `/help`.

- **Streamlit download button.**  `_render_script_download()` detects fenced code
  blocks in the last assistant message and renders `st.download_button` for each.
  File content is streamed directly to the browser — no server-side file is written.
  Correct approach for browser-based deployment.  Suggested filename is honoured
  using the same resolution order as the TUI `/script` command.

- **`/script` in Streamlit slash commands.**  Typing `/script` in the Streamlit
  chat input displays an explanation of the download button mechanism.

### Fixed

- **`/rates` missing entries (exists filter).**  `range:{rating:{gte:1}}` silently
  skips documents where the `rating` field is absent.  Changed to
  `exists:{field:rating}` so all rated documents are returned regardless of how the
  field was mapped.  `max_hits` set explicitly to 50.

- **`/rates` 400 parsing_exception.**  The submitted question was embedding `max_hits`
  and `source_fields` inside the JSON query body, causing OpenSearch to return
  `Unknown key for a START_OBJECT in [bool]`.  Rewrote the question text to label
  each argument separately and provide the DSL body as a standalone JSON string.

- **`/rates` showing only top entries.**  `user_prompt` (full synthesis prompt) was
  included in `source_fields`, consuming large amounts of context window and causing
  early truncation.  Replaced with `raw_question` (short original question).

- **`/rates` wrong extensions on multi-block output.**  When `/script calc.py` was
  used with a response containing multiple code blocks (Python + C++ + bash), all
  subsequent blocks inherited the `.py` extension from the user-supplied filename
  instead of using their own detected language extension.  Each block now uses its
  own `_lang_to_extension` result for blocks after the first.

- **`/script` missing extension when user omits it.**  `/script rnd` produced a file
  named `rnd` with no extension.  If the user-supplied filename has no extension,
  the first block's detected language extension is now appended automatically.

- **`/script` not honouring "Save the script as X.C" pattern.**  The
  `_extract_suggested_filename` function did not match the LLM's common phrasing
  *"Save the script as random_numbers.C"*.  Added a `save_re` pattern matching
  `save/name/call ... as <filename.ext>` (case-insensitive) as a fourth extraction
  strategy, after `Script:/File:` labels and inline fence filenames.

- **ROOT `.C` extension missing.**  Added `"root": ".C"` to both the TUI
  `_lang_to_extension` map and the Streamlit `_LANG_EXT_MAP` so ROOT macro
  code blocks get the correct `.C` extension in auto-named output.

- **`/rates` and "show me all the rates" misrouted to PanDA jobs.**  Promptlog
  routing was checked after the topic guard, which could substitute the original
  question with a prior PanDA-domain turn (context bleed).  Moved
  `_is_promptlog_question` into `_run_fast_path_intercepts`, before the topic
  guard, so the original question text is always used.

- **Rating vocabulary not in routing signals.**  "Show me all the rates from today"
  was misrouted to PanDA job tools because "rates" is ambiguous.  Added `rating`,
  `ratings`, `rated`, `star rating`, `lowest rated`, `highest rated`, `average
  rating` to `_PROMPTLOG_SIGNALS`, `_PROMPTLOG_PHRASES`, and `topic_guard`
  `_ALLOW_TERMS`.

- **`raw_question.keyword` for accurate FAQ aggregations.**  `user_prompt` contains
  the full synthesis context (question + evidence) and is unique per turn even for
  identical questions — aggregating on `user_prompt.keyword` produces no useful
  frequency data.  Added `raw_question: str | None` to `log_prompt()` and threaded
  it from `execute_plan()` via `call_llm()`.  `raw_question` stores the user's
  original typed question and its `.keyword` sub-field enables correct `terms`
  aggregations for `/faq`.  Updated all FAQ examples and `/faq` command text to
  use `raw_question.keyword`.



## v1.0.7 — 2026-05-15

### Added

- **`cgsim.sim_query` — natural-language to SQL tool for the CGSim simulation
  output database (`packages/askcgsim/`).** Answers questions about a CGSim
  simulation run by translating natural-language questions into SQL, executing
  them read-only against the local SQLite database, and summarising the results
  in natural language via a second LLM call.

  New files:

  | File | Purpose |
  |---|---|
  | `askcgsim/sim_query_schema.py` | SQL guard (AST allow-list), schema context string, and LLM prompt builders for both the SQL-generation and summarisation calls. Zero bamboo-core dependency. |
  | `askcgsim/sim_query_impl.py` | Full NL→SQL→execute→NL pipeline. Both LLM calls are async; SQLite execution runs synchronously on the event loop thread (consistent with the DuckDB precedent in `panda_jobs_query`). |
  | `askcgsim/sim_query.py` | Thin re-export wrapper with `ImportError` fallback if `sqlglot` is absent. |
  | `askcgsim/cgsim_reader.py` | `cgsim_reader.py` vendored from the `sqlite-reader` repository. Provides typed structured access to the EVENTS table via `CGSimReader` and `EventRow`. |
  | `tests/test_sim_query.py` | 64 unit tests covering the guard (every rejection rule, LIMIT injection, aggregation cap, CTE allowance), the full pipeline (happy path, cannot-answer, guard rejection, execution error, wrong database, summarisation failure, truncation), `CgsimSimQueryTool.call()`, schema context caching, and prompt builder shape. |

  Security — four independent read-only layers:

  1. SQLite URI `file:{path}?mode=ro` — the driver refuses any write at the OS level.
  2. `PRAGMA query_only = ON` — a second enforcement inside the SQLite library.
  3. sqlglot AST guard (`validate_and_guard`) — parses with the SQLite dialect;
     enforces single statement, SELECT-only root, no forbidden constructs at any
     AST depth, no system tables (`sqlite_master`, `sqlite_sequence`, …), and a
     table allow-list (`events` only). Queries without a LIMIT get `LIMIT 200`
     injected; aggregation queries (`GROUP BY`) get `LIMIT 1000`.
  4. Local-only deployment — `CGSIM_DB_PATH` is a local filesystem path.

  `pyproject.toml` changes: `cgsim.sim_query` entry point uncommented;
  `sqlglot>=25.0` added as a package dependency.

- **`cgsim.sim_query` documentation** — `docs/cgsim-database.md`,
  `docs/tools/cgsim_sim_query.md`; updated `docs/tools/README-mcp_tools.md`,
  `docs/question-cheatsheet.md`, `README.md`.

### Fixed

- **`cgsim.sim_query` routing** — `plugin_id` was not reaching the fast-path
  interceptors. `_run_fast_path_intercepts` and `_run_db_query_fast_path` had no
  `plugin_id` parameter, so every `_build_deterministic_plan` call inside them
  defaulted to `"atlas"` and routed simulation questions to `panda_jobs_query`.
  `plugin_id` is now threaded through the full chain:
  `_route()` → `_run_fast_path_intercepts` → `_run_db_query_fast_path` →
  `_build_deterministic_plan`. The CGSim branch in `_build_deterministic_plan`
  was also moved before the `_is_jobs_db_question` check so it takes priority.

- **LLM planner routing for CGSim (fast-path off)** — the planner had no
  plugin awareness, causing it to select `panda_jobs_query` for simulation
  questions when `BAMBOO_FAST_PATH=0`. Two changes:
  - `plugin_id` is now passed from `bamboo_answer.call()` through `plan_args`
    to `bamboo_plan_tool.call()` and into `build_planner_system_prompt()`.
  - `build_planner_system_prompt` now dispatches to a plugin-specific prompt
    builder. The CGSim prompt (`_build_cgsim_planner_prompt`) contains no PanDA
    vocabulary — it only knows `cgsim.sim_query`, `cgsim.doc_search`, and
    `cgsim.doc_bm25`, with clear guidance to prefer `cgsim.sim_query` for any
    simulation-data question.

- **Wrong-database error handling** — when the file at `CGSIM_DB_PATH` exists
  but contains no `EVENTS` table (empty file, wrong database), the tool now
  returns a specific `_wrong_database_evidence` error message rather than the
  generic "query could not be executed" message.

- **SQL generation prompt — ambiguous follow-up questions** — added explicit
  examples for "show me all jobs" / "list all job IDs" (`SELECT DISTINCT
  JOB_ID FROM EVENTS`) and strengthened the prompt to state that `EVENTS` is
  the only permitted table. Without this, follow-up questions like "show me all
  jobs" were generating PanDA-schema SQL (`SELECT * FROM jobs`) which the AST
  guard correctly blocked.

- **Summarisation prompt — tie/uniform distribution** — added an explicit
  instruction to report when all rows share the same ranked value (e.g. all
  sites have the same job count) rather than reporting only the top row as if
  it were uniquely the winner.

### Improved

- **`cgsim.sim_query` tracing** — three sub-spans are now emitted inside
  `fetch_and_analyse` so `/tracing` shows the breakdown between the two LLM
  calls and the SQLite execution:
  - `cgsim.sim_query/sql_generation` (`llm_call`) — SQL generation latency and
    token counts.
  - `cgsim.sim_query/sqlite_execute` (`tool_call`) — SQLite execution time and
    row count.
  - `cgsim.sim_query/summarisation` (`llm_call`) — summarisation latency and
    token counts.
  All three spans correctly wrap the operation they measure (the `generate()`
  call is now *inside* the `async with span(...)` block).

- **Planner tracing fix** — the `bamboo_plan` `llm_call` span previously
  recorded 0 ms because `client.generate()` was called *before* the span
  opened. Fixed by moving `generate()` inside the span, consistent with the
  corrected `cgsim.sim_query` spans.

- **`cgsim.sim_query` synthesis bypass** — when `cgsim.sim_query` returns a
  non-null `summary`, the executor now returns it directly without a redundant
  `bamboo_llm_answer` synthesis call. This saves ~3 seconds per query (one
  full LLM round-trip). The synthesis span is still emitted with
  `bypass="cgsim_summary"` for tracing consistency. The bypass falls through to
  normal synthesis if `summary` is null (e.g. summarisation LLM failure).

- **TUI fallback banner** — replaced the AskPanDA ASCII art in `FALLBACK_BANNER`
  with "Bamboo MCP" ASCII art (standard figlet font, 5-line layout matching the
  original height). Also updated all transient UI strings that previously said
  "AskPanDA": the default `display_name`, input placeholder, response panel
  title, and both error-fallback display names in `_load_banner`. Plugin-specific
  banners (e.g. AskCGSim) still override the fallback once `ui_manifest` loads.

- **Agent live progress callback** (`interfaces/agent/agent.py`,
  `scripts/bamboo_agent.py`): `BambooAgent` now accepts an optional
  `progress_callback: Callable[[str], None]` parameter.  The callback is
  invoked at each key moment in the ReAct loop — tool discovery, step start,
  tool call, observation size, eval result, and synthesis — so callers can
  display live status without coupling the agent to any output channel.
  `AgentResult` gains a new `llm_calls: int` field counting every
  `bamboo_llm_answer` MCP call made during the run (reason + evaluate +
  synthesise combined).  Step progress display changed from `Step X/Y` to
  `Step X (max Y)` to clarify that `max_steps` is a ceiling, not a target.
  A `✔  Done — N step(s), M LLM call(s), confidence=F` line is emitted after
  synthesis completes.
- **Agent CLI progress display and `--quiet` flag** (`scripts/bamboo_agent.py`):
  live progress is now shown on stderr by default using `\r` overwrite (single
  tidy line, no scrolling).  Line width is capped to the terminal width via
  `shutil.get_terminal_size()`.  The `--quiet` flag suppresses all progress
  output, useful when piping stdout or capturing JSON output.  `llm_calls=N`
  is included in both the printed footer line and `--output-json` output.
  The module docstring now contains a full annotated table of the MCP HTTP
  message sequence (POST/GET/DELETE) so operators understand what each server
  log line represents.

---

## 2026-05-12  Security — four independent read-only layers:

  1. SQLite URI `file:{path}?mode=ro` — the driver refuses any write at the OS level.
  2. `PRAGMA query_only = ON` — a second enforcement inside the SQLite library.
  3. sqlglot AST guard (`validate_and_guard`) — parses with the SQLite dialect;
     enforces single statement, SELECT-only root, no forbidden constructs at any
     AST depth, no system tables (`sqlite_master`, `sqlite_sequence`, …), and a
     table allow-list (`events` only). Queries without a LIMIT get `LIMIT 200`
     injected; aggregation queries (`GROUP BY`) get `LIMIT 1000`.
  4. Local-only deployment — `CGSIM_DB_PATH` is a local filesystem path.

  Pipeline: LLM call 1 (temperature 0.0, 512 tokens) generates SQL using a
  system prompt that embeds the full EVENTS schema, all METADATA fields by
  event type, `json_extract()` guidance, the `CANNOT_ANSWER` sentinel, explicit
  exclusion of the uncalibrated `cost` field, and eight worked example patterns.
  The generated SQL is fence-stripped, checked for refusals, and passed through
  the AST guard before execution. LLM call 2 (temperature 0.2, 1024 tokens)
  receives the original question, the executed SQL, and the raw results as JSON,
  and returns a natural-language summary with correct units. LLM call 2 is
  non-fatal: if it fails, the raw evidence dict is still returned with
  `summary: null`.

  `pyproject.toml` changes: `cgsim.sim_query` entry point uncommented;
  `sqlglot>=25.0` added as a package dependency.

- **`cgsim.sim_query` documentation.**

  | File | Description |
  |---|---|
  | `docs/cgsim-database.md` | Full reference: EVENTS schema, all METADATA fields by event type, total wall-clock time formula, eight example questions with generated SQL, four-layer security model, two-LLM-call pipeline diagram, and configuration. |
  | `docs/tools/cgsim_sim_query.md` | Tool reference card: purpose, inputs, data source, pipeline summary, guard rules table, full output key reference, configuration, key design notes. |

  Updated files:

  - `docs/tools/README-mcp_tools.md` — new "CGSim simulation data tools"
    section with `cgsim.sim_query`; CGSim plugin table updated.
  - `docs/question-cheatsheet.md` — new `cgsim.sim_query` section with six
    themed question groups (job timing, site analysis, network congestion, I/O
    bottleneck, job health, full job timeline).
  - `README.md` — `docs/cgsim-database.md` added to the docs table;
    `cgsim.sim_query` added to the AskCGSim plugin tools table; status blurb
    updated to reflect the new tool.

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

- **`cric_query`: copytool follow-up questions routed to RAG instead of CRIC
  (bamboo_answer.py).** Questions like "Are any other sites using object
  stores?" or "Which sites use rucio?" were not recognised as CRIC questions
  because copytool names and object-store vocabulary were absent from
  `_CRIC_SIGNALS`. Added `"objectstore"`, `"object store"`, `"gfalcopy"`,
  `"rucio copytool"`, `"using rucio"`, `"using objectstore"`, and
  `"using gfal"` to `_CRIC_SIGNALS` so these route directly to `cric_query`
  without depending on the narrower follow-up regex.

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

- **CGSim plugin (`packages/askcgsim/`).** A new Bamboo MCP plugin for the
  CGSim / SimGrid distributed computing simulator. CGSim is a SimGrid-based
  framework for simulating large-scale computing grids such as the WLCG; it
  ingests historical PanDA job records for calibration and is designed to
  simulate infrastructures managed by PanDA.

  Entry points registered under `bamboo.tools`:

  | Entry point | Tool name | Description |
  |---|---|---|
  | `cgsim.doc_search` | `cgsim.doc_search` | ChromaDB vector similarity search over CGSim / SimGrid documentation |
  | `cgsim.doc_bm25` | `cgsim.doc_bm25` | BM25 keyword search over the same corpus |
  | `cgsim.ui_manifest` | `cgsim.ui_manifest` | TUI branding: block-letter banner, green accent, "Bamboo – AskCGSim" display name |

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

- **AskCGSim synthesis prompts updated to welcome PanDA/CGSim correlation.**
  The initial CGSim prompts instructed the LLM to avoid framing answers in
  terms of PanDA or ATLAS. This was over-cautious: CGSim ingests PanDA job
  records for calibration and users legitimately ask about the integration.
  All three AskCGSim synthesis prompts and the `_PLUGIN_IDENTITY["cgsim"]` string
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
| `packages/askcgsim/askcgsim/__init__.py` | AskCGSim plugin package |
| `packages/askcgsim/askcgsim/doc_rag.py` | `cgsim.doc_search` tool |
| `packages/askcgsim/askcgsim/doc_bm25.py` | `cgsim.doc_bm25` tool |
| `packages/askcgsim/askcgsim/ui_manifest.py` | `cgsim.ui_manifest` tool |
| `packages/askcgsim/askcgsim/banner.txt` | 6-line block-letter CGSim banner |
| `packages/askcgsim/pyproject.toml` | Plugin entry points and metadata |
| `packages/askcgsim/tests/test_cgsim_plugin.py` | 30 tests covering all three tools |
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
