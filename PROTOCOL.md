# forge-line.endpoint/v1 — Managed Transit Endpoint Protocol

**Status:** draft · **Date:** 2026-10-04
**Scope:** DDOT bus fleet as observed through the Forge Line proxy.

`forge-line.telemetry/v1` remains the raw wire format: one record per poll,
positions as reported. This protocol is the *derived* layer — what the data
means once you treat each bus as a managed endpoint traveling a known route
network. Telemetry/v1 answers "where did the proxy see?"; endpoint/v1 answers
"what is this bus doing?"

## 1. Design principles

**Events, not snapshots.** The log is an append-only stream of typed events.
Endpoint state (the registry) is a materialized view — rebuildable from the
event log at any time. State can be wrong; the log is the truth.

**Staleness is explicit.** Every observation carries three timestamps:

| field | meaning |
|---|---|
| `observed_at` | when the vehicle/GPS produced the fix (`updated_at` from the feed) |
| `received_at` | when the proxy ingested it (`polled_at`) |
| `recorded_at` | when the logger wrote the event |

Age of any fact = `now - observed_at`, always computable, never guessed.

**Positions are route-contextual.** A raw lat/lon is nearly useless for
transit. Every position is map-matched at ingest to:

```json
"at": {"lat": 42.331, "lon": -83.045,
       "route_id": "16",
       "nearest_stop": "1348",
       "stop_distance_m": 42.1,
       "segment": ["1348", "2801"]}
```

`segment` is the stop-pair the bus is traveling between — the unit of timing.
Segments are *observed*, not scheduled: if a bus skips stops, the segment is
the longer pair it actually traversed. The schedule never constrains the
observation; it only annotates it.

**Timing is empirical.** No scheduled times appear anywhere in this protocol.
A traversal carries measured seconds; the model aggregates them. Point-to-point
time = Σ segment means + Σ dwell means, with p50/p90 as the honest band.

## 2. Event types

All events share the envelope:

```json
{"schema": "forge-line.endpoint/v1",
 "event": "<type>",
 "event_id": "2026-10-04T17:05:00Z/101/traversed",
 "recorded_at": "2026-10-04T17:05:03Z",
 "vehicle_id": "101", "route_id": "16",
 "source_poll": "2026-10-04T17:05:00Z"}
```

`source_poll` is provenance: the telemetry/v1 `record_id` this event was
derived from. Every derived fact cites its source.

| event | when | key fields |
|---|---|---|
| `endpoint.enrolled` | first sighting of a vehicle_id | `first_seen` (= recorded_at) |
| `segment.traversed` | confirmed arrival at a new stop | `from_stop`, `to_stop`, `seconds`, `distance_m`, `bucket` |
| `dwell.observed` | bus leaves a stop after stopping | `stop`, `dwell_seconds` |
| `assignment.changed` | route_id differs from registry | `old_route`, `new_route` |
| `anomaly.flagged` | see §4 | `subtype`, `evidence` |
| `endpoint.quiet` | absent from N consecutive polls | `last_seen`, `missed_polls` |

Deliberately absent: a per-poll `checkin` event. Raw polls already exist in
telemetry/v1; duplicating them would double the log for zero information.
This protocol logs *transitions* — the things that changed.

## 3. Segment detection

Per vehicle, the detector holds an **anchor**: the last confidently-visited
stop and its time. Each poll:

1. Find the nearest stop on the vehicle's route (`reference/route-stops.json`).
2. If it matches the anchor → accumulate dwell (speed < 5 mph or movement < 30 m).
3. If a *different* stop is within 120 m → candidate switch. Confirm when it
   persists for 2 consecutive polls, or when the anchor is > 250 m behind.
4. On confirm → emit `segment.traversed` + `dwell.observed` (if any), move anchor.

Debounce kills GPS flicker: a bus sitting between two stops can't strobe the
log. Skipped stops are fine — the segment is just longer, and the timing model
learns the real pair.

## 4. Anomaly taxonomy

| subtype | rule | handling |
|---|---|---|
| `impossible_jump` | implied speed > 70 mph between polls | flag; do **not** move the anchor (bad GPS must not corrupt the model) |
| `frozen` | identical rounded position ≥ 6 polls | flag once per freeze episode |
| `off_route` | nearest stop > 800 m | flag; skip traversal logic that poll |
| `ghost_route` | route_id not in the GTFS catalog | flag; match against all stops, not just the route's |

Anomalies are events, not deletions. The raw data stays; the flag says
"don't trust this for timing."

## 5. Timing model

`data/timing/segments.json` — rebuilt from the event log each run:

```json
{"16|1348|2801|wkd_pm": {"n": 42, "mean_s": 95.3, "p50_s": 88.0, "p90_s": 141.0, "dist_m": 612.4}}
```

Buckets (America/Detroit): `wkd_am` 06–09, `wkd_mid` 09–15, `wkd_pm` 15–19,
`wkd_eve` 19–24, `wkd_night` 00–06, `sat`, `sun`. Minimum 3 observations
before a cell is published — fewer than that and the honest answer is
"not enough data."

## 6. What's intentionally out

- **No schedule data.** The protocol never reads GTFS times. Schedules are a
  claim; this protocol records observations.
- **No predictions.** The model aggregates; the planner predicts. Separation
  of concerns.
- **No identity beyond vehicle_id.** No driver, no run number — the feed
  doesn't carry them, so the protocol doesn't invent them.

## 7. File layout (telemetry repo)

```
data/vehicles/registry.json      # materialized endpoint state (telemetry/v1 logger)
data/vehicles/events.jsonl       # endpoint/v1 event stream (this protocol)
data/timing/segments.json        # aggregated timing model
data/timing/watermark.json       # incremental processing watermark
reference/route-stops.json       # per-route stop coordinates (static reference)
PROTOCOL.md                      # this document
```
