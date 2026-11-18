"""House dry-run: what v3 WOULD do, recorded next to what the house is actually doing, without touching it.

While another assistant runs the house, v3's own logic runs in shadow: every half hour it reads Home Assistant
(read-only), decides what it would hold each zone at (the data-driven away hold, only when the house is vacant)
and whether it would raise a cold alert, and writes one line. The report compares that with the setpoints the
house actually had, which reflect the other assistant's decisions. Agreement and every divergence are the
evidence for the switch-over decision.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from . import coldwatch, fastpath, hold

TOL_F = 0.6


def observe(home, house: dict, now_ts: Optional[float] = None) -> dict:
    """One read-only observation and v3's would-do for it."""
    r = fastpath.cold_readings(home, house, now_ts)
    zones = []
    for zid in (house.get("zones") or {}):
        z = home.zone_reading(zid)
        would = None
        if r.get("vacant") is True:
            d = hold.decide(house, r.get("basement_f"), None, z.get("setpoint_f"))
            if d is not None:
                would = d["target_f"]
        verdict = "no-opinion"
        if not z.get("reachable") or z.get("setpoint_f") is None:
            verdict = "unreadable"
        elif would is not None:
            verdict = "agrees" if abs(z["setpoint_f"] - would) <= TOL_F else "differs"
        elif r.get("vacant") is False:
            verdict = "occupied"                         # v3 leaves an occupied house's setpoints to the people in it
        zones.append({"zone": zid, "setpoint_f": z.get("setpoint_f"), "room_f": z.get("room_f"), "mode": z.get("mode"),
                      "v3_would_hold_f": would, "verdict": verdict})
    findings = coldwatch.reportable(coldwatch.evaluate(r, coldwatch.Thresholds.from_house(house), None))
    return {"ts": r["ts"], "vacant": r.get("vacant"), "basement_f": r.get("basement_f"),
            "hold_reason": r.get("expected_hold_reason"), "zones": zones,
            "v3_would_alert": [f["text"] for f in findings]}


def append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def read(path: Path, since_ts: float = 0) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts", 0) >= since_ts:
            out.append(r)
    return out


def report(recs: list[dict]) -> dict:
    """Agreement while vacant, every divergence (merged into stretches), alerts v3 would have sent."""
    counts = {"agrees": 0, "differs": 0, "occupied": 0, "unreadable": 0, "no-opinion": 0}
    stretches: dict[str, list[dict]] = {}
    open_: dict[str, dict] = {}
    for r in sorted(recs, key=lambda x: x["ts"]):
        for z in r.get("zones", []):
            counts[z["verdict"]] = counts.get(z["verdict"], 0) + 1
            zid = z["zone"]
            if z["verdict"] == "differs":
                cur = open_.get(zid)
                if cur and cur["actual_f"] == z["setpoint_f"] and cur["v3_f"] == z["v3_would_hold_f"]:
                    cur["to"] = r["ts"]; cur["n"] += 1
                else:
                    cur = {"zone": zid, "from": r["ts"], "to": r["ts"], "n": 1, "actual_f": z["setpoint_f"], "v3_f": z["v3_would_hold_f"]}
                    stretches.setdefault(zid, []).append(cur)
                    open_[zid] = cur
            else:
                open_.pop(zid, None)
    judged = counts["agrees"] + counts["differs"]
    alerts: dict[str, int] = {}
    for r in recs:
        for a in r.get("v3_would_alert", []):
            alerts[a] = alerts.get(a, 0) + 1
    return {"observations": len(recs), "counts": counts,
            "agreement_pct": round(100 * counts["agrees"] / judged) if judged else None,
            "divergences": [s for v in stretches.values() for s in v], "would_alert": alerts}


def render(rep: dict, days: int) -> str:
    if not rep["observations"]:
        return f"House dry-run: no observations in the last {days} days (is potter-merrick-shadow running?)."
    c = rep["counts"]
    lines = [f"House dry-run, last {days} days: {rep['observations']} observations."]
    if rep["agreement_pct"] is None:
        lines.append(f"No vacant periods to compare (occupied readings: {c['occupied']}; unreadable: {c['unreadable']}).")
    else:
        lines.append(f"While vacant, v3's hold agreed with the house {rep['agreement_pct']}% of the time "
                     f"({c['agrees']} agree, {c['differs']} differ; {c['unreadable']} unreadable).")
    for s in rep["divergences"][:8]:
        hrs = max(0.5, (s["to"] - s["from"]) / 3600 + 0.5)
        when = time.strftime("%a %d %b %H:%M", time.localtime(s["from"]))
        lines.append(f"- {s['zone']}: house at {s['actual_f']:g} F, v3 would hold {s['v3_f']:g} F, from {when} for about {hrs:.1f} h")
    for text, n in list(rep["would_alert"].items())[:5]:
        lines.append(f"- v3 would have alerted ({n}x): {text}")
    return "\n".join(lines)
