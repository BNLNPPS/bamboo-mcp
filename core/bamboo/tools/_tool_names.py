"""Single source of truth for tool names.

Bamboo names the same tool in three places, and until this module existed the
three could disagree without anything failing loudly:

* the **wire name** — what ``list_tools`` returns and therefore the only name
  an MCP client can actually call;
* the **planner catalog** — what the LLM planner is shown and told it may
  propose; and
* **internal literals** — the exact strings ``bamboo_executor`` compares
  ``called_tool_names`` against to pick a specialist synthesis prompt or a
  bypass.

The wire name is the canon here, because it is the only one of the three a
caller outside the process can observe.

Deriving it is not a formatting rule.  ``bamboo.core`` exposes a plugin tool
under its *entry-point key* (``atlas.core_dump_analysis``), except when the
tool's own ``get_definition()["name"]`` is already registered in the built-in
``TOOLS`` dict, in which case the entry point is skipped entirely and the
built-in name wins (``atlas.log_analysis`` is never on the wire; callers use
``panda_log_analysis``).  Any consumer that reimplements that rule will drift
from it.  :func:`wire_tool_definitions` is that rule, written once.

:func:`canonical_tool_name` inverts the same map so every accepted spelling of
a tool collapses to its wire name.  That matters because ``_execute_one_tool``
keys ``_last_evidence_store`` on the name the *plan* used: a plan naming
``core_dump_analysis`` runs the same tool as one naming
``atlas.core_dump_analysis``, but stored its evidence under a key no reader
looks up, so core-dump synthesis was skipped and the analyzer's JSON fell
through to generic prose synthesis.
"""
from __future__ import annotations

from typing import Any, cast

from bamboo.tools.loader import find_tool_by_name, list_tool_entry_points


def _advertised_name(obj: Any) -> str:
    """Return a tool object's own ``get_definition()["name"]``.

    Args:
        obj: Tool object, expected to expose ``get_definition()``.

    Returns:
        The advertised name, or ``""`` when the object has no usable
        definition.  Callers treat ``""`` as "skip this tool" rather than
        raising, so one malformed plugin cannot break discovery for the rest.
    """
    get_def = getattr(obj, "get_definition", None)
    if not callable(get_def):
        return ""
    try:
        raw = get_def()
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(cast("dict[str, Any]", raw).get("name") or "")


def _builtin_definitions() -> dict[str, dict[str, Any]]:
    """Return the built-in ``TOOLS`` definitions keyed by advertised name.

    The import of :data:`bamboo.core.TOOLS` is deferred to call time because
    ``bamboo.core`` imports this module's sibling ``loader`` at module level;
    importing it back at module scope here would close the cycle.

    Returns:
        Mapping of advertised MCP name to definition dict.  Empty when
        ``bamboo.core`` cannot be imported, which keeps this module usable in
        the plugin-only test environments that never import core.
    """
    try:
        from bamboo.core import TOOLS  # pylint: disable=import-outside-toplevel
    except Exception:  # pylint: disable=broad-exception-caught
        return {}

    out: dict[str, dict[str, Any]] = {}
    for tool in TOOLS.values():
        get_def = getattr(tool, "get_definition", None)
        if not callable(get_def):
            continue
        try:
            raw = get_def()
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if not isinstance(raw, dict):
            continue
        defn = cast("dict[str, Any]", raw)
        name = str(defn.get("name") or "")
        if name:
            out[name] = defn
    return out


def wire_tool_definitions() -> list[dict[str, Any]]:
    """Return plugin tool definitions under the names clients actually see.

    Discovers tools from the ``bamboo.tools`` entry-point group and applies the
    two registration rules ``bamboo.core`` applies when building the MCP server:

    1. An entry point whose key collides with a built-in ``TOOLS`` key, or
       whose tool advertises a name already registered in ``TOOLS``, is
       dropped — the built-in registration already covers it.  This is why
       ``atlas.log_analysis`` never appears: ``panda_log_analysis`` is built in.
    2. Every surviving definition is renamed to its fully-qualified
       entry-point key, so ``core_dump_analysis`` is exposed as
       ``atlas.core_dump_analysis``.

    Definitions are copied before renaming.  A tool whose ``get_definition``
    returns a module-level constant rather than a fresh dict would otherwise be
    permanently renamed in-place by the first call.

    Returns:
        List of definition dicts, each with ``name`` set to the wire name.
        Built-in tools are **not** included; callers that need the full wire
        surface combine this with ``bamboo.core.TOOLS``.
    """
    covered: set[str] = set(_builtin_definitions())

    try:
        from bamboo.core import TOOLS  # pylint: disable=import-outside-toplevel
        builtin_keys: frozenset[str] = frozenset(TOOLS)
    except Exception:  # pylint: disable=broad-exception-caught
        builtin_keys = frozenset()

    defs: list[dict[str, Any]] = []
    for ep in list_tool_entry_points():
        full_name = str(ep.get("name") or "")
        if not full_name or full_name in builtin_keys or "." not in full_name:
            continue
        namespace, tool_name = full_name.split(".", 1)

        resolved = find_tool_by_name(tool_name, namespace=namespace)
        if resolved is None:
            continue

        get_def = getattr(resolved.obj, "get_definition", None)
        if not callable(get_def):
            continue
        try:
            raw = get_def()
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if not isinstance(raw, dict):
            continue

        defn = dict(cast("dict[str, Any]", raw))
        if str(defn.get("name") or "") in covered:
            continue

        defn["name"] = full_name
        defs.append(defn)
    return defs


def _build_alias_map() -> dict[str, str]:
    """Build the alias-to-wire-name mapping from scratch.

    Separated from :func:`alias_map` so the cache has something to call.  This
    is expensive — it loads every entry point in the ``bamboo.tools`` group —
    which is why the public accessor caches.

    Returns:
        Mapping of alias to wire name.
    """
    claims: dict[str, set[str]] = {}

    def _claim(alias: str, wire: str) -> None:
        if alias:
            claims.setdefault(alias, set()).add(wire)

    builtins = _builtin_definitions()
    for name in builtins:
        _claim(name, name)

    try:
        from bamboo.core import TOOLS  # pylint: disable=import-outside-toplevel
        for key, tool in TOOLS.items():
            advertised = _advertised_name(tool) or key
            _claim(key, advertised)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    for ep in list_tool_entry_points():
        full_name = str(ep.get("name") or "")
        if not full_name or "." not in full_name:
            continue
        namespace, tool_name = full_name.split(".", 1)
        resolved = find_tool_by_name(tool_name, namespace=namespace)
        if resolved is None:
            continue
        advertised = _advertised_name(resolved.obj)
        # A tool whose advertised name is built in keeps the built-in name on
        # the wire; otherwise the entry-point key is the wire name.
        wire = advertised if advertised in builtins else full_name
        _claim(full_name, wire)
        _claim(advertised, wire)
        _claim(tool_name, wire)

    return {
        alias: next(iter(wires))
        for alias, wires in claims.items()
        if len(wires) == 1
    }


#: Memoised alias map.  ``None`` means "not built yet"; an empty map is a
#: legitimate result (no plugins installed) and must not force a rebuild.
_ALIAS_CACHE: dict[str, str] | None = None


def alias_map() -> dict[str, str]:
    """Return a mapping of every accepted tool spelling to its wire name.

    Three spellings are accepted for a plugin tool: its entry-point key, the
    name it advertises in its own definition, and — where unambiguous — the
    bare suffix after the namespace.  All three resolve through
    ``_resolve_tool``, so all three can reach ``_last_evidence_store`` as keys
    unless they are collapsed first.

    An alias claimed by two different wire names is **omitted** rather than
    resolved arbitrarily.  ``doc_search`` is the live candidate: both
    ``atlas.doc_search`` and ``epic.doc_search`` shorten to it, and guessing
    between them would route one plugin's evidence to the other's reader.
    Omitted aliases fall through :func:`canonical_tool_name` unchanged, which
    is the pre-existing behaviour.

    The result is memoised because building it loads every entry point in the
    group — around 230 ms — and :func:`canonical_tool_name` is called once per
    tool call on the execution path.  Entry points cannot change without a
    process restart, so the cache has no natural invalidation; tests that patch
    discovery call :func:`reset_alias_cache`.

    Returns:
        A fresh dict mapping alias to wire name.  Every wire name maps to
        itself.  A copy is returned so a caller mutating the result cannot
        corrupt the cache.
    """
    global _ALIAS_CACHE  # pylint: disable=global-statement
    if _ALIAS_CACHE is None:
        _ALIAS_CACHE = _build_alias_map()
    return dict(_ALIAS_CACHE)


def reset_alias_cache() -> None:
    """Discard the memoised alias map.

    Only needed by tests that patch entry-point discovery, and by any caller
    that installs a plugin into a running process.

    Returns:
        None.
    """
    global _ALIAS_CACHE  # pylint: disable=global-statement
    _ALIAS_CACHE = None


def canonical_tool_name(name: str, namespace: str | None = None) -> str:
    """Collapse any accepted spelling of a tool to its wire name.

    Args:
        name: Tool name as written by a plan, a planner response or a client.
        namespace: Optional namespace hint, used only to disambiguate a bare
            suffix (``"doc_search"`` with ``namespace="atlas"``).

    Returns:
        The wire name, or *name* unchanged when it is unknown or ambiguous.
        Returning the input rather than raising keeps this safe to apply on
        the execution path: an unknown name still reaches ``_resolve_tool``,
        which reports it as unknown with the spelling the caller used.
    """
    if not name:
        return name
    aliases = alias_map()
    if namespace and "." not in name:
        qualified = f"{namespace}.{name}"
        if qualified in aliases:
            return aliases[qualified]
    return aliases.get(name, name)


__all__ = [
    "alias_map",
    "canonical_tool_name",
    "reset_alias_cache",
    "wire_tool_definitions",
]
