"""Thermal arithmetic carried over from v2, as pure functions (no files, no network, no clock).

Carried over because it is understood and tested: heating-time estimate, the running calibration of a zone's
warming and cooling rate, the split of history into heating / carry-over / cooling episodes, dew point, and
hours until a space reaches freezing. See README.md for what was changed and what was not ported.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

ESTABLISHED_SAMPLES = 10          # v2's line between "calibrating" and "established"
MIN_SEGMENT_HOURS = 0.2           # v2 ignored anything shorter than 12 minutes
MIN_CALIBRATION_HOURS = 0.5       # v3: a rate from a shorter stretch is mostly sensor rounding


def heating_estimate(start_f: float, target_f: float, outdoor_f: Optional[float], rate_f_per_hour: Optional[float],
                     samples: int = 0, lag_minutes: float = 20.0) -> dict:
    """Hours to warm a zone from start to target. Always an ESTIMATE; `rate_basis` says what the rate rests on.

    v2's rule is kept: below 30 F outdoors the zone warms 15% slower, above 45 F 15% faster, and the range is the
    nominal rate x1.25 (fast) to x0.80 (slow), plus the thermal lag. If the outdoor temperature is unknown no
    adjustment is made and that is stated (v2 silently assumed 40-45 F).
    """
    if rate_f_per_hour is None or rate_f_per_hour <= 0:
        return {"label": "unknown", "statement": "No warming rate is known for this zone yet, so no estimate."}
    delta = target_f - start_f
    if delta <= 0:
        return {"label": "observed", "nominal_hours": 0.0, "range_hours": [0.0, 0.0],
                "statement": f"Already at or above {target_f:g} F (reads {start_f:g} F)."}
    rate = rate_f_per_hour * (0.85 if outdoor_f is not None and outdoor_f < 30 else
                              1.15 if outdoor_f is not None and outdoor_f > 45 else 1.0)
    lag = lag_minutes / 60.0
    nominal, low, high = delta / rate + lag, delta / (rate * 1.25) + lag, delta / (rate * 0.80) + lag
    basis = "observed" if samples >= ESTABLISHED_SAMPLES else "calibrating" if samples > 0 else "prior"
    outdoor = f"{outdoor_f:g} F outdoors" if outdoor_f is not None else "outdoor temperature unknown (no adjustment made)"
    return {"label": "estimated", "delta_f": round(delta, 1), "rate_f_per_hour": round(rate, 2),
            "nominal_hours": round(nominal, 2), "range_hours": [round(low, 1), round(high, 1)],
            "samples": samples, "rate_basis": basis,
            "statement": f"Warming from {start_f:g} F to {target_f:g} F should take {round(low, 1)}-{round(high, 1)} "
                         f"hours (nominal {round(nominal, 1)} h); {outdoor}; rate based on "
                         + (f"{samples} recorded heating episodes." if samples else "a starting assumption, not on measurements.")}


def update_rate(mean: Optional[float], samples: int, new_rates: list) -> tuple[Optional[float], int, str]:
    """Fold new observed rates into a running mean. Returns (mean, sample count, confidence).

    With zero prior samples the prior is replaced outright, exactly as v2 did. (v2 called this a median; it is a
    mean, and is named as one here.)
    """
    good = [abs(float(r)) for r in new_rates if r is not None and not math.isnan(float(r))]
    if not good:
        return mean, samples, _confidence(samples)
    total = samples + len(good)
    new_mean = (float(mean or 0.0) * samples + sum(good)) / total
    return round(new_mean, 2), total, _confidence(total)


def _confidence(samples: int) -> str:
    return "established" if samples >= ESTABLISHED_SAMPLES else "calibrating" if samples > 0 else "prior"


def _when(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_episodes(climate_history: list, temp_history: list, now: datetime) -> list[dict]:
    """Split a zone's history into episodes, one per stretch between thermostat state changes.

    active_heating     the thermostat reported hvac_action 'heating'
    thermal_carryover  not heating, the previous stretch was heating, and the room still rose more than 0.3 F
    passive_cooling    everything else
    """
    temps = []
    for row in temp_history or []:
        t, v = _when((row or {}).get("last_updated")), (row or {}).get("state")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if t is not None and not math.isnan(v):
            temps.append((t, v))
    temps.sort()
    marks = [(t, r) for r in (climate_history or []) if (t := _when((r or {}).get("last_updated"))) is not None]
    marks.sort(key=lambda m: m[0])
    out, prev_action = [], None
    if not temps:
        return out
    for i, (start, row) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else now
        action = ((row.get("attributes") or {}).get("hvac_action")) or "idle"
        hours = (end - start).total_seconds() / 3600.0
        if hours < MIN_SEGMENT_HOURS:
            prev_action = action
            continue
        at_start = [v for t, v in temps if t <= start]
        at_end = [v for t, v in temps if t <= end]
        if not at_start or not at_end:              # v2 borrowed the nearest reading; v3 does not invent a start point
            prev_action = action
            continue
        delta = at_end[-1] - at_start[-1]
        kind = "active_heating" if action == "heating" else \
            "thermal_carryover" if delta > 0.3 and prev_action == "heating" else "passive_cooling"
        prev_action = action
        out.append({"type": kind, "start_time": start.isoformat(), "end_time": end.isoformat(),
                    "duration_hours": round(hours, 2), "start_temp": round(at_start[-1], 1),
                    "end_temp": round(at_end[-1], 1), "delta_temp": round(delta, 2),
                    "rate_f_per_hour": round(delta / hours, 2), "hvac_action": action, "mode": row.get("state")})
    return out


def calibration_rates(episodes: list) -> tuple[list, list]:
    """(warming rates, cooling rates) that are fit to learn from.

    v2's learner looked for episode types "heating" and "cooling", which its own collector never produced
    ("active_heating" / "passive_cooling"), so the hourly collector never calibrated anything. Fixed here.
    """
    heat = [e["rate_f_per_hour"] for e in episodes if e.get("type") == "active_heating"
            and e.get("duration_hours", 0) >= MIN_CALIBRATION_HOURS and e.get("rate_f_per_hour", 0) > 0.1]
    cool = [abs(e["rate_f_per_hour"]) for e in episodes if e.get("type") == "passive_cooling"
            and e.get("duration_hours", 0) >= MIN_CALIBRATION_HOURS and e.get("rate_f_per_hour", 0) < -0.05]
    return heat, cool


def dew_point_f(temp_f: Optional[float], rel_humidity: Optional[float]) -> Optional[float]:
    """Magnus-Tetens dew point, in F."""
    if temp_f is None or rel_humidity is None or rel_humidity <= 0:
        return None
    c = (temp_f - 32.0) * 5.0 / 9.0
    a, b = 17.27, 237.7
    alpha = (a * c) / (b + c) + math.log(min(rel_humidity, 100.0) / 100.0)
    return round((b * alpha) / (a - alpha) * 9.0 / 5.0 + 32.0, 1)


def slope_f_per_hour(earlier_f: float, earlier_ts: float, later_f: float, later_ts: float,
                     min_gap_s: float = 2700.0) -> Optional[float]:
    """Temperature change per hour between two samples, or None if they are under 45 minutes apart (v2's rule:
    a slope from closer samples is noise)."""
    gap = later_ts - earlier_ts
    return None if gap < min_gap_s else (later_f - earlier_f) / (gap / 3600.0)


def hours_to_freeze(current_f: Optional[float], slope: Optional[float], threshold_f: float = 32.0) -> Optional[float]:
    """Hours until a space reaches the threshold at the present rate of fall. None when steady, warming or unknown."""
    if current_f is None or slope is None or slope >= 0:
        return None
    return 0.0 if current_f <= threshold_f else round((current_f - threshold_f) / abs(slope), 1)
