# `analyze_core_dump.py`

Standalone CLI for analysing a PanDA payload core dump: drives `gdb` in batch
mode, normalises the output into a bounded evidence bundle, and optionally asks
an LLM to explain it.

The same code backs the `atlas.core_dump_analysis` MCP tool. This document
covers the CLI; the tool's own reference is
[`docs/tools/core_dump_analysis.md`](../docs/tools/core_dump_analysis.md).

## Where the code lives

`scripts/analyze_core_dump.py` is a thin wrapper. The implementation is
`packages/askpanda_atlas/askpanda_atlas/_core_dump_analyzer.py`.

That split is deliberate. The MCP tool imports `reconcile_llm_analysis`,
`core_evidence_from_dict`, `build_system_prompt`, `build_user_prompt` and
`extract_json_object` from the module, and the ATLAS plugin's `pyproject.toml`
lists its packages explicitly — so a module outside `askpanda_atlas/` would be
missing from any non-editable install, working in a source checkout and failing
only in the container image. The wrapper keeps the script discoverable and
runnable from `scripts/` without a second copy of 4000 lines.

Both of these work:

```bash
python scripts/analyze_core_dump.py core.18277 --mode hang
python -m askpanda_atlas._core_dump_analyzer core.18277 --mode hang
```

The wrapper prefers an installed `askpanda_atlas` and only falls back to the
checkout layout, so a deployed environment is never shadowed by whichever
working copy you happen to run from.

## Quick start

```bash
# Evidence only — no LLM, no API key needed. Start here.
python scripts/analyze_core_dump.py core.18277 --no-llm --json evidence.json

# Full analysis, letting the backend pick the provider (see below).
python scripts/analyze_core_dump.py core.18277 --mode hang

# Inside the job's own ATLAS release container (the usual case for a real job).
python scripts/analyze_core_dump.py core.18277 \
    --execution atlas-container --job-dir /path/to/job --mode hang
```

`--help` lists every option.

## The executable matters

`gdb` needs the ELF binary that was running, **not** the script. For an
`athena.py` job that is the Python interpreter, and it must be the same build
the job used — normally from CVMFS. Passing `--exe /path/to/athena.py` produces
a useless analysis, not an error.

The analyzer recovers the correct path automatically from the core's `NT_FILE`
note and only falls back to `--exe`, so you rarely need to supply it. When it
cannot, the report says `Executable: UNRESOLVED` and the evidence-quality
warnings explain what that costs.

Build IDs are compared where `eu-unstrip` is available. A mismatch means `gdb`
read symbols from a different build than the one that produced the core, so
frame names and line numbers can be confidently wrong. That appears as an
evidence-quality warning and caps the reported confidence.

## Execution backends

| `--execution` | What it does | When to use it |
|---|---|---|
| `local` (default) | Runs `gdb` in the current environment | The core was produced by software you already have set up |
| `atlas-container` | Sets up the job's release via `atlasLocalSetup.sh` and runs the collector inside the matching container | Analysing a real ATLAS job's core |

`atlas-container` requires, in order: `ATLAS_LOCAL_ROOT_BASE` set,
`/cvmfs/atlas.cern.ch` readable, `$ALRB/user/atlasLocalSetup.sh` present,
`apptainer` resolvable, and `gdb` on `PATH` inside the container. It also needs
`my_release_setup.sh` in `--job-dir` (or an explicit `--release-setup`), and the
core must live under `--job-dir`, which is mounted at `/srv`.

The original `container_script.sh` is deliberately not executed. The analyzer
runs its own worker inside the container instead.

Note that `--job-dir` must be **writable**: the container backend writes a
temporary worker, runner and evidence file there and removes them afterwards
(keep them with `--keep-container-artifacts` when debugging a container run
that fails).

## LLM backends

Synthesis is not tied to one provider. `--llm-backend` selects where it runs:

| Value | Behaviour | Credentials |
|---|---|---|
| `auto` (default) | Uses `bamboo` when Bamboo is importable, otherwise `anthropic` | Whichever the resolved backend needs |
| `bamboo` | Goes through Bamboo's configured provider — Anthropic, OpenAI, Gemini, Mistral or an OpenAI-compatible endpoint | `LLM_DEFAULT_PROVIDER` plus that provider's key variable |
| `anthropic` | Calls the Anthropic SDK directly | `ANTHROPIC_API_KEY` |

The `bamboo` backend selects the *reasoning* profile, since core-dump synthesis
is the same class of work as log analysis. It is synchronous and will refuse to
run inside an existing event loop; async callers should collect evidence with
`--no-llm` and synthesise through their own provider stack.

Model resolution, in order: `--model`, then `CORE_ANALYSIS_MODEL`, then
`LLM_DEFAULT_MODEL`. If none is set, the backend keeps its own default — the
profile's model for `bamboo`, `claude-sonnet-4-6` for `anthropic`.

### If you write your own synthesis

Apply `reconcile_llm_analysis()` to the model's structured reply. It is not
cosmetic: it is what stops EventLoop completion markers being read as evidence
that a looping PanDA job finished normally. Dropping it reproduces a
misdiagnosis this analyzer previously shipped, where a job killed by the
looping-job detector was reported as having completed successfully because its
payload log said `worker finished successfully`.

`build_system_prompt`, `build_user_prompt`, `extract_json_object`,
`core_evidence_from_dict` and `reconcile_llm_analysis` are the intended
embedding surface. `render_report` is not — it is this CLI's fixed-width
presentation; render from the dicts instead.

## Crash mode versus hang mode

`--mode` frames the analysis and is inferred from the terminating signal by
default:

- **crash** (`SIGSEGV`, `SIGBUS`, `SIGFPE`, `SIGILL`, `SIGSYS`, `SIGTRAP`) — a
  genuine fault. The faulting thread's frame is the subject.
- **hang** (`SIGQUIT`, `SIGABRT`, `SIGTERM`, `SIGKILL`, `SIGUSR1/2`, `SIGINT`) —
  a supervisor snapshotted or killed a process that was still running. This is
  the looping-job case: the question is what every thread was *waiting on*.

Hang mode changes log correlation as well as framing. The pilot's own log is
excluded from automatic discovery, because pilot termination records describe
what the pilot did *after* deciding the payload was looping, not what the
payload was doing before the core was captured. `--job-log` remains an escape
hatch for any file, including `pilotlog.txt`.

## Log correlation and file modification times

The analyzer correlates the core with payload logs found next to it. Its most
diagnostic observation for a looping job is of the form:

> `payload.stdout` was last modified 2h 09m 34s before the core capture

That figure is computed from **filesystem modification times** — the core's
against each log's. Anything that stages these files must therefore preserve
their original mtimes. Freshly copied or downloaded files all carry mtime ≈ now,
so the silence computes as roughly zero and the observation silently disappears
rather than failing.

Related: the recency window that filters stale logs under `workDir/` is anchored
on the newest **non-empty payload stream**, not on the core. For a looping job
the payload has by definition been silent for a long time before the core is
captured, so a window measured from the core would discard exactly the logs that
were active when the payload stopped.

## Output

- **stdout** — the fixed-width report.
- `--json FILE` — the full bundle: `{"schema_version", "tool_version",
  "evidence", "analysis"}`. Consumers should key on `schema_version`, which
  changes only when the payload shape changes incompatibly; `tool_version`
  moves for unrelated reasons.
- `--raw-gdb FILE` — the unprocessed `gdb` transcript, redacted.

Credentials are scrubbed from anything that leaves the process: private keys,
certificates, bearer tokens, JWTs, X.509 proxy paths, `*TOKEN`/`*PASSWORD`/
`*SECRET`/`*API_KEY` assignments and Anthropic keys. `--no-redact` disables
this; do not use it on output you intend to share.

## Cost and time

`gdb` reloads the whole core once per analysis phase, so wall-clock time scales
with both core size and phase count. A ~1 GB core takes roughly a minute
end to end including synthesis. Cores above 1024 MiB print a one-time note
saying so; `-v` adds a periodic heartbeat during long phases, which is the
answer to "is it frozen?".

Evidence sent to the model is bounded by `--max-evidence-chars` (default 50000)
with a hard multiplier applied to the rendered prompt as defence in depth. The
budget is applied to a copy, so the report and JSON stay complete.

## Tests

```bash
cd packages/askpanda_atlas && python -m pytest tests/test_core_dump_analyzer.py
```

No real `gdb` and no network: the gdb-driving code is exercised against captured
output and the provider SDKs are stubbed.

One test, `test_saved_looping_cases_share_family_but_have_distinct_subtypes`,
reads two validated looping-job evidence bundles from `/mnt/data/`. It fails
where those files are absent rather than skipping, which is intentional — a
silently-skipped regression test is how duplicated-code drift went unnoticed
here before.
