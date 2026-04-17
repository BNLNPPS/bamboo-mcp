# `panda_doc_bm25`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.doc_bm25`
**Type:** Documentation retrieval — BM25 keyword search

---

## Purpose

`panda_doc_bm25` performs BM25 keyword search over the same documentation corpus used by [`panda_doc_search`](panda_doc_search.md). It excels at exact-match and enumeration queries where vector similarity search may miss precise terms.

Use it (alongside `panda_doc_search`) for:
- "List all pilot error codes."
- "What is BADALLOC?"
- "Which configuration keys accept boolean values?"
- Any query where the exact string matters more than semantic similarity.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | Yes | Keyword search query. |
| `n_results` | integer | No (default `5`) | Number of document chunks to return. |

---

## Data source

The same ChromaDB collection as `panda_doc_search`. All documents are loaded from the collection on the first call, tokenised, and indexed with `BM25Okapi` from the `rank_bm25` package. The index and corpus are cached in-process. The cache is automatically invalidated when the collection document count changes (e.g. after re-ingestion).

Configuration uses the same environment variables as `panda_doc_search`:

| Variable | Default | Description |
|---|---|---|
| `BAMBOO_CHROMA_PATH` | `./chroma_db` | Path to the ChromaDB persistent directory. |
| `BAMBOO_CHROMA_COLLECTION` | `bamboo_docs` | Collection name. |

**Dependencies:** `chromadb` (shared with `panda_doc_search`) and `rank_bm25`.

---

## Output

A text block listing the top matching document chunks with their metadata and BM25 scores. Each chunk includes:

- Document text (truncated to `_SNIPPET_MAX_CHARS = 500` characters).
- Source file or URL (from metadata).
- BM25 relevance score (higher = more relevant).

---

## Tokenisation

Text is tokenised by lowercasing and splitting on `[a-z0-9_]+` — alphanumeric tokens plus underscores. This retains identifiers like `piloterrorcode` and `BADALLOC` as single tokens.

---

## Routing

`bamboo_answer` always calls `panda_doc_bm25` together with `panda_doc_search` for documentation retrieval (`RETRIEVE` route). The two results are synthesised by the LLM into a single answer.

---

## See also

- [`panda_doc_search`](panda_doc_search.md) — vector similarity search over the same corpus
