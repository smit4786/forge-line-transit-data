# Forge Line Transit Data

Logged real-time bus telemetry powering the Detroit Automation Academy
[Live Transit map](https://www.detroitautomationacademy.com/live-transit.html).

A GitHub Action polls the academy's transit proxy every 5 minutes and appends
one standardized record per poll. The log is the raw material for headway
analysis, reliability scores, and arrival predictions.

- **Data:** [`data/`](data/) — `YYYY-MM-DD.jsonl` (one JSON object per line),
  plus [`data/index.json`](data/index.json) listing available days.
- **Format:** [`SCHEMA.md`](SCHEMA.md) — `forge-line.telemetry/v1`.
  Vehicle objects use exactly the same fields as the live API, so code written
  against the live feed reads the log without translation.
- **Live API:** `https://forge-line-live-proxy.netlify.app/api/vehicles`
- **Cadence:** every 5 minutes (UTC). Failed polls are skipped, never logged
  empty — gaps mean unknown, not zero buses.

## Quick start

```python
import json, urllib.request

url = ("https://raw.githubusercontent.com/smit4786/forge-line-transit-data"
       "/main/data/index.json")
days = json.load(urllib.request.urlopen(url))["days"]
print(days[-1])  # most recent day
```

## Sources

Vehicle positions: Detroit Department of Transportation (DDOT) via the
academy's proxy. SMART regional transit joins the feed when its real-time
key is provisioned. This repo stores no API keys.
