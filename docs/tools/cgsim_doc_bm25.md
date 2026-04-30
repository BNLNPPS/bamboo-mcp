# `cgsim.doc_bm25`

**Package:** `cgsim`
**Entry point:** `cgsim.doc_bm25`
**Module:** `cgsim.doc_bm25`
**Type:** Documentation retrieval — BM25 keyword search

---

## Purpose

`cgsim.doc_bm25` performs BM25 keyword search over the same documentation
corpus used by [`cgsim.doc_search`](cgsim.doc_search.md).  It excels at
exact-match and enumeration queries where vector similarity search may miss
precise terms.

Use it (alongside `cgsim.doc_search`) for:
- "What are the arguments to `assignJob`?"
- "List all virtual methods in the plugin base class."
- "What does `getResourceInformation` return?"
- "Which JSON keys are required in the site configuration file?"
- Any query where the exact identifier string matters more than semantic
  similarity.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | Yes | Keyword search query. |
| `top_k` | integer | No (default `10`) | Number of document chunks to return. |

---

## Data source

The same ChromaDB collection as `cgsim.doc_search`.  All documents are loaded
from the collection on the first call, tokenised with a lowercase
alphanumeric splitter (`[a-z0-9_]+`), and indexed with `BM25Okapi` from the
`rank_bm25` package.  The index and corpus are cached in-process.  The cache
is automatically invalidated when the collection document count changes (e.g.
after re-ingestion).

Configuration uses the same environment variables as `cgsim.doc_search`:

| Variable | Default | Description |
|---|---|---|
| `BAMBOO_CHROMA_PATH` | `./chroma_db` | Path to the ChromaDB persistent directory. |
| `BAMBOO_CHROMA_COLLECTION` | `cgsim_docs` | Collection name. |

---

## Output

A text block listing the top matching document chunks with their metadata and
BM25 scores.  Each chunk includes:

- Document text (truncated to `_SNIPPET_MAX_CHARS = 500` characters).
- Source file or URL (from metadata).
- BM25 relevance score (higher = more relevant).

Only chunks with a score greater than zero are returned.  If no chunks score
above zero a friendly "no keyword matches found" message is returned.

---

## Tokenisation

Text is tokenised by lowercasing and splitting on `[a-z0-9_]+` — alphanumeric
tokens plus underscores.  This retains CGSim identifiers such as `assignJob`,
`getResourceInformation`, `onSimulationEnd`, and `BM25Okapi` as single tokens.

---

## Dependencies

- `chromadb` — to load the corpus (shared with `cgsim.doc_search`)
- `rank_bm25` — lightweight BM25 implementation

Both are included in `requirements-rag.txt`.

---

## See also

- [`cgsim.doc_search`](cgsim.doc_search.md) — vector similarity search over the same corpus
- [`docs/rag.md`](../rag.md) — RAG pipeline architecture, ingestion, and configuration
