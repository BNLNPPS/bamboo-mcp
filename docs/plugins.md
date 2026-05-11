# Writing plugins for Bamboo

Plugins provide domain-specific functionality.

Each plugin is a separate Python package with its own `pyproject.toml` and registers tools via entry points.

## Entry points

Plugins register tools under the `bamboo.tools` entry-point group:

```toml
[project.entry-points."bamboo.tools"]
"atlas.task_status" = "askpanda_atlas.task_status:panda_task_status_tool"
```

Naming convention:

- Entry point name: `<namespace>.<tool_name>`
- Example: `atlas.task_status`

## Tool contract

A tool should expose:

```python
def get_definition() -> dict
async def call(arguments: dict) -> dict
```

Return shape:

```json
{
  "text": "Human-readable summary or tool output",
  "evidence": { "structured": "data" }
}
```

## Namespaces

Namespaces prevent collisions and make it clear which plugin owns a tool:

- `atlas.*` — ATLAS / PanDA tooling
- `cgsim.*` — SimGrid / CGSim tooling
- `verarubin.*` — Vera Rubin tooling
- `epic.*` — EPIC / EIC tooling

## Plugin tool filtering

The `list_tools` MCP handler only exposes tools whose namespace matches the
active plugin, set via `ASKPANDA_PLUGIN` (default: `atlas`). The namespace is
the part of the entry-point key before the first dot.

**Why this matters:** every tool description in `list_tools` is sent to the LLM
on every call. Exposing all plugins' tools to every user wastes tokens and
increases cost — an ATLAS user should never pay for CGSim tool descriptions.

**What is filtered:** only entry-point plugin tools (those with a `namespace.name`
key). Core tools (`bamboo_health`, `bamboo_answer`, etc.) are always included
regardless of plugin.

**What is not affected:** `call_tool` — all installed plugin tools remain
callable regardless of `ASKPANDA_PLUGIN`. The filter is only on `list_tools`.

**Implication for new plugins:** the namespace in the entry-point key
(`"cgsim.doc_search"`) must exactly match the value users will set in
`ASKPANDA_PLUGIN` (`"cgsim"`). If these don't match, the plugin's tools will
never appear in `list_tools`.

**Switching plugins** without restarting the server: change `ASKPANDA_PLUGIN`
and start a new client session. The TUI caches the tool list at connect time,
so an existing TUI session will not see the change until reconnected.
