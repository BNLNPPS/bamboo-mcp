# Bamboo RAG System

This document describes how Bamboo retrieves documentation context and uses it
to answer general knowledge questions about the systems it is configured to
support.

---

## Overview

When a user asks a general knowledge question — anything that does not reference
a specific task ID or job ID — Bamboo runs a two-stage retrieval pipeline before
calling an LLM:

1. **Topic guard** — determines whether the question is on-topic for this
   Bamboo deployment. Off-topic questions are rejected immediately without any
   LLM or retrieval cost.
2. **Dual retrieval** — runs vector similarity search and BM25 keyword search
   concurrently against a local ChromaDB collection. The results are merged and
   passed to the LLM as grounding context.

The LLM is instructed to answer from the retrieved documentation rather than
from its own parametric knowledge, which reduces hallucination of
system-specific details.

---

## Architecture

```
User question
      │
      ▼
┌─────────────────┐
│   Topic guard   │──── DENY ────► "I can only answer questions about …"
└────────┬────────┘
         │ ALLOW
         ▼
┌────────────────────────────────────────┐
│  ID extraction (regex)                 │
│  task_id? ──► task status tool         │
│  job_id?  ──► job status / log tool    │
│  neither  ──► RAG pipeline (below)     │
└────────────────────────────────────────┘
         │ no ID found
         ▼
┌──────────────────────┐  ┌──────────────────────┐
│  Vector search       │  │  BM25 keyword search  │
│  panda_doc_search    │  │  panda_doc_bm25        │
│  top_k = 20          │  │  top_k = 10            │
│  (semantic)          │  │  (exact-match)         │
└──────────┬───────────┘  └──────────┬────────────┘
           │    asyncio.gather        │
           └──────────┬──────────────┘
                      │
                      ▼
             Merge results
             (vector first, BM25 second)
                      │
                      ▼
           ┌──────────────────┐
           │   LLM synthesis  │
           │  max_tokens=2048 │
           └──────────────────┘
                      │
                      ▼
              Answer to user
```

---

## Components

### 1. Topic guard (`core/bamboo/tools/topic_guard.py`)

Prevents the system from answering unrelated questions and wasting LLM calls.
The allow and deny keyword lists are domain-specific — a deployment targeting
a different system should update these lists accordingly.

**Stage 1 — keyword fast-path (free)**

The question is matched against two pre-compiled word lists:

- **Allow list** — domain-specific terms that immediately allow the question
  without any LLM call.
- **Deny list** — clearly off-topic terms (cooking, sports, entertainment,
  personal advice, etc.) that immediately reject the question.

If neither list matches, the question is considered ambiguous and escalates to
Stage 2.

**Stage 2 — LLM classifier (fast profile, ~5 tokens)**

A single-word classification call (`ALLOW` or `DENY`) is made to the fast LLM
profile. The model is instructed to be permissive — anything plausibly related
to the domain, including supporting technologies, is allowed. Only clearly
unrelated questions are denied.

If the LLM call fails for any reason the guard **fails open** — the question
is allowed through. A guardrail failure must never silently block a legitimate
user.

---

### 2. Vector search (`core/bamboo/tools/doc_rag.py`)

Semantic similarity search using ChromaDB with the `all-MiniLM-L6-v2` sentence
embedding model (384 dimensions).

- Queries the ChromaDB collection with `top_k=20`
- Returns document chunks ranked by cosine distance
- Good for conceptual questions: *"What is X?"*, *"How does Y work?"*
- The ChromaDB client is initialised lazily on first call and cached on the
  tool instance

---

### 3. BM25 keyword search (`core/bamboo/tools/doc_bm25.py`)

Classical term-frequency ranking using the `rank_bm25` library.

- Loads the full document corpus from ChromaDB on first call (in batches of 500)
- Builds a `BM25Okapi` index in memory and caches it
- The cache is refreshed automatically when the collection document count changes
- Queries with `top_k=10`, filtered to chunks with score > 0
- Good for exact-match and enumeration queries: *"List all error codes"*,
  *"What is the value of constant X?"*

Both search tools run concurrently via `asyncio.gather`. If either fails, the
other's results are still used — neither failure is fatal.

---

### 4. LLM synthesis (`core/bamboo/tools/bamboo_answer.py`)

The merged retrieval results are passed to the default LLM profile as a
structured prompt.

**With context** — the system prompt instructs the LLM to:
- Base its answer on the retrieved excerpts
- Not add claims not present in the excerpts
- Supplement from general knowledge only when excerpts are partially relevant,
  and label it clearly
- Never fabricate system-specific details

**Without context** (retrieval failed or returned nothing) — the system prompt
instructs the LLM to:
- Tell the user the knowledge base did not contain enough information
- Not answer from general knowledge
- Point the user to official documentation

`max_tokens` is set to 2048 on all synthesis calls to avoid truncated answers.

---

## Knowledge base

### Ingestion

Documents are ingested by an external ingestion pipeline (not part of the
Bamboo core) which watches a directory for new or changed files and writes
embeddings into ChromaDB. The `DocumentMonitorAgent` in the
`askpanda-atlas-agents` repository is the reference implementation.

| Parameter | Default | Notes |
|---|---|---|
| Embedding model | `all-MiniLM-L6-v2` | 384-dimensional vectors |
| Chunk size | 3000 chars | Large enough to keep class or function definitions intact |
| Chunk overlap | 300 chars | Prevents context loss at chunk boundaries |
| ChromaDB backend | Persistent local | Path set via `BAMBOO_CHROMA_PATH` |

**Pre-processing:** before chunking, documentation files should have
auto-generated index sections stripped. The reference implementation includes
a `strip_sphinx_index()` utility that removes Sphinx index entries (lines of
the form `ClassName (module.path attribute), 132`) which are useless for RAG
and confuse the LLM because the page numbers look like meaningful values.

### Re-ingestion

Re-ingestion is required after changing chunk size or when the ChromaDB index
becomes corrupted. Always use absolute paths to avoid the database being written
to the wrong location depending on working directory:

```bash
rm -rf /abs/path/to/.chromadb /abs/path/to/.document_monitor/checkpoints.json
askpanda-document-monitor-agent \
    --dir /abs/path/to/docs \
    --chroma-dir /abs/path/to/.chromadb
```

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `BAMBOO_CHROMA_PATH` | `./chroma_db` | Path to the ChromaDB persistent directory |
| `BAMBOO_CHROMA_COLLECTION` | plugin-dependent | Name of the ChromaDB collection to query |

Each plugin defaults `BAMBOO_CHROMA_COLLECTION` to its own collection name if
the variable is unset, so different plugins can coexist in the same ChromaDB
directory without interfering:

| Plugin | Default collection name |
|---|---|
| `askpanda_atlas` | `atlas_docs` |
| `askpanda_epic` | `epic_docs` |
| `cgsim` | `cgsim_docs` |

Set `BAMBOO_CHROMA_COLLECTION` explicitly to override.  The value must match
exactly (case-sensitive) what was used during ingestion.

Set `ASKPANDA_PLUGIN` to select the active plugin for the TUI and Streamlit
interfaces (`atlas`, `epic`, or `cgsim`; default: `atlas`).

Set these in `bamboo_env.sh` before starting the server.

---

## Routing summary

| Question type | Example | Route |
|---|---|---|
| Off-topic | "Give me a pasta recipe" | Rejected by topic guard |
| Ambiguous | "What is Python?" | LLM classifier → allowed if plausibly relevant |
| General knowledge | "What is PanDA?" | Vector + BM25 → LLM synthesis |
| Enumeration | "List all error codes" | Vector + BM25 → LLM synthesis |
| Task status | "Status of task 12345678?" | Task status tool → LLM synthesis |
| Job status | "What happened to job 99887766?" | Job status tool → LLM synthesis |
| Log analysis | "Why did job 99887766 fail?" | Log analysis tool → LLM synthesis |

---

## Providing a custom RAG implementation

Bamboo's plugin architecture allows a deployment to replace or extend the
built-in retrieval tools with a custom implementation. The built-in
`panda_doc_search` and `panda_doc_bm25` tools are registered in the standard
`TOOLS` dict in `core/bamboo/core.py` and are discoverable by clients through
the MCP tool catalog.

To provide your own RAG tool:

**1. Implement the `MCPTool` protocol** (`core/bamboo/tools/base.py`) — your
class needs `get_definition()` and an async `call()` method that accepts
`{"query": str, "top_k": int}` and returns a `text_content(...)` result.

**2. Register via entry point** in your plugin's `pyproject.toml`:

```toml
[project.entry-points."bamboo.tools"]
"myplugin.doc_search" = "myplugin.rag:my_rag_tool"
```

The tool will appear in the MCP tool catalog and be callable by any client or
planner that discovers tools via the entry point mechanism.

**3. Wire into orchestration** — the `bamboo_answer` orchestration tool
currently calls `panda_doc_search` and `panda_doc_bm25` directly by importing
the singletons. If you want your tool to be called automatically for general
questions you have two options:

- **Replace the built-in tools** by overriding `panda_doc_search` and/or
  `panda_doc_bm25` in a fork or subclass.
- **Provide a custom answer tool** as a plugin that replaces `bamboo_answer`
  entirely, following the same `MCPTool` protocol and registering under
  `bamboo.tools`. This keeps the core untouched and is the recommended approach
  for deployments with significantly different retrieval requirements.

See `docs/plugins.md` for the full plugin architecture reference.

---

## Known limitations

- **Coverage depends on ingested documents.** If a topic is not in the
  ChromaDB collection the system will tell the user it could not find relevant
  information rather than fabricating an answer.
- **Chunk boundaries.** Even with `chunk_size=3000`, very large definitions may
  span multiple chunks. The BM25 search partially mitigates this for keyword
  queries.
- **Embedding model consistency.** The same embedding model must be used for
  both ingestion and query time. If a different model is used at query time,
  similarity scores will be meaningless and queries will return no results.
