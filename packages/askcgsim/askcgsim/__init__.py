"""AskCGSim Bamboo plugin package.

Provides MCP tools for querying CGSim documentation and the SQLite simulation
output database produced by CGSim runs.

Entry points registered under ``bamboo.tools``:

``cgsim.doc_search``
    ChromaDB vector-similarity search over CGSim / SimGrid documentation.

``cgsim.doc_bm25``
    BM25 keyword search over the same corpus.

``cgsim.ui_manifest``
    UI branding metadata consumed by the Textual TUI and Streamlit interface.

``cgsim.sim_query``
    Natural-language to SQL tool for querying the CGSim simulation output
    SQLite database.  Translates questions into SQL, validates them through a
    four-layer security guard (SQLite read-only URI, ``PRAGMA query_only``,
    sqlglot AST validation, and table allow-list), executes against the local
    database, and summarises results in natural language.  Requires the
    ``CGSIM_DB_PATH`` environment variable to be set.
"""
