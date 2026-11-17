"""Cold-weather watcher: a pure function over sensor readings. Nothing wrong -> no findings -> nothing is said.

Thresholds are v2's (set by the owner, 2026-11-14): the basement is a problem below 40 F and urgent below 36 F;
the main zone should sit on its vacancy hold while the house is empty; the main zone reading below 58 F means the
heat is not keeping up. Added from v2's own protocol notes: a modelled freeze trajectory under 24 hours.

  evaluate(readings, thresholds, previous=None) -> [finding, ...]
  readings: {"ts": epoch seconds, "basement_f", "zone_target_f", "zone_current_f", "zone_action", "vacant"}
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .thermal import hours_to_freeze, slope_f_per_hour


@dataclass(frozen=True)
class Thresholds:
    basement_alert_f: float = 40.0
    basement_urgent_f: float = 36.0
    vacancy_hold_f: Optional[float] = 62.0      # None = do not check the hold
    hold_tolerance_f: float = 0.6
    zone_min_current_f: float = 58.0
    freeze_hours_alert: float = 24.0
    max_slope_gap_s: float = 6 * 3600.0         # an older previous sample is not used for a slope

    @classmethod
    def from_house(cls, house: dict) -> "Thresholds":
        cfg = dict((house or {}).get("cold_watch") or {})
        known = {k: cfg[k] for k in cls.__dataclass_fields__ if k in cfg}
        if "vacancy_hold_f" not in known and (house or {}).get("away_hold_f") is not None:
            known["vacancy_hold_f"] = house["away_hold_f"]
        return cls(**known)


def _n(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def evaluate(readings: dict, th: Thresholds = Thresholds(), previous: Optional[dict] = None) -> list[dict]:
    """Return findings: {kind, severity ("urgent" | "alert" | "unreadable"), label, text}. Empty list = all well.

    An unreadable basement sensor is a finding of its own (severity "unreadable"): the caller logs it and fails
    the job so the schedule watchdog reports it, rather than staying quiet and looking healthy.
    """
    r = readings or {}
    out = []
    basement, target, current = _n(r.get("basement_f")), _n(r.get("zone_target_f")), _n(r.get("zone_current_f"))
    if basement is None:
        out.append({"kind": "basement_unreadable", "severity": "unreadable", "label": "unknown",
                    "text": "the basement temperature could not be read"})
    elif basement < th.basement_urgent_f:
        out.append({"kind": "basement_cold", "severity": "urgent", "label": "observed",
                    "text": f"URGENT: basement {basement:.1f} F (below {th.basement_urgent_f:.0f} F)"})
    elif basement < th.basement_alert_f:
        out.append({"kind": "basement_cold", "severity": "alert", "label": "observed",
                    "text": f"basement {basement:.1f} F (below the {th.basement_alert_f:.0f} F line)"})
    if basement is not None and previous and _n(previous.get("basement_f")) is not None \
            and _n(previous.get("ts")) is not None and _n(r.get("ts")) is not None \
            and r["ts"] - previous["ts"] <= th.max_slope_gap_s:
        slope = slope_f_per_hour(float(previous["basement_f"]), float(previous["ts"]), basement, float(r["ts"]))
        hrs = hours_to_freeze(basement, slope)
        if hrs is not None and hrs < th.freeze_hours_alert:
            out.append({"kind": "freeze_trajectory", "severity": "alert", "label": "estimated",
                        "text": f"basement falling {abs(slope):.1f} F per hour; at this rate it reaches freezing in "
                                f"about {hrs:.0f} hours"})
    # The hold is checked when the house is known to be empty, and also when occupancy is unknown (as v2 did).
    # It is skipped only when the house is known to be occupied: a person changing the heat is not drift.
    if th.vacancy_hold_f is not None and target is not None and r.get("vacant") is not False \
            and abs(target - th.vacancy_hold_f) > th.hold_tolerance_f:
        out.append({"kind": "hold_drift", "severity": "alert", "label": "observed",
                    "text": f"main zone target is {target:.0f} F (the vacancy hold should be {th.vacancy_hold_f:.0f} F)"})
    if current is not None and current < th.zone_min_current_f:
        action = r.get("zone_action") or "state unknown"
        out.append({"kind": "zone_underheat", "severity": "alert", "label": "observed",
                    "text": f"main zone reads {current:.1f} F (below {th.zone_min_current_f:.0f} F; heating: {action})"})
    return out


def reportable(findings: list[dict]) -> list[dict]:
    """Findings the owner should be told about (an unreadable sensor is a job failure, not a chat message)."""
    return [f for f in findings if f["severity"] in ("urgent", "alert")]


def new_findings(findings: list[dict], seen: dict, now: float, repeat_after_s: float = 12 * 3600.0) -> tuple[list, dict]:
    """Report a problem when it appears or gets worse, then again only every `repeat_after_s` while it lasts.
    A problem that clears drops out of `seen`, so it alerts again if it returns."""
    report, nxt = [], {}
    for f in reportable(findings):
        sig = f"{f['kind']}:{f['severity']}"
        old = seen.get(f["kind"]) or {}
        if old.get("sig") != sig or now - float(old.get("at", 0)) >= repeat_after_s:
            report.append(f)
            nxt[f["kind"]] = {"sig": sig, "at": now}
        else:
            nxt[f["kind"]] = old
    return report, nxt


def render(findings: list[dict], readings: dict) -> str:
    """Plain text for the owner. Empty string when there is nothing to report (silence is the result)."""
    fs = reportable(findings)
    if not fs:
        return ""
    r = readings or {}
    detail = []
    if _n(r.get("basement_f")) is not None:
        detail.append(f"basement {float(r['basement_f']):.1f} F")
    if _n(r.get("zone_target_f")) is not None:
        cur = _n(r.get("zone_current_f"))
        detail.append(f"main zone target {float(r['zone_target_f']):.0f} F, reads "
                      f"{'unknown' if cur is None else f'{cur:.0f} F'}")
    lines = ["Cold-weather watch" + (": " + "; ".join(detail) if detail else "")]
    lines += [f"- {f['text']} ({f['label']})" for f in fs]
    return "\n".join(lines)
