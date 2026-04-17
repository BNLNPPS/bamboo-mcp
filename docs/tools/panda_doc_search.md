# `panda_doc_search`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.doc_rag`
**Type:** Documentation retrieval — vector similarity search

---

## Purpose

`panda_doc_search` searches the PanDA and Bamboo documentation for conceptual questions, how-to guidance, configuration options, and explanations of system behaviour. It uses vector similarity search against a pre-built ChromaDB collection.

Use it for questions about *how something works*, not questions about the *live status* of a specific task or job.

Typical questions:
- "How does the PanDA pilot work?"
- "What is Harvester?"
- "How do I configure a new queue in CRIC?"
- "What does pilot error code 1305 mean?"
- "How does Bamboo route questions?"

Complements [`panda_doc_bm25`](panda_doc_bm25.md) — use both together for best coverage (vector search handles semantic similarity; BM25 handles exact-match and enumeration queries).

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | Yes | Natural-language search query. |
| `n_results` | integer | No (default `5`) | Number of document chunks to return. |

---

## Data source

A ChromaDB persistent collection. Configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `BAMBOO_CHROMA_PATH` | `./chroma_db` | Path to the ChromaDB persistent directory. |
| `BAMBOO_CHROMA_COLLECTION` | `bamboo_docs` | Collection name to query. |

The ChromaDB client is initialised lazily on the first call and cached. If `chromadb` is not installed, or the configured path does not exist, the tool returns a human-readable error rather than raising.

> **Note:** As of the current deployment the ChromaDB collection contains only base PanDA documentation. Bamboo-specific documentation has not yet been ingested.

---

## Output

A text block listing the top matching document chunks with their metadata and distance scores. Each chunk includes:

- Document text (truncated to `_SNIPPET_MAX_CHARS = 500` characters).
- Source file or URL (from metadata).
- Distance score (lower = more similar).

---

## Routing

`bamboo_answer` routes to this tool (paired with `panda_doc_bm25`) for questions that do not match any operational fast-path and pass the topic guard. Questions starting with documentation-intent prefixes (`"how does"`, `"what is"`, `"explain"`, `"describe"`, etc.) are directed here even when they contain pilot or job signal words.

---

## See also

- [`panda_doc_bm25`](panda_doc_bm25.md) — keyword BM25 search over the same corpus
