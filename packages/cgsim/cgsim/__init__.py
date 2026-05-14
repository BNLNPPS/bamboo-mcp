"""AskCGSim Bamboo plugin package.

Provides MCP tools for querying CGSim documentation and (in future releases)
the SQLite simulation output database produced by CGSim runs.

Entry points registered under ``bamboo.tools``:

``cgsim.doc_search``
    ChromaDB vector-similarity search over CGSim / SimGrid documentation.

``cgsim.doc_bm25``
    BM25 keyword search over the same corpus.

``cgsim.ui_manifest``
    UI branding metadata consumed by the Textual TUI and Streamlit interface.
"""
