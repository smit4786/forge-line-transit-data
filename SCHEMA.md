# Telemetry record format — `forge-line.telemetry/v1`

Standardized, API-compatible format for the logged Detroit bus telemetry.
"API-compatible" means: a vehicle object in the log uses **exactly the same
field names** as a vehicle object from the live proxy endpoint
`GET https://forge-line-live-proxy.netlify.app/api/vehicles`, so any consumer
written against the live API can read logged vehicles without translation.

## Files

| Path | Contents |
|---|---|
| `data/YYYY-MM-DD.jsonl` | One JSON object per line; one line per successful poll (UTC date). |
| `data/index.json` | Discovery document: available days with poll and record counts. |

All timestamps are ISO 8601 UTC (`...Z`). Cadence is every 5 minutes
(288 polls/day max). Coordinates are rounded to 6 decimals (~0.11 m).

## Poll record (one line of the JSONL)

```json
{
  "schema": "forge-line.telemetry/v1",
  "record_id": "2026-10-03T01:10:00Z",
  "polled_at": "2026-10-03T01:10:02Z",
  "proxy": "forge-line-live-proxy.netlify.app",
  "vehicle_count": 17,
  "vehicles": [ { "agency": "ddot", "route_id": "4", "vehicle_id": "2532",
                  "lat": 42.331234, "lon": -83.046789,
                  "bearing": 151, "speed_mph": 13,
                  "updated_at": "2026-10-03T01:09:42" } ],
  "sources": {
    "ddot":  { "ok": true,  "vehicles": 17 },
    "smart": { "ok": false, "reason": "no SWIFTLY_KEY" }
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | Format version. Always `forge-line.telemetry/v1` in this file layout. |
| `record_id` | string | Unique id of this poll; equals `polled_at`. |
| `polled_at` | string | When the logger polled the proxy (UTC). |
| `proxy` | string | Proxy host that served the data. |
| `vehicle_count` | integer | Number of vehicle objects in this record. |
| `vehicles` | array | Vehicle snapshots, live-API shape (below). |
| `sources` | object | Per-agency feed status, trimmed from the proxy response. |

## Vehicle object (identical to the live API)

| Field | Type | Meaning |
|---|---|---|
| `agency` | string | `ddot` today; `smart` when that feed comes online. |
| `route_id` | string | GTFS route id (`1`, `4`, `10`, `16`). |
| `vehicle_id` | string | Agency vehicle number. |
| `lat` / `lon` | number | WGS84 position, 6 decimals. |
| `bearing` | number | Compass heading, degrees. Omitted if unknown. |
| `speed_mph` | number | Reported speed. Omitted if unknown. |
| `updated_at` | string | Feed timestamp for this vehicle (agency clock). Omitted if unknown. |

Vehicles with missing or out-of-range coordinates are dropped before logging
and never appear in `vehicles` (the `vehicle_count` reflects kept records).

## `data/index.json`

```json
{
  "schema": "forge-line.telemetry/v1",
  "index_updated_at": "2026-10-03T01:10:03Z",
  "days": [
    { "date": "2026-10-03", "polls": 12, "vehicle_records": 204,
      "path": "data/2026-10-03.jsonl" }
  ]
}
```

## Reading the data

Raw files are directly fetchable (this is a public repo):

```
https://raw.githubusercontent.com/smit4786/forge-line-transit-data/main/data/index.json
https://raw.githubusercontent.com/smit4786/forge-line-transit-data/main/data/2026-10-03.jsonl
```

Python:

```python
import json, urllib.request
def records(day):
    url = f"https://raw.githubusercontent.com/smit4786/forge-line-transit-data/main/data/{day}.jsonl"
    for line in urllib.request.urlopen(url):
        line = line.strip()
        if line:
            yield json.loads(line)
```

## Gaps

Failed polls are **never** logged as empty records. A missing 5-minute slot in
a day's file means that poll failed (proxy down, network error, or invalid
payload). Treat gaps as unknown, not as zero buses.

## Versioning

- Backward-compatible additions (new optional fields) keep the `v1` tag.
- Any breaking change (rename, removal, type change) bumps to `v2` and is
  documented here with a migration note. `v1` files are never rewritten.
