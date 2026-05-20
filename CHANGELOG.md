# Changelog

All notable changes to Bamboo are documented here.

---

## [Unreleased]

### Added

- **PanDA MCP OIDC token file support.** `panda_mcp_session.py` now reads
  the `id_token` field from the OIDC token cache file written by
  `get-panda-token` (from the `panda-mcp-client` package).  Token resolution
  order: (1) `PANDA_MCP_TOKEN` env var, (2) `id_token` from the file at
  `PANDA_MCP_TOKEN_FILE` (default `~/.panda_id_token`), (3) no token for
  public endpoints.  A new `_read_token_file()` helper handles JSON parsing
  and all failure modes (missing file, malformed JSON, missing field) with
  WARNING-level log messages rather than crashes.  Token renewal will be
  handled by a forthcoming Bamboo MCP agent service.

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
