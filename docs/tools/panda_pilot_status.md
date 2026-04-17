# `panda_pilot_status`

**Package:** `bamboo-core`
**Module:** `bamboo.tools.pilot_monitor`
**Type:** Stub — not yet connected to live data

---

## Purpose

`panda_pilot_status` is intended to return pilot job counts and basic status for a named ATLAS computing site. It is currently a **stub implementation** that returns fixed dummy values and is not connected to any live data source.

> **This tool is not used in production routing.** Operational pilot questions are handled by [`panda_harvester_workers`](panda_harvester_workers.md), which queries the live BigPanDA Harvester API.

---

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `site` | string | Yes | Site name, e.g. `BNL-ATLAS`. |
| `window_minutes` | integer | No (default `60`) | Lookback window in minutes. |

---

## Output (stub)

```
Pilot status for <site> (dummy)
- window_minutes: <window>
- pilots_running: 128
- pilots_idle: 12
- pilots_failed: 3
Replace with real Grafana/Harvester/PanDA monitor queries.
```

---

## Status

Stub. The module docstring notes it maps to the previous `PilotMonitorAgent` concept. Real functionality should query the BigPanDA Harvester API or a Grafana data source. The live replacement is `panda_harvester_workers`.

---

## See also

- [`panda_harvester_workers`](panda_harvester_workers.md) — live pilot counts (use this instead)
