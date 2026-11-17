#!/usr/bin/env python3
"""Build Merrick's house file from a Potter v2 export (the owner's entity ids are personal: never stored in the repo).

Reads `.../workspace/merrick/data/house_entities.json` (the default location's zones, doors and windows, locks,
alarm, valves) plus the entity names v2's cold watcher used, and writes the v3 house file. Decisions baked in:
  - a zone's room sensor is used only when v2 marked it as an independent sensor (the thermostat's own thermistor
    is never a stand-in);
  - valves, pumps and leak devices become protected (never operated);
  - locks and the first alarm panel go under `departure` (lock/arm only after the owner confirms);
  - the away hold is the data-driven default (62 normal, 64 when the basement data says so).

  house_from_v2.py EXPORT.zip OUT.json
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
AWAY_HOLD = {"normal_f": 62, "cold_f": 64, "raise_when_basement_below_f": 40, "lower_when_basement_above_f": 43,
             "raise_when_hours_to_freeze_below": 24}


def _ent(v) -> str | None:
    return v if isinstance(v, str) and ENTITY.match(v) else None


def convert(v2: dict, script_text: str = "") -> dict:
    locs = (v2 or {}).get("locations") or {}
    loc = next((v for v in locs.values() if isinstance(v, dict) and v.get("default")), None) \
        or next((v for v in locs.values() if isinstance(v, dict) and v.get("zones")), None)
    if not loc:
        raise ValueError("no location with zones in the v2 house data")
    zones, controllable = {}, []
    for zid, z in (loc.get("zones") or {}).items():
        if not isinstance(z, dict) or not _ent(z.get("climate_entity")) or not re.fullmatch(r"[a-z0-9_]+", zid):
            continue
        out = {"name": str(z.get("name") or zid)[:60], "climate_entity": z["climate_entity"],
               "aliases": [a for a in (z.get("aliases") or []) if isinstance(a, str)][:12]}
        if str(z.get("temp_source_type", "")).startswith("independent") and _ent(z.get("temp_sensor")):
            out["room_sensor"] = z["temp_sensor"]
        zones[zid] = out
        for k, v in z.items():
            if k.endswith("_switch") and _ent(v) and v.startswith("switch."):
                controllable.append(v)
    if not zones:
        raise ValueError("no usable zones in the v2 house data")
    default_zone = next(iter(zones))
    perimeter = {k.replace("_", " "): v for k, v in (loc.get("doors_windows") or {}).items()
                 if _ent(v) and v.startswith("binary_sensor.") and "group" not in k}
    sec = loc.get("security_and_locks") or {}
    locks = [v for v in sec.values() if _ent(v) and v.startswith("lock.")]
    alarms = [v for v in sec.values() if _ent(v) and v.startswith("alarm_control_panel.")]
    protected = [v for group in ("valves_and_pumps", "leak_sensors") for v in (loc.get(group) or {}).values() if _ent(v)]
    house = {"temperature_unit": "F", "default_zone": default_zone, "away_hold": dict(AWAY_HOLD),
             "zones": zones, "sensors": {}, "perimeter": perimeter,
             "security_read": {k.replace("_", " "): v for k, v in sec.items() if _ent(v)},
             "controllable": sorted(set(controllable)), "protected_entities": sorted(set(protected)),
             "cold_watch": {"basement_sensor": "basement", "zone": default_zone}}
    if _ent(loc.get("weather_entity")):
        house["weather_entity"] = loc["weather_entity"]
    m = re.search(r"sensor\.[a-z0-9_]*basement[a-z0-9_]*temperature", script_text or "")
    if m:
        house["sensors"]["basement"] = {"entity": m.group(0), "aliases": ["basement", "cellar"]}
    v = re.search(r"input_boolean\.[a-z0-9_]*(closed|vacan|away)[a-z0-9_]*", script_text or "")
    if v:
        house["vacancy_entity"], house["vacancy_on_state"] = v.group(0), "on"
    if locks or alarms:
        house["departure"] = {"locks": locks, **({"alarm": alarms[0]} if alarms else {}), "arm_mode": "arm_away"}
    others = [str(v.get("name") or k) for k, v in locs.items() if isinstance(v, dict) and v is not loc]
    house["unsupported_location_words"] = [w for k, v in locs.items() if isinstance(v, dict) and v is not loc
                                           for w in ([k] + [a for a in (v.get("aliases") or []) if isinstance(a, str)])][:20]
    house["_missing"] = [x for x, ok in (("basement sensor", "basement" in house["sensors"]),
                                         ("vacancy switch", "vacancy_entity" in house)) if not ok]
    return house


def from_zip(zpath: Path) -> dict:
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        hit = [n for n in names if n.endswith("/merrick/data/house_entities.json")]
        if not hit:
            raise ValueError("the export has no merrick/data/house_entities.json")
        base = hit[0][: -len("data/house_entities.json")]
        scripts = "\n".join(z.read(n).decode("utf-8", "ignore") for n in names
                            if n.startswith(base + "scripts/") and n.endswith(".py"))
        return convert(json.loads(z.read(hit[0])), scripts)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: house_from_v2.py EXPORT.zip OUT.json", file=sys.stderr)
        return 2
    try:
        house = from_zip(Path(argv[0]))
    except (ValueError, zipfile.BadZipFile, KeyError) as e:
        print(f"house file not built: {e}", file=sys.stderr)
        return 1
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(house, indent=1), encoding="utf-8")
    print(json.dumps({"zones": len(house["zones"]), "perimeter": len(house["perimeter"]),
                      "locks": len((house.get("departure") or {}).get("locks", [])), "missing": house["_missing"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
