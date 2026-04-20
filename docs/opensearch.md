# OpenSearch Integration

Bamboo MCP uses the CERN OpenSearch cluster at `os-atlas.cern.ch` for two
purposes:

| Purpose | Index pattern | Direction |
|---|---|---|
| Harvester pilot/worker timeseries | `atlas_harvesterworkers-*` | **Read** |
| Prompt and response logging | `bamboomcp-promptlog-*` | **Write** |

Both features share the same cluster and the same set of connection environment
variables.

---

## Connection environment variables

| Variable | Default | Description |
|---|---|---|
| `ASKPANDA_OPENSEARCH_HOST` | `https://os-atlas.cern.ch/os` | Cluster base URL |
| `ASKPANDA_OPENSEARCH_USER` | `pilot-monitor-agent` | HTTP Basic-auth username |
| `ASKPANDA_OPENSEARCH_CA` | `/etc/pki/tls/certs/CERN-bundle.pem` | CA certificate bundle path |
| `ASKPANDA_OPENSEARCH_VERIFY_CERTS` | `true` | Set to `false` to skip TLS verification (dev only) |

The **password** for each feature is set separately (see below).

---

## Harvester timeseries (read)

The `atlas.harvester_timeseries` tool queries the `atlas_harvesterworkers-*`
index for per-bucket pilot counts.  Set the read password via:

```bash
export ASKPANDA_OPENSEARCH=<password>
```

This is read-only access.  No index template or write permissions are needed.

See [`docs/harvester-workers.md`](harvester-workers.md) for the full tool
reference.

---

## Prompt logging (write)

### Overview

When enabled, every LLM synthesis call is logged to a daily-rollover index.
Only the **current turn** is stored — chat history is deliberately excluded.
The `session_id` + `turn_id` + `@timestamp` fields are sufficient to
reconstruct a full conversation by joining documents in time order.

All text fields are **pseudonymised before writing** — see the
[Privacy / GDPR](#privacy--gdpr) section below.

### Enabling

```bash
export BAMBOO_OPENSEARCH_PROMPTLOG=<write-password>
```

When this variable is absent or empty the feature is completely passive — no
connections are attempted, no overhead is incurred.

### Index name

Indices follow daily rollover naming:

```
bamboomcp-promptlog-YYYY.MM.DD
```

e.g. `bamboomcp-promptlog-2026.04.17`.

Override the base name via:

```bash
export BAMBOO_OPENSEARCH_PROMPTLOG_INDEX=bamboomcp-promptlog  # default
```

### Document schema

Each LLM call produces one document:

```json
{
    "@timestamp":    "2026-04-17T14:33:01.123456+00:00",
    "session_id":    "3f2a1b4c-...",
    "turn_id":       "9e8d7c6b-...",
    "provider":      "gemini",
    "model":         "gemini-2.0-flash",
    "max_tokens":    2048,
    "system_prompt": "You are AskPanDA...",
    "user_prompt":   "User question:\njobs at BNL-ATLAS...\n\nEvidence:...",
    "response":      "There are 42 running jobs at BNL-ATLAS...",
    "tools_used":    ["panda_jobs_query"],
    "input_tokens":  847,
    "output_tokens": 312
}
```

| Field | Type | Description |
|---|---|---|
| `@timestamp` | ISO-8601 datetime | UTC time of the LLM call |
| `session_id` | UUID | Stable for the lifetime of the server process (one TUI session = one session_id) |
| `turn_id` | UUID | Unique per LLM call — use to identify a single exchange |
| `provider` | string | LLM provider: `gemini`, `openai`, `anthropic`, `mistral` |
| `model` | string | Model name, e.g. `gemini-2.0-flash` |
| `max_tokens` | int | Token budget passed to the LLM |
| `system_prompt` | string | Redacted system prompt |
| `user_prompt` | string | Redacted synthesis prompt (question + injected evidence) |
| `response` | string | Redacted LLM response |
| `tools_used` | string[] | MCP tools that contributed evidence to this call |
| `input_tokens` | int \| null | Input token count from provider usage object; null when unavailable |
| `output_tokens` | int \| null | Output token count; null when unavailable |

### Reconstructing a conversation

To replay a full session in chronological order:

```json
GET bamboomcp-promptlog-*/_search
{
  "query": { "term": { "session_id": "3f2a1b4c-..." } },
  "sort":  [ { "@timestamp": "asc" } ]
}
```

Each document is one turn: `user_prompt` is what was sent, `response` is what
came back.

### Suggested index template

Apply this template before the first document lands to get correct field
mappings and a 30-day retention policy:

```json
PUT _index_template/bamboomcp-promptlog
{
  "index_patterns": ["bamboomcp-promptlog-*"],
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 1,
      "index.lifecycle.name": "bamboomcp-30d"
    },
    "mappings": {
      "properties": {
        "@timestamp":    { "type": "date" },
        "session_id":    { "type": "keyword" },
        "turn_id":       { "type": "keyword" },
        "provider":      { "type": "keyword" },
        "model":         { "type": "keyword" },
        "max_tokens":    { "type": "integer" },
        "system_prompt": { "type": "text" },
        "user_prompt":   { "type": "text" },
        "response":      { "type": "text" },
        "tools_used":    { "type": "keyword" },
        "input_tokens":  { "type": "integer" },
        "output_tokens": { "type": "integer" }
      }
    }
  }
}
```

A simple 30-day ILM policy:

```json
PUT _ilm/policy/bamboomcp-30d
{
  "policy": {
    "phases": {
      "delete": {
        "min_age": "30d",
        "actions": { "delete": {} }
      }
    }
  }
}
```

---

## Privacy / GDPR

Prompts and responses may contain personal identifiers — primarily CERN/ATLAS
usernames appearing in PanDA queries (e.g. `prodUserName: jsmith`) or in
natural-language questions ("show jobs for jsmith").  Storing these without
consent is prohibited under EU GDPR.

**Bamboo pseudonymises all text before writing to OpenSearch.**  Three passes
run in order:

1. **Structured PanDA fields** — values of known name-carrying JSON fields
   (`prodUserName`, `owner`, `createdBy`, `email`, `dn`, etc.) are always
   replaced.
2. **Capitalised word pairs** — two consecutive title-case words not in the
   technical-term whitelist are treated as a first+last name and replaced.
   This pass runs before the contextual pass so "John Smith" is matched as a
   unit.
3. **Contextual triggers** — identifiers following trigger phrases such as
   `"user"`, `"for"`, `"submitted by"`, `"owned by"` are replaced.  The
   pattern `"for user jsmith"` is handled as a single match.

Replacements use the format `user_XXXXXXXX` where `XXXXXXXX` is the
8-character lowercase hex CRC32 of the original identifier.  The **same
identifier always maps to the same token**, so log documents remain joinable
(e.g. you can count how many turns a pseudonymous user had) without the raw
name being stored anywhere.

Technical terms — site names, PanDA statuses, queue types, physics
terminology — are whitelisted and never replaced.

### Security note

CRC32 is a non-cryptographic checksum.  An attacker with the full CERN
username list (~10 k entries) could reverse the mapping by exhaustive lookup.
If the `bamboomcp-promptlog-*` index is ever accessible outside the CERN
network, upgrade to HMAC-SHA256 keyed by a secret stored in
`BAMBOO_PROMPTLOG_HASH_KEY`.  The change is isolated to the `_crc32_token()`
function in `core/bamboo/llm/prompt_log.py`.

---

## Circuit breaker

To prevent a broken OpenSearch connection from flooding the Python log with
per-turn warnings, `prompt_log.py` implements a simple circuit breaker:

- Each write failure increments a consecutive-failure counter and emits a
  `WARNING`.
- When the counter reaches **3** (configurable via `_CIRCUIT_BREAKER_THRESHOLD`
  in `prompt_log.py`), the circuit **opens**: an `ERROR` is logged once and all
  subsequent write attempts are skipped silently for the rest of the session.
- A successful write resets the counter to zero.

The `ERROR` message includes the index name and the last exception, e.g.:

```
ERROR bamboo.llm.prompt_log: circuit breaker tripped after 3 consecutive
write failures — prompt logging disabled for this session.
Check BAMBOO_OPENSEARCH_PROMPTLOG credentials and write access to index
'bamboomcp-promptlog-2026.04.17'. Last error: 403 Forbidden
```

Common causes: wrong password in `BAMBOO_OPENSEARCH_PROMPTLOG`, the
`bamboomcp-promptlog-*` index does not exist yet (create it or apply the
index template above), or the account does not have write permission on that
index.

---

## Implementation

| File | Role |
|---|---|
| `core/bamboo/llm/prompt_log.py` | Redaction, document building, circuit breaker, OpenSearch write |
| `core/bamboo/tools/bamboo_executor.py` | Calls `log_prompt()` from `call_llm()` after every synthesis call |
| `packages/askpanda_atlas/askpanda_atlas/harvester_timeseries_impl.py` | Read-only timeseries queries |
| `scripts/opensearch_monitor.py` | CLI exploration script for the harvester index |
| `tests/test_prompt_log.py` | Unit tests for redaction, circuit breaker, document shape |
