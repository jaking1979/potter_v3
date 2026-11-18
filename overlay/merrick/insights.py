"""House insights: what the collected data says, and only once there is enough of it.

Every detector declares the evidence it needs (for example "5 heating runs with the outdoors below 25 F").
Until that is met it reports what it is waiting for and how far along it is, and proposes nothing. A finding
always carries its numbers. This is code; the creative step (ideas no detector covers) is the model's, weekly,
from this evidence pack only, and Jev filters those ideas (see the house-insights skill).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Optional

BINS = [(-100, 15, "below 15 F"), (15, 25, "15-25 F"), (25, 35, "25-35 F"), (35, 45, "35-45 F"), (45, 200, "above 45 F")]
NEED = {"runs_per_bin": 5, "vacancy_days": 7, "setpoint_days": 10, "trend_weeks": 4, "repeats": 3, "sensor_days": 7}


def _ts(s) -> Optional[float]:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _lines(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def bin_of(f: Optional[float]) -> Optional[str]:
    if f is None:
        return None
    for lo, hi, label in BINS:
        if lo <= f < hi:
            return label
    return None


def outdoor_at(snapshots: list[dict], ts: float, within_s: float = 3 * 3600) -> Optional[float]:
    best, dist = None, within_s
    for s in snapshots:
        t = _ts(s.get("ts"))
        if t is not None and s.get("outdoor_f") is not None and abs(t - ts) <= dist:
            best, dist = s["outdoor_f"], abs(t - ts)
    return best


def load(state: Path) -> dict:
    return {"snapshots": _lines(state / "room-snapshots.jsonl"), "episodes": _lines(state / "thermal-episodes.jsonl"),
            "shadow": _lines(state / "shadow.jsonl"), "requests": _lines(state / "path-log.jsonl")}


def _rate(e: dict) -> Optional[float]:
    r = e.get("rate_f_per_hour")
    if isinstance(r, (int, float)):
        return float(r)
    a, b = e.get("start_f"), e.get("end_f")
    t0, t1 = _ts(e.get("start_time")), _ts(e.get("end_time"))
    if None in (a, b, t0, t1) or t1 <= t0:
        return None
    return (b - a) / ((t1 - t0) / 3600)


def preheat(data: dict, house: dict) -> dict:
    """Per zone and outdoor band: median heating rate. Ready per band once it has enough runs."""
    per = defaultdict(lambda: defaultdict(list))
    for e in data["episodes"]:
        if e.get("type", "active_heating") != "active_heating":     # thermal.classify_episodes' heating runs
            continue
        t0, r = _ts(e.get("start_time")), _rate(e)
        if t0 is None or r is None or r <= 0:
            continue
        b = bin_of(outdoor_at(data["snapshots"], t0))
        if b:
            per[e.get("zone", "?")][b].append(r)
    ready, waiting = [], []
    for zid in (house.get("zones") or {}):
        for _, _, label in BINS:
            rates = per[zid][label]
            if len(rates) >= NEED["runs_per_bin"]:
                m = median(rates)
                ready.append({"detector": "preheat", "zone": zid, "outdoor": label, "runs": len(rates), "median_f_per_hour": round(m, 2),
                              "text": f"{zid}: with the outdoors {label}, it warms about {m:.1f} F an hour ({len(rates)} heating runs); "
                                      f"a 6 F rise takes about {6 / m:.1f} h. Preheats can start that long (plus a margin) before arrival."})
            else:
                waiting.append({"detector": "preheat", "zone": zid, "outdoor": label, "have": len(rates), "need": NEED["runs_per_bin"]})
    return {"ready": ready, "waiting": waiting}


def vacant_overheat(data: dict, house: dict) -> dict:
    sh = [r for r in data["shadow"] if r.get("vacant") is True]
    days = len({datetime.fromtimestamp(r["ts"], timezone.utc).date() for r in sh})
    if days < NEED["vacancy_days"]:
        return {"ready": [], "waiting": [{"detector": "vacant_overheat", "have": days, "need": NEED["vacancy_days"], "unit": "vacant days observed"}]}
    excess = defaultdict(float)
    for r in sh:
        for z in r.get("zones", []):
            if z.get("verdict") == "differs" and z.get("setpoint_f") and z.get("v3_would_hold_f") and z["setpoint_f"] > z["v3_would_hold_f"]:
                excess[z["zone"]] += 0.5                     # one observation = half an hour
    ready = [{"detector": "vacant_overheat", "zone": zid, "hours": h,
              "text": f"{zid} was held above the away hold for about {h:.0f} hours while the house was empty ({days} vacant days)."}
             for zid, h in excess.items() if h >= 6]
    return {"ready": ready, "waiting": []}


def never_reaches(data: dict, house: dict) -> dict:
    sh = [r for r in data["shadow"] if r.get("zones")]
    days = len({datetime.fromtimestamp(r["ts"], timezone.utc).date() for r in sh})
    if days < NEED["setpoint_days"]:
        return {"ready": [], "waiting": [{"detector": "never_reaches", "have": days, "need": NEED["setpoint_days"], "unit": "days of setpoint readings"}]}
    short, total = defaultdict(int), defaultdict(int)
    for r in sh:
        for z in r["zones"]:
            if z.get("mode") == "heat" and z.get("setpoint_f") is not None and z.get("room_f") is not None:
                total[z["zone"]] += 1
                short[z["zone"]] += z["room_f"] < z["setpoint_f"] - 2
    ready = [{"detector": "never_reaches", "zone": zid, "share": round(short[zid] / total[zid], 2),
              "text": f"{zid} sat more than 2 F below its setpoint in {100 * short[zid] // total[zid]}% of heating readings: "
                      "a sensor in the wrong spot, a draft, or a heating problem."}
             for zid in total if total[zid] >= 50 and short[zid] / total[zid] >= 0.3]
    return {"ready": ready, "waiting": []}


def slowing(data: dict, house: dict) -> dict:
    eps = [(e.get("zone"), _ts(e.get("start_time")), _rate(e)) for e in data["episodes"] if e.get("type", "active_heating") == "active_heating"]
    eps = [x for x in eps if x[1] and x[2] and x[2] > 0]
    if not eps:
        return {"ready": [], "waiting": [{"detector": "slowing", "have": 0, "need": NEED["trend_weeks"], "unit": "weeks of heating runs"}]}
    weeks = (max(t for _, t, _ in eps) - min(t for _, t, _ in eps)) / (7 * 86400)
    if weeks < NEED["trend_weeks"]:
        return {"ready": [], "waiting": [{"detector": "slowing", "have": round(weeks, 1), "need": NEED["trend_weeks"], "unit": "weeks of heating runs"}]}
    ready = []
    for zid in {z for z, _, _ in eps}:
        zs = sorted((t, r) for z, t, r in eps if z == zid)
        if len(zs) < 10:
            continue
        k = len(zs) // 3
        early, late = median(r for _, r in zs[:k]), median(r for _, r in zs[-k:])
        if late < early * 0.85:
            ready.append({"detector": "slowing", "zone": zid, "early": round(early, 2), "late": round(late, 2),
                          "text": f"{zid} heats {100 - 100 * late / early:.0f}% slower than {weeks:.0f} weeks ago "
                                  f"({early:.1f} -> {late:.1f} F/h; outdoor mix not adjusted): check the filter or the system."})
    return {"ready": ready, "waiting": []}


def repeats(data: dict, house: dict) -> dict:
    """The same house request made by hand again and again is an automation candidate."""
    groups = defaultdict(list)
    for r in data["requests"]:
        if r.get("intent") in ("set_temperature", "hold_away") and r.get("outcome") == "ok":
            t = _ts(r.get("ts"))
            if t is None:
                continue
            hour = datetime.fromtimestamp(t).hour
            groups[(r.get("intent"), r.get("zone"), r.get("temperature"), hour // 3)].append(t)
    ready = [{"detector": "repeats", "intent": k[0], "zone": k[1], "temperature": k[2], "times": len(v),
              "text": f"You asked for {k[0].replace('_', ' ')}{' ' + str(k[2]) + ' F' if k[2] else ''}{' in ' + k[1] if k[1] else ''} "
                      f"{len(v)} times around {3 * k[3]:02d}:00-{3 * k[3] + 3:02d}:00. That could run on its own."}
             for k, v in groups.items() if len(v) >= NEED["repeats"]]
    return {"ready": ready, "waiting": [] if ready else [{"detector": "repeats", "have": max((len(v) for v in groups.values()), default=0),
                                                           "need": NEED["repeats"], "unit": "repeats of one request"}]}


def sensor_gaps(data: dict, house: dict) -> dict:
    snaps = data["snapshots"][-int(NEED["sensor_days"] * 24):]
    if len(snaps) < 24:
        return {"ready": [], "waiting": [{"detector": "sensor_gaps", "have": len(snaps), "need": 24, "unit": "readings"}]}
    names = {k for s in snaps for k in (s.get("temps") or {})}
    ready = []
    for n in sorted(names):
        missing = sum(1 for s in snaps if (s.get("temps") or {}).get(n) is None)
        if missing / len(snaps) >= 0.2:
            ready.append({"detector": "sensor_gaps", "sensor": n, "share": round(missing / len(snaps), 2),
                          "text": f"{n} gave no reading {100 * missing // len(snaps)}% of the time lately: battery or signal."})
    return {"ready": ready, "waiting": []}


DETECTORS = [preheat, vacant_overheat, never_reaches, slowing, repeats, sensor_gaps]


def analyse(state: Path, house: dict) -> dict:
    data = load(state)
    ready, waiting = [], []
    for d in DETECTORS:
        r = d(data, house)
        ready += r["ready"]
        waiting += r["waiting"]
    outs = [s.get("outdoor_f") for s in data["snapshots"] if s.get("outdoor_f") is not None]
    coverage = {label: sum(1 for f in outs if lo <= f < hi) for lo, hi, label in BINS}
    ts = [t for t in (_ts(s.get("ts")) for s in data["snapshots"]) if t]
    return {"days_of_data": round((max(ts) - min(ts)) / 86400, 1) if len(ts) > 1 else 0, "outdoor_readings": coverage,
            "coldest_seen_f": min(outs) if outs else None, "ready": ready, "waiting": waiting}


def render(rep: dict) -> str:
    lines = [f"House data: {rep['days_of_data']} days; coldest outdoor reading {rep['coldest_seen_f'] if rep['coldest_seen_f'] is not None else 'none yet'} F."]
    if rep["ready"]:
        lines.append("What the data supports:")
        lines += [f"- {f['text']}" for f in rep["ready"]]
    else:
        lines.append("Nothing the data can support yet.")
    pre = [w for w in rep["waiting"] if w["detector"] == "preheat"]
    if pre:
        bands = defaultdict(list)
        for w in pre:
            bands[w["outdoor"]].append(w["have"])
        lines.append("Waiting for data: preheat timing with the outdoors " +
                     "; ".join(f"{b} ({min(v)} of {NEED['runs_per_bin']} heating runs)" for b, v in bands.items()) + ".")
    for w in rep["waiting"]:
        if w["detector"] != "preheat":
            lines.append(f"Waiting for data: {w['detector'].replace('_', ' ')} ({w['have']} of {w['need']} {w.get('unit', '')}).")
    return "\n".join(lines)
