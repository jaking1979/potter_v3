"""The vacancy (away) hold, decided from data rather than fixed.

Owner's rule (2026-11-17): 62 F is the normal hold; on very cold days it goes up to 64 F, and the house's own
readings decide which. The numbers live in the house file, so they can be re-derived as more data is collected:

  "away_hold": {"normal_f": 62, "cold_f": 64,
                "raise_when_basement_below_f": 40,      # v2's baseline protocol: pipe-risk line
                "lower_when_basement_above_f": 43,      # hysteresis, so the hold does not flap
                "raise_when_hours_to_freeze_below": 24}

A bare number (`"away_hold_f": 62`) still works and means "always that number".
Missing data never changes the level: if the basement cannot be read, the hold stays where it is.
"""
from __future__ import annotations

from typing import Optional

TOL_F = 0.6


def config(house: dict) -> Optional[dict]:
    h = (house or {}).get("away_hold")
    if isinstance(h, dict) and h.get("normal_f") is not None:
        normal = float(h["normal_f"])
        cold = float(h.get("cold_f", normal))
        raise_below = h.get("raise_when_basement_below_f")
        lower_above = h.get("lower_when_basement_above_f", raise_below)
        return {"normal_f": normal, "cold_f": max(cold, normal),
                "raise_below": None if raise_below is None else float(raise_below),
                "lower_above": None if lower_above is None else float(lower_above),
                "freeze_hours": h.get("raise_when_hours_to_freeze_below")}
    if (house or {}).get("away_hold_f") is not None:
        n = float(house["away_hold_f"])
        return {"normal_f": n, "cold_f": n, "raise_below": None, "lower_above": None, "freeze_hours": None}
    return None


def decide(house: dict, basement_f: Optional[float] = None, hours_to_freeze: Optional[float] = None,
           current_target_f: Optional[float] = None) -> Optional[dict]:
    """-> {"target_f", "level": "normal"|"cold", "label": "observed"|"estimated"|"unknown", "reason"} or None."""
    c = config(house)
    if c is None:
        return None
    was_cold = (c["cold_f"] > c["normal_f"] and current_target_f is not None
                and abs(current_target_f - c["cold_f"]) <= TOL_F)

    def out(level, label, reason):
        return {"target_f": c["cold_f"] if level == "cold" else c["normal_f"], "level": level,
                "label": label, "reason": reason}

    if c["cold_f"] == c["normal_f"] or c["raise_below"] is None:
        return out("normal", "observed", "one hold value is configured")
    if c["freeze_hours"] is not None and hours_to_freeze is not None and hours_to_freeze < float(c["freeze_hours"]):
        return out("cold", "estimated", f"the basement is on course to reach freezing in about {hours_to_freeze:.0f} hours")
    if basement_f is None:
        return out("cold" if was_cold else "normal", "unknown",
                   "the basement could not be read, so the hold is left where it is")
    if basement_f < c["raise_below"]:
        return out("cold", "observed", f"basement {basement_f:.1f} F is below {c['raise_below']:.0f} F")
    if was_cold and basement_f < c["lower_above"]:
        return out("cold", "observed",
                   f"basement {basement_f:.1f} F has not yet recovered above {c['lower_above']:.0f} F")
    return out("normal", "observed", f"basement {basement_f:.1f} F is at or above the cold line")
