"""The fast path: route a request, and if it is a recognised intent, do it with plain code and verified calls.

  handle(text, home, house, log_path, decide=None, model=None) -> {"path", "intent", "outcome", "reply", "id", ...}

Every call appends exactly one line to the path log. A request that is not recognised returns path "fallback"
with the reason; the caller (the Merrick agent) then works under a time budget and logs how it ended.

Outcomes: ok (read done, or action confirmed by read-back) | unverified | refused | error | not_home | escalated
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from . import coldwatch, pathlog
from .ha import SafeHome, num
from .router import Route, route
from .thermal import heating_estimate

OPEN_STATES = {"on", "open", "unlocked", "opening"}


def _f(v) -> str:
    return "unknown" if v is None else f"{v:.1f} F"


def _zone_line(z: dict) -> str:
    if not z["reachable"]:
        return f"{z['name']}: Home Assistant did not return this zone (unknown)."
    return (f"{z['name']}: room {_f(z['room_f'])} ({z['room_source']}), set to {_f(z['setpoint_f'])}, "
            f"mode {z['mode'] or 'unknown'}, {z['action'] or 'activity unknown'}.")


def _zone_entity(house: dict, zone: Optional[str]) -> Optional[str]:
    return ((house.get("zones") or {}).get(zone or "") or {}).get("climate_entity")


def perimeter(home: SafeHome, house: dict) -> dict:
    """Read every door and window. Unreadable entries are listed as unknown, never assumed closed."""
    open_, unknown, closed = [], [], []
    for name, eid in (house.get("perimeter") or {}).items():
        st = home.read(eid)
        s = str((st or {}).get("state", "")).lower()
        if st is None or s in ("", "unknown", "unavailable"):
            unknown.append(name)
        elif s in OPEN_STATES:
            open_.append(name)
        else:
            closed.append(name)
    return {"open": open_, "unknown": unknown, "closed": closed}


def _perimeter_text(p: dict) -> str:
    total = len(p["open"]) + len(p["unknown"]) + len(p["closed"])
    if not total:
        return "Unknown: no doors or windows are listed in the house file."
    parts = []
    if p["open"]:
        parts.append("OPEN: " + ", ".join(p["open"]) + ".")
    if p["unknown"]:
        parts.append("Unknown (no reading): " + ", ".join(p["unknown"]) + ".")
    if not p["open"] and not p["unknown"]:
        parts.append(f"All {total} doors and windows read closed.")
    elif p["closed"]:
        parts.append(f"{len(p['closed'])} others read closed.")
    return "Observed: " + " ".join(parts)


def cold_readings(home: SafeHome, house: dict, now_ts: Optional[float] = None) -> dict:
    """Gather what the cold-weather watcher needs, per the house file."""
    cw = house.get("cold_watch") or {}
    sensor = ((house.get("sensors") or {}).get(cw.get("basement_sensor", "basement")) or {}).get("entity")
    st = home.read(_zone_entity(house, cw.get("zone") or house.get("default_zone")) or "") or {}
    attrs = st.get("attributes") or {}
    vacant = None
    if house.get("vacancy_entity"):
        v = home.read(house["vacancy_entity"])
        if v is not None and str(v.get("state")).lower() not in ("unknown", "unavailable", ""):
            vacant = str(v.get("state")).lower() == str(house.get("vacancy_on_state", "on")).lower()
    return {"ts": time.time() if now_ts is None else now_ts,
            "basement_f": home.value(sensor) if sensor else None,
            "zone_target_f": num(attrs.get("temperature")), "zone_current_f": num(attrs.get("current_temperature")),
            "zone_action": attrs.get("hvac_action"), "vacant": vacant}


def _act(res, what: str) -> tuple[str, str]:
    note = f" (you asked for {res.requested:g} F; the allowed range is 50-74 F)" if res.clamped else ""
    if res.ok:
        return "ok", f"Done: {what}{note}. {res.message}"
    return res.outcome, f"{what}{note}: {res.message}"


def _execute(r: Route, home: SafeHome, house: dict, model: dict) -> tuple[str, str]:
    zone = r.slots.get("zone")
    entity = _zone_entity(house, zone)
    zname = ((house.get("zones") or {}).get(zone or "") or {}).get("name") or zone
    assumed = "" if r.slots.get("zone_named", True) else " (no zone was named, so the default zone was used)"
    if r.intent == "not_home":
        return "not_home", ""
    if r.intent == "refuse_protected":
        return "refused", ("Refused: locks, alarms, water valves, doors and windows are never operated by Merrick. "
                           "The owner does these directly in Home Assistant.")
    if r.intent == "check_perimeter":
        text = _perimeter_text(perimeter(home, house))
        for name, eid in (house.get("security_read") or {}).items():
            st = home.read(eid)
            text += f" {name}: {(st or {}).get('state', 'unknown')}."
        return "ok", text
    if r.intent == "close_up":
        p = perimeter(home, house)
        stop = "Departure stopped: something is open or unreadable. " if p["open"] or p["unknown"] else ""
        return "refused", (f"{stop}{_perimeter_text(p)} Locking and arming are not done by Merrick: "
                           "the owner does these directly in Home Assistant.")
    if r.intent == "house_status":
        lines = ["Observed: " + _zone_line(home.zone_reading(z)) for z in (house.get("zones") or {})]
        for name, s in (house.get("sensors") or {}).items():
            lines.append(f"Observed: {name} {_f(home.value(s.get('entity', '')))}.")
        lines.append(_perimeter_text(perimeter(home, house)))
        readings = cold_readings(home, house)
        lines += [f"Attention: {f['text']} ({f['label']})."
                  for f in coldwatch.reportable(coldwatch.evaluate(readings, coldwatch.Thresholds.from_house(house)))]
        return "ok", "\n".join(lines)
    if r.intent == "check_temperature":
        if r.slots.get("sensor"):
            s = (house.get("sensors") or {})[r.slots["sensor"]]
            v = home.value(s.get("entity", ""))
            return "ok", (f"Observed: {r.slots['sensor']} {_f(v)}." if v is not None
                          else f"Unknown: the {r.slots['sensor']} sensor returned no reading.")
        z = home.zone_reading(zone)
        return "ok", ("Observed: " if z["reachable"] else "Unknown: ") + _zone_line(z) + assumed
    if entity is None:
        return "error", "Not done: that zone has no climate device in the house file."
    if r.intent == "set_temperature":
        return _act(home.set_temperature(entity, r.slots["temperature"]), f"{zname} setpoint{assumed}")
    if r.intent == "turn_off":
        return _act(home.set_hvac_mode(entity, "off"), f"{zname} heating and cooling off{assumed}")
    if r.intent == "hold_away":
        return _act(home.set_temperature(entity, house["away_hold_f"]), f"{zname} away hold{assumed}")
    if r.intent == "heating_estimate":
        z = home.zone_reading(zone)
        if z["room_f"] is None:
            return "ok", f"Unknown: {zname} has no room temperature reading, so no estimate was made."
        w = home.read(house.get("weather_entity") or "") or {}
        outdoor = num((w.get("attributes") or {}).get("temperature"))
        learned = ((model.get("zones") or {}).get(zone) or {}).get("heating") or {}
        cfg = (house.get("zones") or {}).get(zone) or {}
        rate = learned.get("mean_f_per_hour") if learned.get("samples") else cfg.get("prior_heat_f_per_hour")
        est = heating_estimate(z["room_f"], float(r.slots["temperature"]), outdoor, rate,
                               int(learned.get("samples") or 0), float(cfg.get("thermal_lag_minutes", 20)))
        return "ok", (f"Observed: {zname} room {_f(z['room_f'])}{assumed}. "
                      f"{est['label'].capitalize()}: {est['statement']}")
    return "error", f"No handler for intent {r.intent}."


def handle(text: str, home: SafeHome, house: dict, log_path, decide: Optional[Callable] = None,
           model: Optional[dict] = None, clock: Callable[[], float] = time.monotonic) -> dict:
    t0 = clock()
    rid = pathlog.new_id()
    r = route(text, house, decide)
    if r.path == "fallback":
        outcome, reply = "escalated", ""
    else:
        try:
            outcome, reply = _execute(r, home, house, model or {})
        except Exception as e:                       # noqa: BLE001 - a broken handler must be visible, not fatal
            outcome, reply = "error", f"Not done: the fast path failed ({type(e).__name__}). Nothing is assumed."
    ms = (clock() - t0) * 1000.0
    logged = pathlog.append(log_path, pathlog.record(r.intent, r.path, ms, outcome, request=text, source=r.source,
                                                     reason=r.reason, request_id=rid))
    return {"id": rid, "path": r.path, "intent": r.intent, "source": r.source, "reason": r.reason,
            "outcome": outcome, "reply": reply, "ms": int(round(ms)), "logged": logged, "slots": r.slots}
