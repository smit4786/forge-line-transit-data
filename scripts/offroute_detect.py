#!/usr/bin/env python3
"""Off-route detector for Forge Line transit telemetry (idea 11).

Compares each logged vehicle's position against its route's GTFS shape
polyline and flags vehicles farther than a threshold from the shape.

The shapes in ddot-routes-3d.json are stored in projected scene meters
(equirectangular around lat0/lon0, y negated); they are unprojected back to
WGS84 lat/lon here with the exact inverse of the page's project() function:

    x = (lon - lon0) * mLon        mLon = 111320 * cos(lat0)
    y = -(lat - lat0) * mLat       mLat = 111320

Distance is the minimum haversine distance to any shape point (coarse point
sampling; shapes are pre-simplified, so no extra subsampling is applied).

Usage:
    python3 scripts/offroute_detect.py data/2026-10-03.jsonl [--json]
    python3 scripts/offroute_detect.py data/2026-10-0*.jsonl --threshold 400 \
        --shapes /path/to/ddot-routes-3d.json

Stdlib only.
"""
import argparse
import json
import math
import sys
from pathlib import Path

M_LAT = 111320.0
DEFAULT_THRESHOLD_M = 400.0
DEFAULT_SHAPES = (Path(__file__).resolve().parents[2]
                  / "detroit-automation-academy-rebrand" / "site"
                  / "ddot-routes-3d.json")


def haversine_m(lat1, lon1, lat2, lon2):
    r = math.radians
    dlat, dlon = r(lat2 - lat1), r(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(dlon / 2) ** 2)
    return 6371000.0 * 2 * math.asin(math.sqrt(a))


def load_shapes(path):
    """Return {route_id: [(lat, lon), ...]} unprojected from scene meters."""
    with open(path) as f:
        data = json.load(f)
    lat0 = data["projection"]["lat0"]
    lon0 = data["projection"]["lon0"]
    m_lon = M_LAT * math.cos(math.radians(lat0))
    shapes = {}
    for route in data["routes"]:
        pts = []
        for path in route["paths"]:
            for x, y in path:
                lon = lon0 + x / m_lon
                lat = lat0 - y / M_LAT
                pts.append((lat, lon))
        shapes[str(route["id"])] = pts
    return shapes


def min_shape_distance(lat, lon, pts):
    best = None
    for plat, plon in pts:
        # cheap reject: >0.05 deg (~5.5 km) cannot beat a 400 m threshold
        if abs(plat - lat) > 0.05 or abs(plon - lon) > 0.05:
            continue
        d = haversine_m(lat, lon, plat, plon)
        if best is None or d < best:
            best = d
            if best == 0.0:
                break
    return best


def load_vehicles(paths):
    out = []
    for path in paths:
        with open(path) as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    print(f"warn: {path}:{i}: skipping bad JSON", file=sys.stderr)
                    continue
                polled = r.get("polled_at", "?")
                for v in r.get("vehicles") or []:
                    out.append({"polled_at": polled, "v": v,
                                "src": f"{path}:{i}"})
    return out


def analyze(vehicles, shapes, threshold_m):
    flagged = []
    checked = 0
    skipped_no_shape = 0
    skipped_bad_coord = 0
    for item in vehicles:
        v = item["v"]
        rid = str(v.get("route_id", "?"))
        pts = shapes.get(rid)
        if not pts:
            skipped_no_shape += 1
            continue
        try:
            lat = float(v["lat"])
            lon = float(v["lon"])
        except (KeyError, TypeError, ValueError):
            skipped_bad_coord += 1
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            skipped_bad_coord += 1
            continue
        checked += 1
        d = min_shape_distance(lat, lon, pts)
        if d is None:
            # no shape point within the cheap-reject window: far off-route
            d = float("inf")
        if d > threshold_m:
            flagged.append({
                "vehicle_id": str(v.get("vehicle_id", "?")),
                "route_id": rid,
                "distance_m": round(d, 1) if d != float("inf") else None,
                "distance_note": ">5.5 km from any shape point" if d == float("inf") else None,
                "lat": lat,
                "lon": lon,
                "polled_at": item["polled_at"],
                "vehicle_updated_at": v.get("updated_at"),
            })
    flagged.sort(key=lambda f: (f["distance_m"] is None, -(f["distance_m"] or 0)))
    return {
        "vehicles_checked": checked,
        "skipped_no_shape": skipped_no_shape,
        "skipped_bad_coord": skipped_bad_coord,
        "threshold_m": threshold_m,
        "off_route_count": len(flagged),
        "flagged": flagged,
    }


def text_report(res):
    L = []
    L.append("Forge Line telemetry — off-route detection")
    L.append(f"Vehicles checked: {res['vehicles_checked']} "
             f"(skipped: {res['skipped_no_shape']} no shape, "
             f"{res['skipped_bad_coord']} bad coords)")
    L.append(f"Threshold: {res['threshold_m']:.0f} m from route GTFS shape")
    L.append(f"Flagged off-route: {res['off_route_count']}")
    for f in res["flagged"]:
        dist = f"{f['distance_m']:.1f} m" if f["distance_m"] is not None else f["distance_note"]
        L.append(f"  route {f['route_id']} vehicle {f['vehicle_id']}: {dist} "
                 f"at {f['polled_at']} (feed ts {f['vehicle_updated_at']}) "
                 f"[{f['lat']}, {f['lon']}]")
    if not res["flagged"]:
        L.append("  (none — all vehicles within threshold of their route shape)")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Flag vehicles far from their GTFS route shape.")
    ap.add_argument("files", nargs="+", help="JSONL day file(s)")
    ap.add_argument("--shapes", default=str(DEFAULT_SHAPES),
                    help="ddot-routes-3d.json path")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_M,
                    help="flag distance in meters (default 400)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    try:
        shapes = load_shapes(args.shapes)
    except (OSError, KeyError, ValueError) as e:
        print(f"cannot load shapes from {args.shapes}: {e}", file=sys.stderr)
        sys.exit(1)

    vehicles = load_vehicles(args.files)
    if not vehicles:
        print("no vehicle records found", file=sys.stderr)
        sys.exit(1)
    res = analyze(vehicles, shapes, args.threshold)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(text_report(res))


if __name__ == "__main__":
    main()
