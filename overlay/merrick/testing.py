"""An in-memory Home Assistant for tests. No network. It behaves like the real thing in the ways that matter:
writes change state (unless told to ignore them, lag, or fail), and every call is recorded."""
from __future__ import annotations

import copy


class FakeHome:
    def __init__(self, states: dict | None = None):
        self.states = copy.deepcopy(states or {})
        self.calls: list[tuple] = []
        self.reads: list[str] = []
        self.ignore_writes = False      # Home Assistant accepts the call but nothing changes
        self.fail_writes = False        # the service call raises
        self.fail_reads = False         # Home Assistant unreachable
        self.lag_reads = 0              # number of reads after a write that still show the old state
        self._pending = None

    def get_state(self, entity_id):
        self.reads.append(entity_id)
        if self.fail_reads:
            raise ConnectionError("unreachable")
        if self._pending is not None:
            if self._pending[0] <= 0:
                self._apply(*self._pending[1])
                self._pending = None
            else:
                self._pending = (self._pending[0] - 1, self._pending[1])
        st = self.states.get(entity_id)
        return copy.deepcopy(st) if st is not None else None

    def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data)))
        if self.fail_writes:
            raise RuntimeError("service call failed")
        if self.ignore_writes:
            return
        if self.lag_reads:
            self._pending = (self.lag_reads, (domain, service, dict(data)))
        else:
            self._apply(domain, service, data)

    def _apply(self, domain, service, data):
        st = self.states.setdefault(data["entity_id"], {"state": "unknown", "attributes": {}})
        st.setdefault("attributes", {})
        if service == "set_temperature":
            st["attributes"]["temperature"] = data["temperature"]
        elif service == "set_hvac_mode":
            st["state"] = data["hvac_mode"]
        elif service in ("alarm_arm_away", "alarm_arm_home"):
            st["state"] = "armed_away" if service == "alarm_arm_away" else "armed_home"
        elif service in ("turn_on", "turn_off", "lock", "unlock", "open_valve", "close_valve"):
            st["state"] = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked",
                           "open_valve": "open", "close_valve": "closed"}[service]


def climate(state="heat", target=62.0, current=61.0, action="idle") -> dict:
    return {"state": state, "attributes": {"temperature": target, "current_temperature": current, "hvac_action": action}}


def sensor(value) -> dict:
    return {"state": str(value), "attributes": {}}


HOUSE = {
    "temperature_unit": "F", "default_zone": "main", "away_hold_f": 62, "weather_entity": "weather.home",
    "vacancy_entity": "input_boolean.house_closed", "vacancy_on_state": "on",
    "zones": {
        "main": {"name": "Main house", "climate_entity": "climate.main_house",
                 "aliases": ["main house", "house", "downstairs", "heat"], "prior_heat_f_per_hour": 1.4,
                 "thermal_lag_minutes": 20},
        "family_room": {"name": "Family room", "climate_entity": "climate.family_room",
                        "room_sensor": "sensor.family_room_wall_temperature", "aliases": ["family room", "den"],
                        "prior_heat_f_per_hour": 2.0, "thermal_lag_minutes": 0},
    },
    "sensors": {"basement": {"entity": "sensor.basement_temperature", "aliases": ["basement", "cellar"]}},
    "perimeter": {"front door": "binary_sensor.front_door", "kitchen window": "binary_sensor.kitchen_window"},
    "security_read": {"front door lock": "lock.front_door"},
    "controllable": ["switch.fireplace_blower"],
    "protected_entities": ["switch.main_supply"],
    "unsupported_location_words": ["city flat"],
    "cold_watch": {"basement_sensor": "basement", "zone": "main"},
}


def states() -> dict:
    return {
        "climate.main_house": climate("heat", 62.0, 61.0, "idle"),
        "climate.family_room": climate("heat", 64.0, 71.5, "heating"),   # its own thermistor reads high
        "sensor.family_room_wall_temperature": sensor(63.2),
        "sensor.basement_temperature": sensor(52.0),
        "binary_sensor.front_door": {"state": "off", "attributes": {}},
        "binary_sensor.kitchen_window": {"state": "off", "attributes": {}},
        "lock.front_door": {"state": "locked", "attributes": {}},
        "alarm_control_panel.house": {"state": "disarmed", "attributes": {}},
        "switch.main_water_valve": {"state": "on", "attributes": {}},
        "switch.main_supply": {"state": "on", "attributes": {}},
        "switch.fireplace_blower": {"state": "off", "attributes": {}},
        "switch.porch_light": {"state": "off", "attributes": {}},
        "input_boolean.house_closed": {"state": "on", "attributes": {}},
        "weather.home": {"state": "cloudy", "attributes": {"temperature": 38.0}},
    }
