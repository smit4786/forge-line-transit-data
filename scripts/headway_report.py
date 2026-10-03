#!/usr/bin/env python3
"""Daily reliability / bunching report for Forge Line transit telemetry.

Reads one or more forge-line.telemetry/v1 JSONL day files and, per route,
computes poll-visibility stats, a bunching count, and a simple 0-100
reliability score.

HONESTY NOTE: the "headway proxy" below is the median time gap between
consecutive *polls* in which a route had at least one visible vehicle. That
is a poll-visibility metric, not true headway (which needs per-vehicle
trip/stop matching). It is labeled as such everywhere in the output.

Usage:
    python3 scripts/headway_report.py data/2026-10-03.jsonl [--json]
    python3 scripts/headway_report.py data/2026-10-0*.jsonl --json

Stdlib only.
"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone
from statistics import median

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Detroit")
except Exception:  # pragma: no cover - extremely old Python
    LOCAL_TZ = timezone.utc

SERVICE_START_H = 6   # 06:00 local
SERVICE_END_H = 22    # 22:00 local (exclusive)
BUNCH_M = 300.0       # two vehicles within this distance = bunched
BUNCH_PENALTY = 5     # score points deducted per bunching poll
EMPTY_PENALTY = 10    # points deducted per zero-vehicle poll in service window
EXPECTED_CADENCE_MIN = 5.0
GAP_FACTOR = 1.5      # gap > cadence * factor counts as a missing-poll gap


def haversine_m(lat1, lon1, lat2, lon2):
    r = math.radians
    dlat, dlon = r(lat2 - lat1), r(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(dlon / 2) ** 2
    return 6371000.0 * 2 * math.asin(math.sqrt(a))


def parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_records(paths):
    records = []
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
                ts = parse_ts(r.get("polled_at"))
                if ts is None:
                    print(f"warn: {path}:{i}: skipping record with bad polled_at", file=sys.stderr)
                    continue
                vehicles = r.get("vehicles") or []
                records.append({"ts": ts, "vehicles": vehicles, "src": f"{path}:{i}"})
    records.sort(key=lambda r: r["ts"])
    return records


def in_service_window(ts):
    local = ts.astimezone(LOCAL_TZ)
    return SERVICE_START_H <= local.hour < SERVICE_END_H


def analyze(records):
    routes = {}
    for rec in records:
        for v in rec["vehicles"]:
            rid = str(v.get("route_id", "?"))
            routes.setdefault(rid, []).append((rec["ts"], v))

    poll_times = [r["ts"] for r in records]
    gaps = 0
    for a, b in zip(poll_times, poll_times[1:]):
        if (b - a).total_seconds() / 60.0 > EXPECTED_CADENCE_MIN * GAP_FACTOR:
            gaps += 1

    per_route = {}
    for rid, seen in sorted(routes.items()):
        # polls where this route had >= 1 vehicle
        by_poll = {}
        for ts, v in seen:
            by_poll.setdefault(ts, []).append(v)
        poll_list = sorted(by_poll)
        n_polls = len(poll_list)
        n_veh_total = len(seen)
        avg_per_poll = (n_veh_total / n_polls) if n_polls else 0.0

        # headway proxy: gaps between consecutive polls that both saw the route
        vis_gaps_min = [
            (b - a).total_seconds() / 60.0 for a, b in zip(poll_list, poll_list[1:])
        ]
        headway_proxy_min = median(vis_gaps_min) if vis_gaps_min else None

        # bunching: polls where >= 2 vehicles of this route are within BUNCH_M
        bunch_polls = 0
        bunch_details = []
        for ts in poll_list:
            vs = [v for v in by_poll[ts]
                  if isinstance(v.get("lat"), (int, float)) and isinstance(v.get("lon"), (int, float))]
            pairs = []
            for i in range(len(vs)):
                for j in range(i + 1, len(vs)):
                    d = haversine_m(vs[i]["lat"], vs[i]["lon"], vs[j]["lat"], vs[j]["lon"])
                    if d <= BUNCH_M:
                        pairs.append((str(vs[i].get("vehicle_id")), str(vs[j].get("vehicle_id")), round(d, 1)))
            if pairs:
                bunch_polls += 1
                bunch_details.append({"polled_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "pairs": pairs})

        # reliability score: deduct for bunching and for empty polls in window
        empty_in_window = sum(
            1 for r in records
            if r["ts"] not in by_poll and in_service_window(r["ts"])
        )
        score = 100 - BUNCH_PENALTY * bunch_polls - EMPTY_PENALTY * empty_in_window
        score = max(0, min(100, score))

        per_route[rid] = {
            "polls_with_data": n_polls,
            "total_polls": len(records),
            "avg_vehicles_per_poll": round(avg_per_poll, 2),
            "headway_proxy_min": round(headway_proxy_min, 2) if headway_proxy_min is not None else None,
            "headway_proxy_note": ("median minutes between consecutive polls where the route "
                                   "was visible; poll-visibility metric, NOT true headway"),
            "bunching_polls": bunch_polls,
            "bunching_details": bunch_details,
            "empty_polls_in_service_window": empty_in_window,
            "reliability_score": score,
        }

    return {
        "polls_total": len(records),
        "polls_with_gap_over_expected": gaps,
        "expected_cadence_min": EXPECTED_CADENCE_MIN,
        "service_window_local": f"{SERVICE_START_H:02d}:00-{SERVICE_END_H:02d}:00 America/Detroit",
        "bunching_threshold_m": BUNCH_M,
        "routes": per_route,
    }


def text_report(res):
    L = []
    L.append("Forge Line telemetry — daily reliability report")
    L.append(f"Polls analyzed: {res['polls_total']} "
             f"(gaps over {res['expected_cadence_min']} min cadence: {res['polls_with_gap_over_expected']})")
    L.append(f"Service window: {res['service_window_local']}; bunching threshold: {res['bunching_threshold_m']:.0f} m")
    L.append("")
    if not res["routes"]:
        L.append("No route data.")
        return "\n".join(L)
    for rid, s in res["routes"].items():
        L.append(f"Route {rid}: score {s['reliability_score']}/100")
        L.append(f"  polls with data: {s['polls_with_data']}/{s['total_polls']}  "
                 f"avg vehicles/poll: {s['avg_vehicles_per_poll']}")
        hp = s["headway_proxy_min"]
        L.append(f"  headway proxy: {hp if hp is not None else 'n/a (need 2+ polls seeing the route)'} min "
                 f"(poll-visibility, not true headway)")
        L.append(f"  bunching polls: {s['bunching_polls']}  "
                 f"empty polls in service window: {s['empty_polls_in_service_window']}")
        for b in s["bunching_details"]:
            for vid1, vid2, d in b["pairs"]:
                L.append(f"    bunched {b['polled_at']}: vehicles {vid1} + {vid2} ({d} m apart)")
        L.append("")
    return "\n".join(L).rstrip()


def main():
    ap = argparse.ArgumentParser(description="Reliability/bunching report for transit telemetry.")
    ap.add_argument("files", nargs="+", help="JSONL day file(s)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    records = load_records(args.files)
    if not records:
        print("no usable poll records found", file=sys.stderr)
        sys.exit(1)
    res = analyze(records)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(text_report(res))


if __name__ == "__main__":
    main()
