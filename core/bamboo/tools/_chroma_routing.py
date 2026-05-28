"""Blue/green ChromaDB collection name resolver.

The ``bamboo-mcp-services`` document-monitor agent stores vectors in a pair of
physical ChromaDB collections (``<name>__a`` and ``<name>__b``) and rotates
between them atomically on each update cycle.  Which slot is currently live is
recorded in a JSON sidecar file:

.. code-block:: json

    {
        "atlas_docs": "atlas_docs__a",
        "epic_docs":  "epic_docs__b"
    }

The sidecar is written by the agent using ``os.replace`` (POSIX atomic rename)
so it is never partially written from a reader's perspective.

This module provides :func:`resolve_collection`, a **read-only** helper that
translates a logical collection name (e.g. ``"atlas_docs"``) to the currently
live physical name (e.g. ``"atlas_docs__a"``).  It is intentionally standalone
— it does not import from ``bamboo_mcp_services`` — so Bamboo MCP remains
independent of the services package.

Fallback behaviour
------------------
If the sidecar is absent, unreadable, or has no entry for the requested logical
name, :func:`resolve_collection` returns the logical name unchanged.  This
means:

- Deployments that have not yet upgraded to the blue/green agent continue to
  work without any configuration change.
- A transient sidecar read error (e.g. during a write) degrades gracefully
  rather than taking the RAG tool offline.

Live re-resolution
------------------
:func:`resolve_collection` re-reads the sidecar on **every call**.  This is
intentional: the sidecar is a tiny JSON file (< 1 KB) and reading it is
cheap compared with a ChromaDB query.  Re-reading on every call means that
when the document-monitor agent swaps the active slot the MCP tools pick up
the new collection on the next query without requiring a server restart.

Callers that cache a ``chromadb.Collection`` handle should compare the
resolved physical name against the name of the cached handle and invalidate
the cache when they differ.  :class:`~bamboo.tools.doc_rag.PandaDocSearchTool`
does this via its ``_resolved_physical`` attribute.
"""
from __future__ import annotations

import json
import logging
import os

LOG = logging.getLogger(__name__)

#: Name of the routing sidecar file, relative to the ChromaDB directory.
ROUTING_SIDECAR = "collection_routing.json"


def resolve_collection(chroma_path: str, logical_name: str) -> str:
    """Resolve a logical ChromaDB collection name to its current live physical name.

    Reads ``<chroma_path>/collection_routing.json`` written by the
    ``bamboo-mcp-services`` document-monitor agent.  If the sidecar is absent,
    unreadable, or contains no entry for *logical_name*, the logical name is
    returned unchanged (graceful fallback for deployments that do not yet use
    blue/green rotation).

    This function re-reads the sidecar file on every call so that a live swap
    by the document-monitor agent is picked up immediately without a server
    restart.

    Args:
        chroma_path: Path to the ChromaDB persistent directory (the same value
            as ``BAMBOO_CHROMA_PATH``).
        logical_name: The logical collection name as configured via
            ``BAMBOO_CHROMA_COLLECTION`` (e.g. ``"atlas_docs"``).

    Returns:
        The physical ChromaDB collection name to open (e.g. ``"atlas_docs__a"``),
        or *logical_name* unchanged if no routing record is found.
    """
    sidecar = os.path.join(chroma_path, ROUTING_SIDECAR)
    try:
        with open(sidecar, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        # Normal for deployments not yet on the blue/green agent.
        return logical_name
    except Exception:  # pylint: disable=broad-exception-caught
        # Corrupt sidecar, permission error, etc. — degrade gracefully.
        LOG.warning(
            "_chroma_routing: failed to read sidecar '%s'; using logical name '%s'",
            sidecar, logical_name,
        )
        return logical_name

    physical = data.get(logical_name)
    if not physical:
        # No entry for this logical name — new corpus not yet swapped, or a
        # deployment with a custom collection name that predates the sidecar.
        return logical_name

    if physical != logical_name:
        LOG.debug(
            "_chroma_routing: '%s' → '%s' (via sidecar)",
            logical_name, physical,
        )
    return physical
