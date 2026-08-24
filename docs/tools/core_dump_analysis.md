# `atlas.core_dump_analysis`

**Package:** `askpanda_atlas`
**Modules:** `askpanda_atlas.core_dump_analysis`, `askpanda_atlas.core_dump_analysis_impl`, `askpanda_atlas._core_dump_worker`, `askpanda_atlas._core_dump_analyzer`
**Type:** Post-mortem analysis — gdb against a PanDA payload core dump

> Call this tool by its **entry point name**, `atlas.core_dump_analysis`. The
> MCP server overwrites a plugin tool's internal `name` with the entry point
> key, so `core_dump_analysis` alone does not resolve.

---

## Purpose

`atlas.core_dump_analysis` reconstructs a failed PanDA job's working directory
from BigPanDA, runs `gdb` against its core file inside the matching ATLAS
release container, and returns **deterministic evidence**: the faulting or
stalled thread's backtrace, thread groupings, loaded libraries, and how long
the payload had been silent when the core was written.

It answers a question that log analysis structurally cannot. A looping-job kill
(pilot error code 1150) happens precisely because the payload stopped producing
log output — so the log ends before the interesting part. The core is the only
record of what the process was actually doing at the moment it was killed.

This tool is **ATLAS-only**. `_CORE_DUMP_ANALYSIS_AVAILABLE` is `False` in the
ePIC mirror and the tool is deliberately not sed-substituted into
`askpanda_epic`: the analysis needs CVMFS, apptainer and gdb on the host, plus
an ATLAS release container that matches the job.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `job_id` | integer | Yes | PanDA job ID (`pandaid`) whose core dump to analyse. |
| `action` | string | No | `start` (default) begins or re-uses an analysis and waits briefly; `status` reports progress; `result` returns the evidence. |
| `mode` | string | No | `auto` (default), `hang` or `crash`. `auto` derives the framing from the job's pilot error code: 1150 is a looping-job kill and is analysed as a hang. |
| `request_id` | string | No | Run identifier returned by a previous `start` call. |
| `restart` | boolean | No | Re-run even when a completed analysis of this job already exists. Off by default, since a core is expensive to fetch. |

---

## Output

A JSON-serialised `{"evidence": {...}, "text": "..."}` payload.

| Key | Description |
|---|---|
| `job_id` | The queried job ID. |
| `request_id` | Handle for this run, for a later `status` or `result` call. |
| `state` | `queued`, `preparing`, `downloading`, `analyzing`, `complete` or `failed`. |
| `elapsed_s` | Seconds since the run started. |
| `progress` | Human-readable progress line for a non-terminal state. |
| `failure_mode` | The resolved framing, `hang` or `crash`. |
| `mode_source` | Whether the mode was requested or derived from the pilot error code. |
| `workspace` | On-disk workspace directory. |
| `monitor_url` | BigPanDA job page. |
| `acquisition` | `{bytes_downloaded, fetched, created_empty, skipped_count, skipped_sample, warnings}`. |
| `core_evidence` | The analyzer's own evidence dict. Present only on `complete`. |
| `core_evidence_schema_version` | Schema version of `core_evidence`. |
| `analyzer_version` | Version of the vendored analyzer. |
| `error` | Present only on `failed`. |

There is **no** `analysis` key. This tool never calls an LLM — see below.

---

## Execution model

A core is routinely a gigabyte, so the work runs in a **detached worker**
process and the tool waits inline for a bounded period.

```
start → acquire the single slot → spawn worker → wait up to 120 s
    finished inside the window  → full result in the same turn
    still running               → request_id, ask for the result later
```

The manifest file *is* the state store. There is no in-process registry, so a
server restart mid-run loses nothing: `status` still answers from disk.

| Setting | Default | Environment variable |
|---|---|---|
| Inline wait | 120 s | `BAMBOO_CORE_ANALYSIS_INLINE_WAIT` |
| Hard timeout | 900 s | `BAMBOO_CORE_ANALYSIS_HARD_TIMEOUT` |
| Container deadline | 600 s | `BAMBOO_CORE_ANALYSIS_CONTAINER_TIMEOUT` |
| Workspace root | `/tmp/bamboo/core-analysis` | `BAMBOO_CORE_ANALYSIS_ROOT` |
| Disk quota | 50 GiB | `BAMBOO_CORE_ANALYSIS_QUOTA_BYTES` |
| Container runtime override | *(discovered)* | `BAMBOO_CORE_DUMP_APPTAINER` |
| Skip the runtime check | off | `BAMBOO_CORE_DUMP_SKIP_RUNTIME_CHECK` |
| Evidence budget | 120 000 chars | `BAMBOO_CORE_ANALYSIS_MAX_EVIDENCE_CHARS` |
| CPython gdb helper dir | *(searched)* | `BAMBOO_CORE_DUMP_PYTHON_GDB` |

The 120 s inline wait assumes the 300 s `BAMBOO_MCP_CLIENT_TIMEOUT` default. If
a deployment pins that lower, lower the inline wait to match or `start` will
time out on the wire before it can hand back a handle.

### Replay of a completed analysis

`start` returns the stored evidence when a manifest is `complete`, rather than
refetching the core. The payload carries `replayed: true` and the text says so,
naming when the run finished — because gdb did not run, so nothing in the
evidence reflects the present host, container or analyzer version.

Rule 1c sets `restart` when the question asks for a fresh run ("re-run",
"again", "redo", "from scratch"). It is never inferred: a restart spends a
gigabyte transfer and holds the single analysis slot.

### Container runtime detection

The preflight refuses before the lock and before any download, so a host that
cannot run the release container says so in the same turn rather than three
minutes into a gigabyte transfer. It checks CVMFS three ways — the mount, its
readability, and `atlasLocalSetup.sh` — and then looks for a container runtime
through four avenues, in order:

1. `BAMBOO_CORE_DUMP_APPTAINER`, when set to an executable file.
2. `apptainer` or `singularity` on the server process's own `PATH`.
3. Either name on a **login** shell's `PATH`, probed once per process with
   `bash -lc 'command -v …'`.
4. ALRB's own apptainer, under `containers/sw/apptainer` in the CVMFS
   repository holding `ATLASLocalRootBase`.

Avenue 3 is the one that matches reality. `_collect_evidence_atlas_container`
starts the release with `bash -lc`, so the `PATH` that decides whether the
analysis can run is a login shell's — assembled from `/etc/profile.d` — not the
narrower one an MCP server inherits from systemd. Avenue 4 matters because ALRB
supplies its own runtime when the host has none, so "no apptainer on this host"
does not imply "no apptainer for the container setup".

A negative probe result is cached for the life of the process. A host that
gains a runtime needs the server restarted.

None of this weakens the no-fallback rule: detection decides whether the
*container* can start, never whether to run gdb without one.

### Evidence budget

`load_core_evidence` is the **only** place the character budget is applied on
this path. The analyzer runs with `--no-llm`, so it never calls
`enforce_global_budget` itself: `evidence.json` on disk carries the full
per-section evidence and `gdb_raw.txt` is not budgeted at any point. Whatever
the budget trims is trimmed from the model's copy alone and stays recoverable
from the workspace.

The reduction cascade spends the budget cheapest-evidence-first and ends at
`primary_thread.backtrace`. That last stage is reached only when the seven
before it have hit their floors — which a job with many shared libraries and
several distinct thread stacks will do. Job 7272161793 did: at the analyzer's
50 000-character CLI default, the XRootD shutdown chain from `Py_Exit` down to
`PollerBuiltIn::Stop` was cut out of the model's copy. The default here is
120 000, and `truncated_sections` reaches the model either way, so a trimmed
analysis always says so.

### Python-level backtraces

`py-bt` needs CPython's gdb helper, which gdb normally auto-loads as
`<objfile>-gdb.py` next to each loaded object. ATLAS/LCG releases do not
reliably ship one: for job 7272161793 gdb's own `info auto-load python-scripts`
listed exactly one script, libstdc++'s, so auto-load was healthy and simply had
no libpython candidate.

The analyzer therefore searches explicitly, in a bootstrap that runs before
`py-bt`: an explicit `--python-gdb-helper` first, then `<objfile>-gdb.py` and
`share/gdb/auto-load/` for every loaded `libpython*` or `python3*` object. What
it searched, and the CPython minor version detected from the core, are recorded
in `gdb_metadata.python_helper` and named in the `python.reason` text — so "not
found", "not looked for" and "found but wrong version" are all distinguishable.

**The helper is version-specific.** It reads CPython's own struct layouts, and
those change between minor versions — 3.12's frame representation is not 3.11's.
A single fixed path is therefore the wrong shape for a deployment that analyses
jobs from several releases. `BAMBOO_CORE_DUMP_PYTHON_GDB` accepts a *directory*
of per-version helpers:

```
/data/bamboo/tools/cpython-gdb/3.11/python-gdb.py
/data/bamboo/tools/cpython-gdb/3.12.13/python-gdb.py
```

The bootstrap detects the version from the core's own interpreter object, takes
the matching subdirectory (exact `X.Y`, then any `X.Y.*`), and declines rather
than loading a mismatched helper. A single file is still accepted for a
deployment where every job uses the same interpreter.

**The path is read on the host and the helper is copied into the job
directory**, which the container sees at `/srv`. It cannot travel as an
environment variable: ALRB launches apptainer with `--cleanenv` and `--contain`,
binding only `/cvmfs`, the user's home, the job directory and a scratch path, so
a helper at, say, `/data/bamboo/tools` does not exist as far as the bootstrap is
concerned. Staged copies join the worker and runner scripts in the same
cleanup — removed on success, kept on failure.

Version detection tries three routes in order: a version in the loaded object's
basename (`libpython3.13.so.1.0`), a version in its path
(`lib/python3.13/lib-dynload/...`), and finally a hint read from the job
directory — the payload's own stdout carries `PYTHONPATH`, which on an ATLAS
release names `lib/python3.13/site-packages`. The third exists because an
interpreter packaged as a plain `python` executable carries no version at all.
Both `python-gdb.py` and `libpython.py` are accepted as filenames.

A helper that loads but yields no frames is reported as such, naming the
detected version, rather than as "this is likely not a Python process". The two
usual causes are a minor-version mismatch and a `libpython` with no DWARF for
the helper to read interpreter structures from.

Only **one** analysis runs at a time, guarded by a lock file whose *content*
records the holder. A second request while one is in flight is refused with a
deterministic message naming the running job.

Nothing here deletes anything — not partial downloads, not failed workspaces,
not superseded evidence. Reaping is a separate concern and deliberately absent.

---

## Why the tool calls no LLM

`_complete_via_bamboo` in the analyzer refuses to run inside a live event loop,
and its own error text names the alternative: an async caller should collect
evidence with `--no-llm` and synthesise through its own provider stack. This
tool is that async caller.

Synthesis therefore lives in `bamboo_executor._synthesise_core_dump`, alongside
the prompt log and the model configuration. That function:

1. Returns the tool's own progress or error line verbatim for any state other
   than `complete` — there is no evidence to reason about, and an LLM call
   could only paraphrase a status message or invent findings.
2. On `complete`, drives `build_system_prompt(mode)` / `build_user_prompt(...)`
   over a `core_evidence_from_dict(...)`, parses the JSON reply, and calls
   **`reconcile_llm_analysis()`**.
3. Appends `acquisition.warnings` to `analysis["limitations"]` deterministically,
   *after* reconciliation.
4. Renders Markdown.

Step 2's reconciliation is not optional. It is what stops the model reading
EventLoop completion markers as evidence that a looping job exited normally —
the markers describe event-processing state, not payload exit.

Step 3's ordering matters: `reconcile_llm_analysis` filters list entries that
read as claims of normal job success, so appending warnings first would let a
legitimate acquisition warning be filtered back out.

`core_evidence` has **already** been through `enforce_global_budget` at 50 000
characters inside the tool. Do not shrink it a second time.

---

## Routing

Two entry points, in `bamboo_answer`:

**Rule 1c — explicit request.** A job ID plus a core-dump signal (`core dump`,
`gdb`, `backtrace`, `core file`) or a "what was it doing / stuck on" phrasing.
Sits ahead of rule 1 (`panda_log_analysis`) because "analyse the core dump of
job X" also matches `_is_log_analysis_request`. Passes `mode="auto"`, since an
explicit request may name any job.

A bare "why did job X hang" is deliberately **not** a rule 1c signal. It is a
diagnosis request, and a multi-gigabyte core fetch is the wrong opening move
for a question a log analysis usually answers outright — and when it does not,
that log analysis makes the offer rule 1d picks up.

**Rule 1d — accepting the offer.** `panda_log_analysis` emits
`core_dump_offer_md` for a looping-job kill with a usable core. A reply that is
nothing but an affirmative ("yes", "ok", "go ahead") starts the analysis of the
offered job, recovering the job ID from the stored evidence. Passes
`mode="hang"`, since the offer only ever follows pilot code 1150.

Rule 1d runs in `_route` via `_run_early_intercepts`, **before** the social
intercept and the topic guard — not in `_build_deterministic_plan` with rule
1c. Both layers would otherwise consume it: `_is_ack` matches "ok", "okay",
"great" and "perfect", and the topic guard rewrites content-free follow-ups
before the deterministic planner ever sees them.

Rule 1d is also, unlike rule 1c, **not** gated on `bypass_fast_path`
(`/fastpath off`). That flag exists to send a *question* to the LLM planner
instead of a deterministic rule, and a bare "yes" is not such a question: the
topic guard reformulates it into a documentation query before any planner sees
it, so skipping rule 1d does not reroute the turn, it destroys it. Resolving an
offer that an earlier turn made deterministically is conversational state
resolution, in the same class as the social replies, not a routing shortcut.

---

## Registration points

Adding or changing this tool touches five files:

| File | What |
|---|---|
| `packages/askpanda_atlas/pyproject.toml` | Entry point `atlas.core_dump_analysis` |
| `core/bamboo/tools/bamboo_answer.py` | Rules 1c and 1d |
| `core/bamboo/tools/planner.py` | Tool catalog and routing guidance |
| `core/bamboo/tools/bamboo_executor.py` | Synthesis branch, presentation key |
| `interfaces/streamlit/chat.py` | `_PLOT_UNSUPPORTED_TOOLS` |

`pip install -e packages/askpanda_atlas/` is required after any entry-point
change, even in an editable install.

---

## Related

- [`scripts/README-core_dump_analysis.md`](../../scripts/README-core_dump_analysis.md) — the standalone CLI over the same analyzer.
- [`panda_log_analysis.md`](panda_log_analysis.md) — produces the core-dump probe and the offer.
