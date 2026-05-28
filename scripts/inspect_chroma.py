"""Inspect a ChromaDB persistent store and report all collections.

Lists every collection found at the given ChromaDB path together with its
document count and a short sample of document IDs.  Highlights which
collection the RAG and BM25 tools will actually query so you can quickly
spot mismatches between what was ingested and what is visible to the tools.

Usage::

    python scripts/inspect_chroma.py [--path PATH] [--collection COLLECTION]

Arguments
---------
--path PATH
    Path to the ChromaDB persistent directory.
    Defaults to the value of ``BAMBOO_CHROMA_PATH``, then ``./chroma_db``.

--collection COLLECTION
    Collection name the RAG / BM25 tools are configured to use.
    Defaults to the value of ``BAMBOO_CHROMA_COLLECTION``, then ``bamboo_docs``.

Examples
--------
Use environment-configured defaults::

    python scripts/inspect_chroma.py

Override path and collection::

    python scripts/inspect_chroma.py --path ~/data/my_chroma --collection panda_docs

Show which source files were ingested into the active collection::

    python scripts/inspect_chroma.py --sources
"""
from __future__ import annotations

import argparse
import os
import sys

_DEFAULT_CHROMA_PATH = "./chroma_db"
_DEFAULT_CHROMA_COLLECTION = "bamboo_docs"
_SAMPLE_IDS = 5  # how many document IDs to show per collection


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed namespace with ``path`` and ``collection`` attributes.
    """
    parser = argparse.ArgumentParser(
        description="Inspect a ChromaDB store and list all collections.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--path",
        default=os.getenv("BAMBOO_CHROMA_PATH", _DEFAULT_CHROMA_PATH),
        help=(
            f"ChromaDB persistent directory "
            f"(default: $BAMBOO_CHROMA_PATH or '{_DEFAULT_CHROMA_PATH}')"
        ),
    )
    parser.add_argument(
        "--collection",
        default=os.getenv("BAMBOO_CHROMA_COLLECTION", _DEFAULT_CHROMA_COLLECTION),
        help=(
            f"Collection name the RAG/BM25 tools query "
            f"(default: $BAMBOO_CHROMA_COLLECTION or '{_DEFAULT_CHROMA_COLLECTION}')"
        ),
    )
    parser.add_argument(
        "--sources",
        action="store_true",
        help=(
            "Also list the unique source files ingested into the active collection, "
            "taken from the 'source_file' metadata field on each document."
        ),
    )
    return parser.parse_args()


def _print_collection_table(collections: list, active_collection: str) -> bool:
    """Print a table of all collections and return whether the active one was found.

    Args:
        collections: List of ChromaDB Collection objects.
        active_collection: Name of the collection the tools are configured to use.

    Returns:
        True if the active collection was present in the list.
    """
    print(f"{'Collection':<40} {'Documents':>10}  {'Active?':>8}")
    print("-" * 65)

    found_active = False
    for col in sorted(collections, key=lambda c: c.name):
        try:
            count = col.count()
        except Exception:  # noqa: BLE001
            count = -1

        is_active = col.name == active_collection
        if is_active:
            found_active = True
        marker = "  ← tools use this" if is_active else ""
        count_str = str(count) if count >= 0 else "error"
        print(f"  {col.name:<38} {count_str:>10}{marker}")

        if count > 0:
            try:
                sample = col.get(limit=_SAMPLE_IDS, include=[])
                ids = sample.get("ids", [])
                for doc_id in ids:
                    print(f"    {'':38} {doc_id}")
                if count > _SAMPLE_IDS:
                    print(f"    {'':38} … ({count - _SAMPLE_IDS} more)")
            except Exception:  # noqa: BLE001
                pass

    print()
    return found_active


def _print_diagnosis(client: object, collections: list, active_collection: str, found_active: bool) -> None:
    """Print a diagnosis line explaining whether the tools will find their data.

    Args:
        client: Open ChromaDB PersistentClient.
        collections: List of ChromaDB Collection objects.
        active_collection: Name of the collection the tools are configured to use.
        found_active: Whether the active collection was found in the store.
    """
    if not found_active:
        print(f"WARNING: collection '{active_collection}' was NOT found in this store.")
        print()
        available = [c.name for c in collections]
        if available:
            print("Available collections:")
            for name in available:
                print(f"  - {name}")
            print()
            print("Fix: set BAMBOO_CHROMA_COLLECTION to one of the above,")
            print("     or re-ingest your documents into the expected collection.")
        else:
            print("The store is empty — ingest documents first.")
        return

    import chromadb as _chromadb  # noqa: PLC0415
    col_obj = _chromadb.PersistentClient  # type hint placeholder
    try:
        col_obj = client.get_collection(name=active_collection)  # type: ignore[assignment]
        count = col_obj.count()
    except Exception:  # noqa: BLE001
        count = -1

    if count == 0:
        print(f"WARNING: collection '{active_collection}' exists but contains 0 documents.")
        print("         Ingest documents before querying.")
    elif count < 0:
        print(f"WARNING: could not count documents in '{active_collection}'.")
    else:
        print(f"OK: '{active_collection}' is present with {count} document(s).")


def _print_sources(client: object, collection_name: str) -> None:
    """Print the unique source files present in a collection's metadata.

    Fetches all document metadata from *collection_name* and extracts the
    ``source_file`` field (the key used by the standard Bamboo ingestion
    agent).  Reports each unique source with its chunk count so you can
    confirm exactly which documents were ingested.

    Args:
        client: Open ChromaDB PersistentClient.
        collection_name: Name of the collection to inspect.
    """
    try:
        col = client.get_collection(name=collection_name)  # type: ignore[union-attr]
        total = col.count()
        if total == 0:
            print("  (collection is empty)")
            return
        # Fetch all metadata in one shot (no embeddings or documents needed).
        result = col.get(include=["metadatas"])
        metadatas = result.get("metadatas") or []
    except Exception as exc:  # noqa: BLE001
        print(f"  error reading metadata: {exc}")
        return

    sources: dict[str, int] = {}
    unknown = 0
    for meta in metadatas:
        src = (meta or {}).get("source_file")
        if src:
            sources[src] = sources.get(src, 0) + 1
        else:
            unknown += 1

    if not sources and not unknown:
        print("  No metadata found.")
        return

    print(f"Ingested sources in '{collection_name}' ({total} chunks total):\n")
    for src in sorted(sources):
        print(f"  {sources[src]:>6} chunk(s)  {src}")
    if unknown:
        print(f"  {unknown:>6} chunk(s)  (no source_file metadata)")
    print()


def main() -> None:
    """Open the ChromaDB store and print a report of all collections.

    Raises:
        SystemExit: On missing dependency, missing path, or ChromaDB error.
    """
    args = _parse_args()
    chroma_path: str = args.path
    active_collection: str = args.collection

    # --- dependency check ---------------------------------------------------
    try:
        import chromadb  # type: ignore[import-untyped]
    except ImportError:
        print("error: chromadb is not installed.  Run: pip install chromadb", file=sys.stderr)
        sys.exit(1)

    # --- path check ---------------------------------------------------------
    if not os.path.exists(chroma_path):
        print(f"error: ChromaDB path not found: '{chroma_path}'", file=sys.stderr)
        print("       Set --path or BAMBOO_CHROMA_PATH to the correct directory.", file=sys.stderr)
        sys.exit(1)

    # --- connect ------------------------------------------------------------
    try:
        client = chromadb.PersistentClient(path=chroma_path)
    except Exception as exc:  # noqa: BLE001
        print(f"error: failed to open ChromaDB at '{chroma_path}': {exc}", file=sys.stderr)
        sys.exit(1)

    # --- list collections ---------------------------------------------------
    try:
        collections = client.list_collections()
    except Exception as exc:  # noqa: BLE001
        print(f"error: could not list collections: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"ChromaDB path : {os.path.abspath(chroma_path)}")
    print(f"Active collection (RAG/BM25 tools will query): '{active_collection}'")

    # Resolve the logical name to the current live physical slot.
    try:
        from bamboo.tools._chroma_routing import resolve_collection  # noqa: PLC0415
        resolved_collection = resolve_collection(chroma_path, active_collection)
    except ImportError:
        resolved_collection = active_collection

    if resolved_collection != active_collection:
        print(
            f"  → resolved via collection_routing.json to: '{resolved_collection}'"
        )
    print()

    if not collections:
        print("No collections found in this store.")
        print()
        print("This means no documents have been ingested yet, or the path is wrong.")
        return

    found_active = _print_collection_table(collections, resolved_collection)
    _print_diagnosis(client, collections, resolved_collection, found_active)

    if args.sources and found_active:
        print()
        _print_sources(client, resolved_collection)


if __name__ == "__main__":
    main()
