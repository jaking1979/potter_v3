"""Home Assistant control with the safety rules built in, not written in a prompt.

The real client is injected (anything with get_state and call_service), so tests use a fake and nothing here
touches the network. Rules enforced on every write:

  * fail closed: locks, alarms, water valves and anything not on the house file's allowlist are refused;
  * climate setpoints are clamped to 50-74 F, and the clamp is reported;
  * read-back is mandatory: a result is ok only when the state read back from Home Assistant matches.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Protocol

MIN_SETPOINT_F = 50.0
MAX_SETPOINT_F = 74.0
WRITE_DOMAINS = frozenset({"climate", "switch", "fan", "light", "input_boolean"})
PROTECTED_DOMAINS = frozenset({"lock", "alarm_control_panel", "valve", "siren", "cover", "automation", "script"})
# A water valve is often a plain switch in Home Assistant, so the name is checked as well as the domain.
_PROTECTED_NAME = re.compile(r"lock|alarm|valve|water|siren|garage|security|shut_?off|(?<!heat_)pump|leak")
HVAC_MODES = frozenset({"off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"})
_MISSING = frozenset({None, "", "unknown", "unavailable", "none"})


class HomeAssistant(Protocol):
    def get_state(self, entity_id: str) -> Optional[dict]:
        """Return {"state": str, "attributes": {...}, "last_updated": str} or None when the entity does not exist."""

    def call_service(self, domain: str, service: str, data: dict) -> Any:
        """Call a Home Assistant service. Raises on transport or API failure."""


@dataclass
class ActionResult:
    ok: bool                 # True ONLY when the read-back matched
    outcome: str             # "verified" | "unverified" | "refused" | "error"
    entity_id: str
    message: str
    requested: Any = None
    applied: Any = None      # what was sent after clamping
    observed: Any = None     # what Home Assistant reported afterwards
    clamped: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def clamp_setpoint(value: Any) -> tuple[float, bool]:
    """Return (setpoint within 50-74 F, whether it had to be changed). Raises ValueError on a non-number."""
    v = float(value)
    if math.isnan(v) or math.isinf(v):
        raise ValueError("not a temperature")
    c = min(MAX_SETPOINT_F, max(MIN_SETPOINT_F, v))
    return c, c != v


def num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def allowed_entities(house: dict) -> set[str]:
    out = {z.get("climate_entity") for z in (house.get("zones") or {}).values() if z.get("climate_entity")}
    out |= set(house.get("controllable") or [])
    return out


def refusal(entity_id: Any, house: dict) -> Optional[str]:
    """Why this entity may not be written to, or None when it may."""
    if not isinstance(entity_id, str) or entity_id.count(".") != 1:
        return "not a Home Assistant entity id"
    eid = entity_id.lower()
    domain, name = eid.split(".")
    if domain in PROTECTED_DOMAINS or _PROTECTED_NAME.search(name) or \
            eid in {str(e).lower() for e in house.get("protected_entities") or []}:
        return "protected (locks, alarms and water valves are never operated by Merrick)"
    if domain not in WRITE_DOMAINS:
        return f"the '{domain}' domain is not one Merrick may control"
    if eid not in {e.lower() for e in allowed_entities(house)}:
        return "not on the house file's list of controllable devices"
    return None


class SafeHome:
    def __init__(self, client: HomeAssistant, house: dict, sleep: Callable[[float], None] = time.sleep,
                 attempts: int = 3, wait_s: float = 1.0):
        self.client, self.house, self.sleep = client, house or {}, sleep
        self.attempts, self.wait_s = max(1, attempts), wait_s

    # ---- reads -------------------------------------------------------------------------------------------
    def read(self, entity_id: str) -> Optional[dict]:
        """Live state, or None when the entity is missing or Home Assistant cannot be reached. Never guesses."""
        try:
            st = self.client.get_state(entity_id)
        except Exception:
            return None
        return st if isinstance(st, dict) else None

    def value(self, entity_id: str) -> Optional[float]:
        st = self.read(entity_id)
        return None if st is None or str(st.get("state")).lower() in _MISSING else num(st.get("state"))

    def zone_reading(self, zone_id: str) -> dict:
        """Room temperature, setpoint and activity for a zone.

        The room temperature comes from the sensor the owner's smart-home rules name as authoritative for that
        room (the house file's room_sensor). If that sensor has no reading the answer is None: the thermostat's
        own thermistor is never used as a stand-in.
        """
        z = (self.house.get("zones") or {}).get(zone_id) or {}
        st = self.read(z["climate_entity"]) if z.get("climate_entity") else None
        attrs = (st or {}).get("attributes") or {}
        if z.get("room_sensor"):
            room, source = self.value(z["room_sensor"]), "room sensor"
        else:
            room, source = num(attrs.get("current_temperature")), "thermostat's own sensor"
        return {"zone": zone_id, "name": z.get("name") or zone_id, "room_f": room, "room_source": source,
                "setpoint_f": num(attrs.get("temperature")), "mode": (st or {}).get("state"),
                "action": attrs.get("hvac_action"), "reachable": st is not None}

    # ---- writes ------------------------------------------------------------------------------------------
    def set_temperature(self, entity_id: str, temperature: Any) -> ActionResult:
        why = refusal(entity_id, self.house)
        if why is None and not str(entity_id).lower().startswith("climate."):
            why = "not a climate device"
        if why is None and str(self.house.get("temperature_unit", "F")).upper() != "F":
            why = "the house file is not in Fahrenheit, so the 50-74 F limit cannot be applied"
        if why:
            return ActionResult(False, "refused", str(entity_id), f"Refused: {why}.", requested=temperature)
        try:
            target, clamped = clamp_setpoint(temperature)
        except (TypeError, ValueError):
            return ActionResult(False, "refused", entity_id, "Refused: that is not a temperature.", requested=temperature)
        return self._write(entity_id, "climate", "set_temperature", {"temperature": target},
                           lambda st: num(((st or {}).get("attributes") or {}).get("temperature")),
                           lambda seen: seen is not None and abs(seen - target) <= 0.5,
                           requested=temperature, applied=target, clamped=clamped)

    def set_hvac_mode(self, entity_id: str, mode: str) -> ActionResult:
        why = refusal(entity_id, self.house)
        if why is None and not str(entity_id).lower().startswith("climate."):
            why = "not a climate device"
        if why is None and mode not in HVAC_MODES:
            why = f"'{mode}' is not a known mode"
        if why:
            return ActionResult(False, "refused", str(entity_id), f"Refused: {why}.", requested=mode)
        return self._write(entity_id, "climate", "set_hvac_mode", {"hvac_mode": mode},
                           lambda st: (st or {}).get("state"), lambda seen: seen == mode,
                           requested=mode, applied=mode)

    def switch(self, entity_id: str, on: bool) -> ActionResult:
        why = refusal(entity_id, self.house)
        if why is None and str(entity_id).lower().startswith("climate."):
            why = "use a mode or a setpoint for climate devices"
        if why:
            return ActionResult(False, "refused", str(entity_id), f"Refused: {why}.", requested="on" if on else "off")
        want = "on" if on else "off"
        return self._write(entity_id, entity_id.split(".")[0], "turn_on" if on else "turn_off", {},
                           lambda st: (st or {}).get("state"), lambda seen: seen == want,
                           requested=want, applied=want)

    def _write(self, entity_id, domain, service, data, extract, matches, **kw) -> ActionResult:
        if self.read(entity_id) is None:                       # the device must exist before anything is sent
            return ActionResult(False, "error", entity_id,
                                f"Not done: {entity_id} could not be read from Home Assistant.", **kw)
        try:
            self.client.call_service(domain, service, {"entity_id": entity_id, **data})
        except Exception as e:
            return ActionResult(False, "error", entity_id,
                                f"Not done: Home Assistant rejected the request ({type(e).__name__}).", **kw)
        seen = None
        for i in range(self.attempts):                         # Home Assistant can take a moment to show a change
            seen = extract(self.read(entity_id))
            if matches(seen):
                return ActionResult(True, "verified", entity_id,
                                    f"Confirmed: {entity_id} now reads {_show(seen)}.", observed=seen, **kw)
            if i + 1 < self.attempts:
                self.sleep(self.wait_s)
        return ActionResult(False, "unverified", entity_id,
                            f"NOT confirmed: asked for {_show(kw.get('applied'))}, but Home Assistant reports "
                            f"{_show(seen)}.", observed=seen, **kw)


def _show(v: Any) -> str:
    if v is None:
        return "no value"
    return f"{v:g} F" if isinstance(v, (int, float)) else str(v)
