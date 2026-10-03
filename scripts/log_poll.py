#!/usr/bin/env python3
"""Poll the Forge Line live proxy and append one standardized telemetry record.

Output: data/YYYY-MM-DD.jsonl (UTC date), one JSON object per line, one line
per successful poll. Failed polls are never logged as empty records -- a gap
in the 5-minute cadence means that poll failed or was skipped.

Record format: forge-line.telemetry/v1 (see SCHEMA.md).
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROXY = "https://forge-line-live-proxy.netlify.app"
SCHEMA = "forge-line.telemetry/v1"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def fetch():
    req = urllib.request.Request(
        PROXY + "/api/vehicles",
        headers={"User-Agent": "forge-line-transit-data-logger/1.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        if resp.status != 200:
            raise RuntimeError(f"proxy HTTP {resp.status}")
        return json.load(resp)


def clean_vehicle(v):
    """Keep the live-API vehicle shape exactly; drop records with bad coords."""
    try:
        lat = float(v["lat"])
        lon = float(v["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    out = {
        "agency": str(v.get("agency", "ddot")),
        "route_id": str(v.get("route_id", "")),
        "vehicle_id": str(v.get("vehicle_id", "")),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
    }
    if v.get("bearing") is not None:
        out["bearing"] = v["bearing"]
    if v.get("speed_mph") is not None:
        out["speed_mph"] = v["speed_mph"]
    if v.get("updated_at"):
        out["updated_at"] = v["updated_at"]
    return out


def main():
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = fetch()
    vehicles = payload.get("vehicles")
    if not isinstance(vehicles, list):
        raise RuntimeError("proxy response missing 'vehicles' list")
    clean = [c for c in (clean_vehicle(v) for v in vehicles) if c]

    sources = {}
    for name, s in (payload.get("sources") or {}).items():
        entry = {"ok": bool(s.get("ok"))}
        if "vehicles" in s:
            entry["vehicles"] = s["vehicles"]
        if s.get("reason"):
            entry["reason"] = s["reason"]
        sources[name] = entry

    record = {
        "schema": SCHEMA,
        "record_id": stamp,
        "polled_at": stamp,
        "proxy": "forge-line-live-proxy.netlify.app",
        "vehicle_count": len(clean),
        "vehicles": clean,
        "sources": sources,
    }

    DATA.mkdir(exist_ok=True)
    dayfile = DATA / (now.strftime("%Y-%m-%d") + ".jsonl")
    with open(dayfile, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    update_index()
    print(f"appended poll {stamp}: {len(clean)} vehicles -> {dayfile.name}")


def update_index():
    days = []
    for p in sorted(DATA.glob("*.jsonl")):
        polls = 0
        veh = 0
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                polls += 1
                veh += int(r.get("vehicle_count", 0))
        days.append({
            "date": p.stem,
            "polls": polls,
            "vehicle_records": veh,
            "path": f"data/{p.name}",
        })
    index = {
        "schema": SCHEMA,
        "index_updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days,
    }
    with open(DATA / "index.json", "w") as f:
        json.dump(index, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
