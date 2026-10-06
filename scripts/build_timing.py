#!/usr/bin/env python3
"""Build the endpoint/v1 event stream and timing model from telemetry history.

Reads data/*.jsonl (telemetry/v1 poll records), runs the per-vehicle segment
detector defined in PROTOCOL.md, and writes:

  data/vehicles/events.jsonl   - endpoint/v1 event stream (append-only)
  data/timing/segments.json    - aggregated empirical timing model
  data/timing/watermark.json   - incremental processing state

Incremental: per-day line counts in the watermark mean re-runs only process
new polls. Detector state (anchors, dwell, anomaly episodes) persists in the
watermark so event emission is idempotent.

Honesty rules: traversals spanning a poll gap > 30 min are discarded (anchor
reset, no event). Timing cells need >= 3 observations. Anomalies are flagged,
never deleted.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

try:
    from zoneinfo import ZoneInfo
    DETROIT = ZoneInfo("America/Detroit")
except Exception:
    DETROIT = None

SCHEMA = "forge-line.endpoint/v1"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REF = ROOT / "reference" / "route-stops.json"
EVENTS = DATA / "vehicles" / "events.jsonl"
SEGMENTS = DATA / "timing" / "segments.json"
WATERMARK = DATA / "timing" / "watermark.json"

EARTH_R = 6371000.0
SWITCH_CONFIRM_M = 120.0   # candidate stop within this distance -> switch candidate
ANCHOR_BEHIND_M = 250.0    # ...or anchor this far behind -> confirm immediately
MAX_GAP_S = 1800           # poll gap beyond this: reset anchor, emit nothing
MAX_SPEED_MS = 31.3        # 70 mph: above this is an impossible jump
FROZEN_POLLS = 6           # identical rounded position this many polls -> frozen
OFF_ROUTE_M = 800.0
QUIET_POLLS = 3            # missed polls before endpoint.quiet
MIN_CELL_N = 3


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def parse_ts(s):
    # telemetry/v1 stamps are UTC with Z; vehicle updated_at is Detroit wall time
    if s.endswith("Z"):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(s)
    if DETROIT:
        return dt.replace(tzinfo=DETROIT)
    return dt.replace(tzinfo=timezone.utc)


def bucket(dt):
    local = dt.astimezone(DETROIT) if DETROIT else dt
    dow = local.weekday()  # 0=Mon
    if dow == 5:
        return "sat"
    if dow == 6:
        return "sun"
    h = local.hour
    if 6 <= h < 9:
        return "wkd_am"
    if 9 <= h < 15:
        return "wkd_mid"
    if 15 <= h < 19:
        return "wkd_pm"
    if 19 <= h < 24:
        return "wkd_eve"
    return "wkd_night"


def nearest_stop(lat, lon, stops):
    best, best_d = None, float("inf")
    for s in stops:
        d = haversine(lat, lon, s["lat"], s["lon"])
        if d < best_d:
            best, best_d = s, d
    return best, best_d


def write_latest():
    """data/latest.json: the single most recent poll, for instant page paint
    and as a lightweight "last known positions" fallback. Rebuilt from the
    newest raw poll file on every run (the old log_poll.py wrote this inline;
    build_timing.py owns it now that polling moved to the Netlify logger)."""
    newest = None
    for p in sorted(DATA.glob("20*.jsonl")):
        newest = p
    if newest is None:
        return
    last = None
    with open(newest) as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if last:
        with open(DATA / "latest.json", "w") as out:
            out.write(last if last.endswith("\n") else last + "\n")


def main():
    with open(REF) as f:
        route_stops = json.load(f)
    all_stops = [s for stops in route_stops.values() for s in stops]

    wm = {"days": {}, "vehicles": {}}
    if WATERMARK.exists():
        try:
            wm = json.loads(WATERMARK.read_text())
        except json.JSONDecodeError:
            pass
    days_done = wm.setdefault("days", {})
    vstate = wm.setdefault("vehicles", {})

    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    SEGMENTS.parent.mkdir(parents=True, exist_ok=True)
    evf = open(EVENTS, "a")

    def emit(event, vid, route, recorded_at, polled_at, **fields):
        emit.seq += 1
        rec = {"schema": SCHEMA, "event": event,
               "event_id": f"{polled_at}/{vid}/{event}/{emit.seq}",
               "recorded_at": recorded_at, "vehicle_id": vid,
               "route_id": route, "source_poll": polled_at}
        rec.update(fields)
        evf.write(json.dumps(rec, separators=(",", ":")) + "\n")
    emit.seq = 0

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_events = 0

    for dayfile in sorted(DATA.glob("*.jsonl")):
        if dayfile.name in ("latest.json",):
            continue
        lines = dayfile.read_text().splitlines()
        start = days_done.get(dayfile.name, 0)
        for line in lines[start:]:
            line = line.strip()
            if not line:
                continue
            try:
                poll = json.loads(line)
            except json.JSONDecodeError:
                continue
            polled_at = poll.get("polled_at", "")
            try:
                pts = parse_ts(polled_at)
            except ValueError:
                continue
            seen = set()
            for v in poll.get("vehicles", []):
                vid = str(v.get("vehicle_id", ""))
                if not vid:
                    continue
                seen.add(vid)
                route = str(v.get("route_id", ""))
                lat, lon = v["lat"], v["lon"]
                obs_at = None
                if v.get("updated_at"):
                    try:
                        obs_at = parse_ts(v["updated_at"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    except ValueError:
                        pass
                st = vstate.get(vid)
                if st is None:
                    st = {"anchor": None, "anchor_t": None, "cand": None,
                          "dwell_s": 0.0, "frozen_n": 0, "last_pos": None,
                          "quiet_n": 0, "quiet_emitted": False,
                          "offroute": False, "frozen_emitted": False}
                    vstate[vid] = st
                    emit("endpoint.enrolled", vid, route, now_utc, polled_at,
                         observed_at=obs_at, received_at=polled_at)
                    new_events += 1
                # assignment change
                if st.get("route") != route:
                    if st.get("route"):
                        emit("assignment.changed", vid, route, now_utc, polled_at,
                             old_route=st["route"], new_route=route,
                             observed_at=obs_at, received_at=polled_at)
                        new_events += 1
                    st["route"] = route
                    st["anchor"] = None  # re-anchor on new route
                    st["cand"] = None
                st["quiet_n"] = 0
                st["quiet_emitted"] = False

                stops = route_stops.get(route)
                ghost = False
                if not stops:
                    stops = all_stops
                    ghost = True
                    if not st.get("ghost_emitted"):
                        emit("anomaly.flagged", vid, route, now_utc, polled_at,
                             subtype="ghost_route", evidence={"route_id": route},
                             observed_at=obs_at, received_at=polled_at)
                        new_events += 1
                        st["ghost_emitted"] = True
                ns, nd = nearest_stop(lat, lon, stops)

                # impossible jump check (needs previous position + time)
                dt = None
                if st.get("last_t"):
                    try:
                        dt = (pts - parse_ts(st["last_t"])).total_seconds()
                    except ValueError:
                        dt = None
                if st["last_pos"] and dt and dt > 0:
                    d = haversine(st["last_pos"][0], st["last_pos"][1], lat, lon)
                    if d / dt > MAX_SPEED_MS:
                        emit("anomaly.flagged", vid, route, now_utc, polled_at,
                             subtype="impossible_jump",
                             evidence={"implied_mph": round(d / dt * 2.237, 1),
                                       "gap_s": round(dt, 1)},
                             observed_at=obs_at, received_at=polled_at,
                             at={"lat": lat, "lon": lon})
                        new_events += 1
                        st["last_pos"] = [lat, lon]
                        st["last_t"] = polled_at
                        continue  # bad GPS must not move the anchor

                # frozen check
                rpos = (round(lat, 4), round(lon, 4))
                if st["last_pos"] and (round(st["last_pos"][0], 4), round(st["last_pos"][1], 4)) == rpos:
                    st["frozen_n"] = st.get("frozen_n", 0) + 1
                else:
                    st["frozen_n"] = 0
                    st["frozen_emitted"] = False
                if st["frozen_n"] >= FROZEN_POLLS and not st["frozen_emitted"]:
                    emit("anomaly.flagged", vid, route, now_utc, polled_at,
                         subtype="frozen",
                         evidence={"polls": st["frozen_n"]},
                         observed_at=obs_at, received_at=polled_at,
                         at={"lat": lat, "lon": lon})
                    new_events += 1
                    st["frozen_emitted"] = True

                # off-route check
                if nd > OFF_ROUTE_M:
                    if not st["offroute"]:
                        emit("anomaly.flagged", vid, route, now_utc, polled_at,
                             subtype="off_route",
                             evidence={"nearest_stop_m": round(nd, 1)},
                             observed_at=obs_at, received_at=polled_at,
                             at={"lat": lat, "lon": lon})
                        new_events += 1
                        st["offroute"] = True
                    st["last_pos"] = [lat, lon]
                    st["last_t"] = polled_at
                    continue
                st["offroute"] = False

                anchor = st["anchor"]
                if anchor is None:
                    # (re)anchor on the nearest stop
                    st["anchor"] = ns["id"]
                    st["anchor_t"] = polled_at
                    st["cand"] = None
                    st["dwell_s"] = 0.0
                elif ns["id"] == anchor:
                    st["cand"] = None
                    # dwell accumulation
                    moved = haversine(st["last_pos"][0], st["last_pos"][1], lat, lon) if st["last_pos"] else 0
                    speed = v.get("speed_mph")
                    if (speed is not None and speed < 5) or moved < 30:
                        inc = min(dt if dt and dt > 0 else 0, 600)
                        st["dwell_s"] = st.get("dwell_s", 0) + inc
                else:
                    # candidate switch with debounce
                    anchor_stop = next((s for s in stops if s["id"] == anchor), None)
                    anchor_d = haversine(anchor_stop["lat"], anchor_stop["lon"], lat, lon) if anchor_stop else 1e9
                    confirmed = (st["cand"] == ns["id"]) or (nd < SWITCH_CONFIRM_M and anchor_d > ANCHOR_BEHIND_M)
                    if confirmed:
                        try:
                            secs = (pts - parse_ts(st["anchor_t"])).total_seconds()
                        except ValueError:
                            secs = -1
                        if 0 < secs <= MAX_GAP_S:
                            seg_dist = haversine(anchor_stop["lat"], anchor_stop["lon"], ns["lat"], ns["lon"]) if anchor_stop else None
                            emit("segment.traversed", vid, route, now_utc, polled_at,
                                 observed_at=obs_at, received_at=polled_at,
                                 from_stop=anchor, to_stop=ns["id"],
                                 seconds=round(secs, 1),
                                 distance_m=round(seg_dist, 1) if seg_dist else None,
                                 bucket=bucket(pts),
                                 at={"lat": ns["lat"], "lon": ns["lon"],
                                     "route_id": route, "nearest_stop": ns["id"],
                                     "stop_distance_m": round(nd, 1),
                                     "segment": [anchor, ns["id"]]})
                            new_events += 1
                            if st.get("dwell_s", 0) >= 60:
                                emit("dwell.observed", vid, route, now_utc, polled_at,
                                     observed_at=obs_at, received_at=polled_at,
                                     stop=anchor, dwell_seconds=round(st["dwell_s"], 1))
                                new_events += 1
                        # move anchor regardless (gap resets silently per honesty rules)
                        st["anchor"] = ns["id"]
                        st["anchor_t"] = polled_at
                        st["cand"] = None
                        st["dwell_s"] = 0.0
                    else:
                        st["cand"] = ns["id"]
                st["last_pos"] = [lat, lon]
                st["last_t"] = polled_at

            # quiet detection for vehicles missing from this poll
            for vid, st in vstate.items():
                if vid in seen:
                    continue
                st["quiet_n"] = st.get("quiet_n", 0) + 1
                if st["quiet_n"] >= QUIET_POLLS and not st.get("quiet_emitted"):
                    emit("endpoint.quiet", vid, st.get("route", ""), now_utc, polled_at,
                         last_seen=st.get("last_t"), missed_polls=st["quiet_n"])
                    new_events += 1
                    st["quiet_emitted"] = True
        days_done[dayfile.name] = len(lines)
    evf.close()

    # timing model: aggregate segment.traversed events
    cells = defaultdict(list)
    dists = {}
    try:
        with open(EVENTS) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("event") != "segment.traversed":
                    continue
                key = (e["route_id"], e["from_stop"], e["to_stop"], e["bucket"])
                cells[key].append(e["seconds"])
                if e.get("distance_m"):
                    dists[key] = e["distance_m"]
    except FileNotFoundError:
        pass

    model = {}
    for (route, fs, ts, b), vals in cells.items():
        if len(vals) < MIN_CELL_N:
            continue
        vals.sort()
        n = len(vals)
        p50 = vals[n // 2]
        p90 = vals[min(n - 1, int(n * 0.9))]
        model[f"{route}|{fs}|{ts}|{b}"] = {
            "n": n,
            "mean_s": round(sum(vals) / n, 1),
            "p50_s": round(p50, 1),
            "p90_s": round(p90, 1),
            "dist_m": dists.get((route, fs, ts, b)),
        }
    with open(SEGMENTS, "w") as f:
        json.dump({"schema": SCHEMA,
                   "built_at": now_utc,
                   "cells": len(model),
                   "segments": model}, f, separators=(",", ":"))
        f.write("\n")
    with open(WATERMARK, "w") as f:
        json.dump(wm, f, separators=(",", ":"))
        f.write("\n")
    write_latest()
    print(f"processed polls; new events: {new_events}; timing cells: {len(model)}")


if __name__ == "__main__":
    main()
