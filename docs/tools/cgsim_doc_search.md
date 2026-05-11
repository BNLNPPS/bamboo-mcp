# `cgsim.doc_search`

**Package:** `cgsim`
**Entry point:** `cgsim.doc_search`
**Module:** `cgsim.doc_rag`
**Type:** Documentation retrieval — vector similarity search

---

## Purpose

`cgsim.doc_search` searches the CGSim and SimGrid documentation for conceptual
questions, how-to guidance, configuration options, and explanations of
simulation behaviour.  It uses vector similarity search against a pre-built
ChromaDB collection.

Use it for questions about *how CGSim or SimGrid works*, not questions about
live simulation state (use a future `cgsim.sim_query` tool for that).

Typical questions:
- "How do I write a CGSim plugin?"
- "What is the SimGrid netzone model?"
- "How does CGSim calibrate job wall time?"
- "What methods must a plugin override?"
- "How does the real-time monitoring dashboard work?"
- "What output does CGSim write to SQLite?"

Complements [`cgsim.doc_bm25`](cgsim.doc_bm25.md) — use both together for
best coverage (vector search handles semantic similarity; BM25 handles
exact-match and enumeration queries).

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | Yes | Natural-language search query. |
| `top_k` | integer | No (default `5`) | Number of document chunks to return. |

---

## Data source

A ChromaDB persistent collection.  Configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `BAMBOO_CHROMA_PATH` | `./chroma_db` | Path to the ChromaDB persistent directory. |
| `BAMBOO_CHROMA_COLLECTION` | `cgsim_docs` | Collection name to query. |

The ChromaDB client is initialised lazily on the first call and cached on the
tool instance.  If `chromadb` is not installed, or the configured path does not
exist, the tool returns a human-readable error rather than raising.

The `cgsim_docs` default is intentionally different from the ATLAS
(`atlas_docs`) and ePIC (`epic_docs`) defaults so all three corpora can coexist
in the same ChromaDB directory.

---

## Output

A text block listing the top matching document chunks with their metadata and
distance scores.  Each chunk includes:

- Document text (truncated to `_SNIPPET_MAX_CHARS = 500` characters).
- Source file or URL (from metadata).
- Distance score (lower = more similar).

---

## Dependencies

- `chromadb` — ChromaDB client library (`pip install -r requirements-rag.txt`)
- Embedding model consistent with the one used during ingestion (default:
  `all-MiniLM-L6-v2`, 384-dimensional vectors)

---

## See also

- [`cgsim.doc_bm25`](cgsim.doc_bm25.md) — keyword BM25 search over the same corpus
- [`docs/rag.md`](../rag.md) — RAG pipeline architecture, ingestion, and configuration
