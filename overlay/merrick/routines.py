"""House routines: bedtime, wake, close-up, coming up, arrival, and the fireplace. Data in the house file, run by code.

v2 re-derived these from prose every time and got them wrong (a wake ramp that never existed; one that fired, failed
and deleted itself). Here each routine is a list of steps in the house file's "routines" block, run with read-back on
every device, and everything that must happen later is a plan or a watch that `merrick watch` (one recurring job)
evaluates every few minutes, re-reading the house each time.

  mode "shadow" (the default): nothing is written; every step and decision is recorded as "would".
  mode "live":                 steps are written and read back.

Step forms:
  {"zone": "<zone id>", "mode": "heat", "setpoint": 64}      heat first if the unit is off, then the setpoint
  {"zone": "<zone id>", "off": true, "unless_home": "person.x"}   left exactly as it is while that person is home
  {"fan": "fan.x", "percentage": 33}                        assumed-state fans: reported as sent, not as observed
  {"switch": "switch.x" | "light.x" | "input_boolean.x", "on": true|false}
  {"away_hold": "<zone id>"}                                the data-driven away hold (hold.py)

Plans (warm by a time): {"zone", "judge": sensor entity, "target_f", "deadline", "at_deadline": [steps]}. Heating
starts when the judge sensor needs that long at the zone's learned rate (room truth, not return air), plus a margin.
Watches: lights off once a room is free (no "busy" signal for two checks running), the overnight bedroom record,
and the triggers (fireplace fan on; arrival by front door unlocked or a person arriving home while the house is closed).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import hold
from .ha import SafeHome, num

DEFAULT_RATE_F_PER_H = 1.1           # room-truth warm-up measured in v2's one step test; replaced by learned values
MARGIN_H = 0.25
MAX_LEAD_H = 5.0                     # longer than this: say so (the owner is told, not surprised)
LATE_START_MIN = 30
BUSY_STATES = {"on", "playing", "paused", "buffering", "home", "detected", "occupied"}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ---- configuration -------------------------------------------------------------------------------------------
def cfg(house: dict) -> dict:
    return dict((house or {}).get("routines") or {})


def live(house: dict) -> bool:
    return cfg(house).get("mode") == "live"


def tz(house: dict):
    name = cfg(house).get("timezone")
    try:
        return ZoneInfo(name) if name else timezone.utc
    except Exception:                                   # noqa: BLE001
        return timezone.utc


def check(house: dict) -> list[str]:
    """Problems in the routines block that would make a routine misbehave. Empty list = usable."""
    c, out = cfg(house), []
    if not c:
        return ["no 'routines' block in the house file"]
    if not c.get("timezone"):
        out.append("routines.timezone is missing (times would be read as UTC)")
    zones = (house.get("zones") or {})
    allowed = {str(e).lower() for e in (house.get("controllable") or [])} | \
              {str(z.get("climate_entity")).lower() for z in zones.values() if z.get("climate_entity")}

    def steps(where, lst):
        for s in lst or []:
            if "zone" in s and s["zone"] not in zones:
                out.append(f"{where}: unknown zone '{s['zone']}'")
            for k in ("fan", "switch"):
                if k in s and str(s[k]).lower() not in allowed:
                    out.append(f"{where}: {s[k]} is not in the house file's controllable list")
            if "away_hold" in s and s["away_hold"] not in zones:
                out.append(f"{where}: unknown zone '{s['away_hold']}'")
    for name in ("bedtime", "wake", "close_up", "coming_up", "arrival"):
        steps(name, (c.get(name) or {}).get("steps"))
    for name in ("wake", "coming_up"):
        r = c.get(name) or {}
        if r and r.get("zone") not in zones:
            out.append(f"{name}: 'zone' must name the zone that does the heating")
        if r and not r.get("judge"):
            out.append(f"{name}: 'judge' (the sensor that must be warm) is missing")
    for k, v in (c.get("lights_when_free") or {}).items() if isinstance(c.get("lights_when_free"), dict) else []:
        pass
    lw = (c.get("bedtime") or {}).get("lights_when_free") or {}
    for e in lw.get("lights") or []:
        if str(e).lower() not in allowed:
            out.append(f"bedtime.lights_when_free: {e} is not in the controllable list")
    fp = c.get("fireplace") or {}
    if fp and not (fp.get("power_entity") or fp.get("fan_entity")):
        out.append("fireplace: needs 'power_entity' (watts) or 'fan_entity' (on/off)")
    return out


# ---- step execution -------------------------------------------------------------------------------------------
def _is_home(home: SafeHome, entity: str) -> Optional[bool]:
    st = home.read(entity)
    s = str((st or {}).get("state", "")).lower()
    return None if s in ("", "unknown", "unavailable") else s == "home"


def run_step(home: SafeHome, house: dict, s: dict) -> dict:
    """-> {"step", "ok", "text", "would"}. Shadow mode reads only and reports what it would do."""
    sh = not live(house)
    zones = house.get("zones") or {}
    if "zone" in s:
        z = zones.get(s["zone"]) or {}
        eid, name = z.get("climate_entity"), z.get("name") or s["zone"]
        if s.get("off"):
            if s.get("unless_home"):
                here = _is_home(home, s["unless_home"])
                if here is not False:
                    return {"step": s, "ok": True, "would": False,
                            "text": f"{name}: left as it is ({'they are home' if here else 'cannot tell whether they are home'})"}
            if sh:
                return {"step": s, "ok": True, "would": True, "text": f"would turn {name} off"}
            r = home.set_hvac_mode(eid, "off")
            return {"step": s, "ok": r.ok, "would": False, "text": f"{name} off: {r.message}"}
        target, mode = s.get("setpoint"), s.get("mode", "heat")
        if sh:
            return {"step": s, "ok": True, "would": True, "text": f"would set {name} to {mode} {target:g} F"}
        cur = (home.read(eid) or {}).get("state")
        msgs, ok = [], True
        if cur != mode:                                  # a setpoint on an "off" unit is silently dropped
            r = home.set_hvac_mode(eid, mode)
            ok, msgs = r.ok, [r.message]
        if ok and target is not None:
            r = home.set_temperature(eid, target)
            ok = r.ok
            msgs.append(r.message)
        return {"step": s, "ok": ok, "would": False, "text": f"{name} {mode} {target:g} F: " + " ".join(msgs)}
    if "fan" in s:
        if sh:
            return {"step": s, "ok": True, "would": True, "text": f"would set {s['fan']} to {s['percentage']}%"}
        r = home.fan_percentage(s["fan"], s["percentage"])
        return {"step": s, "ok": r.ok, "would": False, "text": r.message}
    if "switch" in s:
        word = "on" if s.get("on") else "off"
        if sh:
            return {"step": s, "ok": True, "would": True, "text": f"would turn {s['switch']} {word}"}
        r = home.switch(s["switch"], bool(s.get("on")))
        return {"step": s, "ok": r.ok, "would": False, "text": r.message}
    if "away_hold" in s:
        z = zones.get(s["away_hold"]) or {}
        cw = house.get("cold_watch") or {}
        sensor = ((house.get("sensors") or {}).get(cw.get("basement_sensor", "basement")) or {}).get("entity")
        d = hold.decide(house, home.value(sensor) if sensor else None, None, None)
        if not d:
            return {"step": s, "ok": False, "would": False, "text": "no away hold is configured"}
        return run_step(home, house, {"zone": s["away_hold"], "mode": "heat", "setpoint": d["target_f"]})
    return {"step": s, "ok": False, "would": False, "text": f"unknown step {json.dumps(s)}"}


def run_steps(home: SafeHome, house: dict, steps: list) -> list[dict]:
    return [run_step(home, house, s) for s in steps or []]


# ---- state -----------------------------------------------------------------------------------------------------
def load(path) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_suffix(".tmp")
    t.write_text(json.dumps(data, indent=1))
    t.replace(p)


def update(state_dir, fn) -> dict:
    """Read, change and write state/routines.json under the state directory's lock (the watcher job and a request
    may run at the same moment)."""
    import fcntl
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".routines.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        st = load(d / "routines.json")
        new = fn(st)
        save(d / "routines.json", new)
        return new


def records(state_dir) -> list[dict]:
    out = []
    try:
        for line in (Path(state_dir) / "routines-log.jsonl").read_text().splitlines()[-2000:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def log(path, rec: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


# ---- time ------------------------------------------------------------------------------------------------------
_CLOCK = re.compile(r"\b(?:at )?(\d{1,2})(?::(\d{2}))? ?(am|pm|a\.m\.|p\.m\.)?\b")


def _clock(text: str) -> Optional[tuple[int, int, Optional[str]]]:
    for m in _CLOCK.finditer(text):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "") or None
        if (m.group(2) or ap or "at " in m.group(0)) and 0 <= h <= 23 and 0 <= mi < 60:
            return h, mi, ap
    return None


def wake_time(text: str, house: dict, now: datetime) -> datetime:
    """'up at 7:30' -> the next 7:30 (am unless pm is said); none said -> the configured default."""
    z = tz(house)
    local = now.astimezone(z)
    m = re.search(r"\b(up|wake|waking|get up|getting up|alarm)\b(.*)", text)
    c = _clock(m.group(2)) if m else None
    if c:
        h, mi, ap = c
        h = h + 12 if ap == "pm" and h < 12 else (0 if ap == "am" and h == 12 else h)
    else:
        h, mi = (int(x) for x in str((cfg(house).get("wake") or {}).get("default", "07:30")).split(":"))
    t = local.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t <= local:
        t += timedelta(days=1)
    return t.astimezone(timezone.utc)


def arrival_time(text: str, house: dict, now: datetime) -> tuple[Optional[datetime], str]:
    """'coming up Friday at 7 pm' -> (when, ""), or (None, the question to ask)."""
    z = tz(house)
    local = now.astimezone(z)
    c = _clock(text)
    if not c:
        return None, "When will you get there? (day and time)"
    h, mi, ap = c
    if ap is None and h <= 12:
        if re.search(r"\b(tonight|evening|night|pm)\b", text):
            ap = "pm"
        elif re.search(r"\bmorning\b", text):
            ap = "am"
        elif h >= 13:
            ap = None
        else:
            return None, f"Arriving at {h} am or {h} pm?"
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    day = local.date()
    if re.search(r"\btomorrow\b", text):
        day += timedelta(days=1)
    else:
        for i, d in enumerate(DAYS):
            if re.search(r"\b" + d[:3] + r"(" + d[3:] + r")?\b", text):
                ahead = (i - local.weekday()) % 7
                day += timedelta(days=ahead)
                break
    t = datetime.combine(day, dtime(h, mi), tzinfo=z)
    if t <= local:
        if any(re.search(r"\b" + d[:3], text) for d in DAYS):
            t += timedelta(days=7)
        else:
            return None, "That time has already passed today. Which day?"
    return t.astimezone(timezone.utc), ""


def is_night(home: SafeHome, house: dict, when: datetime) -> Optional[bool]:
    """After sunset or before sunrise at the house, using Home Assistant's sun entity (time of day carried to the
    arrival date). None when the sun entity cannot be read."""
    attrs = (home.read("sun.sun") or {}).get("attributes") or {}
    try:
        rise = datetime.fromisoformat(str(attrs["next_rising"]).replace("Z", "+00:00")).astimezone(timezone.utc).time()
        sets = datetime.fromisoformat(str(attrs["next_setting"]).replace("Z", "+00:00")).astimezone(timezone.utc).time()
    except (KeyError, ValueError):
        return None
    t = when.astimezone(timezone.utc).time()
    day = (rise <= t < sets) if rise < sets else (t >= rise or t < sets)
    return not day


def heating_season(home: SafeHome, house: dict) -> tuple[bool, str]:
    below = float(cfg(house).get("heating_below_f", 55))
    w = home.read(house.get("weather_entity") or "") or {}
    out = num((w.get("attributes") or {}).get("temperature"))
    if out is None:
        return True, "outdoor temperature unknown, so the heating rules apply"
    return out < below, f"outdoor {out:.0f} F"


# ---- plans: warm by a time --------------------------------------------------------------------------------------
def rate(house: dict, model: dict, zone: str) -> tuple[float, str]:
    z = (model or {}).get("rooms", {}).get(zone) or (model or {}).get("zones", {}).get(zone) or {}
    learned = (z.get("heating") or {})
    if learned.get("samples", 0) >= 3 and learned.get("room_f_per_hour"):
        return float(learned["room_f_per_hour"]), f"learned from {learned['samples']} warm-ups"
    prior = ((house.get("zones") or {}).get(zone) or {}).get("room_heat_f_per_hour")
    if prior:
        return float(prior), "the house file's estimate"
    return DEFAULT_RATE_F_PER_H, "one measured test (about 1.1 F an hour in the rooms)"


def make_plan(kind: str, zone: str, judge: str, target: float, deadline: datetime, at_deadline: list | None = None) -> dict:
    return {"id": uuid.uuid4().hex[:8], "kind": kind, "zone": zone, "judge": judge, "target_f": float(target),
            "deadline": deadline.isoformat(), "at_deadline": at_deadline or [], "status": "pending"}


def start_time(plan: dict, current: Optional[float], house: dict, model: dict) -> tuple[datetime, float, str]:
    deadline = datetime.fromisoformat(plan["deadline"])
    r, why = rate(house, model, plan["zone"])
    lag = float(((house.get("zones") or {}).get(plan["zone"]) or {}).get("thermal_lag_minutes", 20)) / 60.0
    if current is None:
        need = 3.0
        why = "the judge sensor has no reading, so a 3-hour lead is used"
    else:
        need = max(0.0, plan["target_f"] - current) / r + lag + MARGIN_H
    return deadline - timedelta(hours=need), need, why


def describe_plan(plan: dict, home: SafeHome, house: dict, model: dict) -> str:
    cur = home.value(plan["judge"])
    start, need, why = start_time(plan, cur, house, model)
    z = tz(house)
    s = start.astimezone(z).strftime("%-I:%M %p").lower()
    d = datetime.fromisoformat(plan["deadline"]).astimezone(z).strftime("%a %-I:%M %p").lower()
    now_txt = f"now {cur:.0f} F" if cur is not None else "no reading now"
    text = f"heating starts about {s} so it is {plan['target_f']:g} F by {d} ({now_txt}; rate: {why})"
    if need > MAX_LEAD_H:
        text += f". That is a {need:.1f}-hour warm-up: say if you would rather start later and accept a cooler wake"
    return text


# ---- the routines ------------------------------------------------------------------------------------------------
def bedtime(home: SafeHome, house: dict, model: dict, text: str, now: datetime) -> tuple[list[dict], list[str], dict]:
    """-> (step results, summary lines, new state additions {plans, watches})."""
    c = cfg(house)
    b = c.get("bedtime") or {}
    season, why = heating_season(home, house)
    steps = [s for s in b.get("steps") or [] if season or not s.get("heating")]
    results = run_steps(home, house, steps)
    lines, plans, watches = [], [], []
    w = c.get("wake") or {}
    if season and w:
        wake = wake_time(text, house, now)
        p = make_plan("wake", w["zone"], w["judge"], w.get("target_f", 70), wake, w.get("steps"))
        plans.append(p)
        lines.append("Wake: " + describe_plan(p, home, house, model))
    lw = b.get("lights_when_free") or {}
    if lw.get("lights"):
        watches.append({"id": uuid.uuid4().hex[:8], "kind": "lights_when_free", "lights": lw["lights"],
                        "busy": lw.get("busy") or [], "free_checks": 0,
                        "until": (now + timedelta(hours=float(lw.get("give_up_hours", 4)))).isoformat()})
        lines.append("Downstairs lights go off once the room is no longer in use.")
    if b.get("watch_sensor"):
        watches.append({"id": uuid.uuid4().hex[:8], "kind": "overnight", "sensor": b["watch_sensor"],
                        "label": b.get("watch_label", "bedroom"), "min": None, "max": None,
                        "until": wake_time(text, house, now).isoformat() if w else (now + timedelta(hours=10)).isoformat()})
    if not season:
        lines.append(f"Heating rules skipped ({why}).")
    return results, lines, {"plans": plans, "watches": watches}


def coming_up(home: SafeHome, house: dict, model: dict, when: datetime) -> tuple[list[dict], list[str], dict]:
    cu = cfg(house).get("coming_up") or {}
    results = run_steps(home, house, cu.get("steps"))
    season, why = heating_season(home, house)
    lines, plans = [], []
    if season and cu.get("zone"):
        night = is_night(home, house, when)
        target = cu.get("night_f", 68) if night in (True, None) else cu.get("day_f", 70)
        p = make_plan("arrival", cu["zone"], cu["judge"], target, when)
        plans.append(p)
        lines.append(f"Arrival ({'night' if night else 'day' if night is False else 'time of day unknown'}): "
                     + describe_plan(p, home, house, model))
    elif not season:
        lines.append(f"No heating needed ({why}).")
    return results, lines, {"plans": plans, "watches": []}


def arrival(home: SafeHome, house: dict, why: str) -> tuple[list[dict], list[str]]:
    a = cfg(house).get("arrival") or {}
    return run_steps(home, house, a.get("steps")), [f"Arrival ({why})."]


def close_up(home: SafeHome, house: dict, st: dict) -> tuple[list[dict], list[str]]:
    """The non-protected half of closing up (loads, climate, the flag). Pending plans are cancelled."""
    cu = cfg(house).get("close_up") or {}
    cancelled = [p for p in st.get("plans", []) if p.get("status") == "pending"]
    if live(house):
        for p in cancelled:
            p["status"] = "cancelled"
    lines = [f"Cancelled {len(cancelled)} pending warm-up plan(s)."] if cancelled else []
    return run_steps(home, house, cu.get("steps")), lines


# ---- the watcher (one recurring job) -------------------------------------------------------------------------------
def _state_of(home: SafeHome, eid: str) -> str:
    return str((home.read(eid) or {}).get("state", "")).lower()


def _closed(home: SafeHome, house: dict) -> Optional[bool]:
    e = house.get("vacancy_entity")
    if not e:
        return None
    s = _state_of(home, e)
    return None if s in ("", "unknown", "unavailable") else s == str(house.get("vacancy_on_state", "on")).lower()


def tick(home: SafeHome, house: dict, model: dict, st: dict, now: datetime) -> tuple[dict, list[str], list[dict]]:
    """Evaluate plans, watches and triggers once. -> (new state, messages for the owner, log records)."""
    st = {"plans": list(st.get("plans", [])), "watches": list(st.get("watches", [])), "last": dict(st.get("last", {}))}
    msgs, recs = [], []
    shadow = not live(house)

    def rec(kind, **kw):
        recs.append({"ts": now.isoformat(), "kind": kind, "shadow": shadow, **kw})

    for p in st["plans"]:
        if p.get("status") not in ("pending", "heating"):
            continue
        deadline = datetime.fromisoformat(p["deadline"])
        cur = home.value(p["judge"])
        if p["status"] == "pending":
            start, need, why = start_time(p, cur, house, model)
            late = now > deadline + timedelta(minutes=LATE_START_MIN)
            if now >= start or late:
                r = run_step(home, house, {"zone": p["zone"], "mode": "heat", "setpoint": p["target_f"]})
                p.update(status="heating", started=now.isoformat(), start_reading=cur, start_ok=r["ok"])
                rec("plan_start", plan=p["id"], plan_kind=p["kind"], judge_f=cur, needed_h=round(need, 2), result=r["text"])
                if not r["ok"]:
                    msgs.append(f"House: the {p['kind']} warm-up could not start: {r['text']}")
        if p["status"] == "heating" and now >= deadline:
            results = run_steps(home, house, p.get("at_deadline"))
            p.update(status="done", end_reading=cur, finished=now.isoformat())
            rec("plan_end", plan=p["id"], plan_kind=p["kind"], judge_f=cur, target_f=p["target_f"],
                results=[x["text"] for x in results])
            bad = [x["text"] for x in results if not x["ok"]]
            if bad:
                msgs.append("House: " + "; ".join(bad))
    for w in st["watches"]:
        if w.get("status") == "done":
            continue
        if now > datetime.fromisoformat(w["until"]):
            w["status"] = "done"
            if w["kind"] == "lights_when_free":
                rec("lights_left_on", reason="the room was still in use when the watch ended")
            if w["kind"] == "overnight":
                rec("overnight", sensor=w["sensor"], label=w.get("label"), min_f=w.get("min"), max_f=w.get("max"))
            continue
        if w["kind"] == "overnight":
            v = home.value(w["sensor"])
            if v is not None:
                w["min"] = v if w.get("min") is None else min(w["min"], v)
                w["max"] = v if w.get("max") is None else max(w["max"], v)
        if w["kind"] == "lights_when_free":
            busy = [b["entity"] for b in w.get("busy", []) if _state_of(home, b["entity"]) in
                    {x.lower() for x in (b.get("states") or BUSY_STATES)}]
            w["free_checks"] = 0 if busy else w.get("free_checks", 0) + 1
            if w["free_checks"] >= 2:
                results = run_steps(home, house, [{"switch": e, "on": False} for e in w["lights"]])
                w["status"] = "done"
                rec("lights_off", results=[x["text"] for x in results])
    # triggers: act on a change, never on a steady state
    fp = cfg(house).get("fireplace") or {}
    if fp:
        if fp.get("power_entity"):
            v = home.value(fp["power_entity"])
            on = None if v is None else v > float(fp.get("on_above_w", 15))
        else:
            s = _state_of(home, fp["fan_entity"])
            on = None if s in ("", "unknown", "unavailable") else s == "on"
        if on is not None:
            if on and st["last"].get("fireplace") is False:
                r = run_step(home, house, {"zone": fp["zone"], "mode": "heat", "setpoint": fp.get("setpoint", 64)})
                rec("fireplace_on", result=r["text"])
            st["last"]["fireplace"] = on
    comfort(home, house, st, now, rec)
    a = cfg(house).get("arrival") or {}
    for sig in a.get("signals") or []:
        s = _state_of(home, sig["entity"])
        if s in ("", "unknown", "unavailable"):
            continue
        prev = st["last"].get("sig:" + sig["entity"])
        st["last"]["sig:" + sig["entity"]] = s
        if prev is not None and prev != s and s == str(sig["state"]).lower() and _closed(home, house) is True:
            results, lines = arrival(home, house, sig.get("label") or f"{sig['entity']} {s}")
            rec("arrival", signal=sig["entity"], results=[x["text"] for x in results])
            msgs.append("House: " + lines[0] + " " + "; ".join(x["text"] for x in results if not x["ok"])
                        if any(not x["ok"] for x in results) else "House: " + lines[0] + " Outlets on.")
            break
    st["plans"] = [p for p in st["plans"] if p.get("status") in ("pending", "heating")
                   or now - datetime.fromisoformat(p["deadline"]) < timedelta(days=2)]
    st["watches"] = [w for w in st["watches"] if w.get("status") != "done"
                     or now - datetime.fromisoformat(w["until"]) < timedelta(days=2)]
    return st, ([] if shadow else msgs), recs


# ---- comfort: humidity and air movement while a room is being heated ------------------------------------------------
def comfort(home: SafeHome, house: dict, st: dict, now: datetime, rec) -> None:
    """While the room's unit is heating in heating season: humidifier on below `low_pct`, off above `high_pct`
    (a band, so it does not flap); the fan runs in periods (`fan_on_min` out of every `fan_every_min`), and every
    period is recorded with the room's temperature and humidity so the pattern can be judged and changed from data.
    Outside heating, or with the unit off, Merrick leaves both alone (and turns off only what he turned on)."""
    c = cfg(house).get("comfort") or {}
    if not c.get("zone"):
        return
    z = (house.get("zones") or {}).get(c["zone"]) or {}
    heating = _state_of(home, z.get("climate_entity", "")) == "heat" and heating_season(home, house)[0]
    last = st["last"]
    hum = home.value(c["humidity_entity"]) if c.get("humidity_entity") else None
    temp = home.value(z["room_sensor"]) if z.get("room_sensor") else None
    if c.get("humidifier"):
        want = None
        if heating and hum is not None:
            if hum < float(c.get("low_pct", 35)):
                want = True
            elif hum > float(c.get("high_pct", 45)):
                want = False
        elif not heating and last.get("humidifier_by_merrick"):
            want = False
        if want is not None and want != last.get("humidifier_on"):
            r = run_step(home, house, {"switch": c["humidifier"], "on": want})
            last["humidifier_on"], last["humidifier_by_merrick"] = want, want
            rec("humidifier", on=want, humidity=hum, result=r["text"])
    if c.get("fan"):
        on_min, every = float(c.get("fan_on_min", 15)), float(c.get("fan_every_min", 60))
        started = last.get("fan_started")
        running = bool(started) and now < datetime.fromisoformat(started) + timedelta(minutes=on_min)
        due = not started or now >= datetime.fromisoformat(started) + timedelta(minutes=every)
        if heating and due and not last.get("fan_on"):
            r = run_step(home, house, {"switch": c["fan"], "on": True})
            last.update(fan_on=True, fan_started=now.isoformat())
            rec("fan", on=True, room_f=temp, humidity=hum, result=r["text"])
        elif last.get("fan_on") and (not running or not heating):
            r = run_step(home, house, {"switch": c["fan"], "on": False})
            last["fan_on"] = False
            rec("fan", on=False, room_f=temp, humidity=hum, result=r["text"])


# ---- the night, reported from the record ---------------------------------------------------------------------------
def night_report(records: list[dict], house: dict, now: datetime) -> str:
    since = now - timedelta(hours=18)
    rs = [r for r in records if datetime.fromisoformat(r["ts"]) >= since]
    bed = [r for r in rs if r.get("kind") == "bedtime"]
    if not bed:
        return "No bedtime routine was run in the last 18 hours, so there is no night to report."
    z = tz(house)
    t = lambda s: datetime.fromisoformat(s).astimezone(z).strftime("%-I:%M %p").lower()   # noqa: E731
    b = bed[-1]
    lines = [f"Bedtime {t(b['ts'])}{' (shadow: nothing was changed)' if b.get('shadow') else ''}:"]
    lines += [f"- {x}" for x in b.get("results", [])]
    for r in rs:
        if datetime.fromisoformat(r["ts"]) < datetime.fromisoformat(b["ts"]):
            continue
        k = r.get("kind")
        if k == "overnight":
            lines.append(f"- {r.get('label', 'bedroom')} overnight: low {r['min_f']:.0f} F, high {r['max_f']:.0f} F"
                         if r.get("min_f") is not None else f"- {r.get('label', 'bedroom')}: no readings overnight")
        elif k == "lights_off":
            lines.append(f"- lights off at {t(r['ts'])}")
        elif k == "lights_left_on":
            lines.append("- lights left on: the room was in use until the watch ended")
        elif k == "plan_start" and r.get("plan_kind") == "wake":
            j = f"{r['judge_f']:.0f} F" if r.get("judge_f") is not None else "no reading"
            lines.append(f"- warm-up started {t(r['ts'])} (living area {j})")
        elif k == "plan_end" and r.get("plan_kind") == "wake":
            j = r.get("judge_f")
            verdict = "no reading" if j is None else ("on target" if j >= r["target_f"] - 0.5 else f"{r['target_f'] - j:.1f} F short")
            lines.append(f"- at wake {t(r['ts'])}: {('%.0f F' % j) if j is not None else 'unknown'} against {r['target_f']:g} F ({verdict})")
            lines += [f"- {x}" for x in r.get("results", [])]
    return "\n".join(lines)
