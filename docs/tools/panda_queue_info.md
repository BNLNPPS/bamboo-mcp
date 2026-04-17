# `panda_queue_info`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.queue_info`
**Type:** Stub — local file only, not connected to live CRIC

---

## Purpose

`panda_queue_info` looks up queue and site configuration for a named ATLAS computing site by reading a local `queuedata.json` file. It is a simple local/stub implementation intended for development and testing.

> **This tool is not used in production routing.** Queue and site configuration questions in production are handled by [`cric_query`](cric_query.md), which queries the live CRIC DuckDB snapshot with full SQL support.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `site` | string | Yes | Site name, e.g. `BNL-ATLAS`, `CERN-PROD`. |

---

## Data source

Reads `queuedata.json` from the path configured by `Config.QUEUE_DATA_PATH`. If the path is relative, it is resolved relative to the `bamboo` package directory. Returns an error if the file is missing or the site is not found.

---

## Output

Pretty-printed JSON of the site's queue configuration, or an error message with the list of known sites.

---

## Status

Stub. Reads a static local file. The production replacement for queue configuration questions is `cric_query`.

---

## See also

- [`cric_query`](cric_query.md) — live CRIC database queries with natural-language SQL (use this instead)
