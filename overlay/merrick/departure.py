"""Closing up the house: lock and arm ONLY after the owner confirms (owner's decision, 2026-11-17).

Two steps, both deterministic:
  prepare()  - reads every door and window. Anything open or unreadable stops the departure. If all is closed it
               returns a pending confirmation that expires in 10 minutes. Nothing is locked or armed yet.
  confirm()  - run only when the owner has explicitly said to go ahead. Refuses if there is no pending
               confirmation or it has expired, re-reads the perimeter, then locks and arms exactly the devices the
               house file lists under "departure", with read-back on each.

There is no unlock and no disarm anywhere in Merrick.
House file:  "departure": {"locks": ["lock.front_door"], "alarm": "alarm_control_panel.house", "arm_mode": "arm_away"}
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .ha import SafeHome

PENDING_TTL_S = 600
OPEN_STATES = {"on", "open", "unlocked", "opening"}


def devices(house: dict) -> dict:
    d = (house or {}).get("departure") or {}
    locks = [e for e in (d.get("locks") or []) if isinstance(e, str) and e.lower().startswith("lock.")]
    alarm = d.get("alarm") if isinstance(d.get("alarm"), str) and d["alarm"].lower().startswith("alarm_control_panel.") else None
    mode = d.get("arm_mode") if d.get("arm_mode") in ("arm_away", "arm_home") else "arm_away"
    return {"locks": locks, "alarm": alarm, "arm_mode": mode}


def assured(text: str, house: dict) -> list[str]:
    """Doors and windows the owner vouches for in this request ("the living room window is closed"). The owner's
    word stands in for a sensor that reads open or cannot be read (owner's decision, 2026-11-21)."""
    t = str(text or "").lower()
    out = []
    for name in ((house or {}).get("perimeter") or {}):
        n = re.escape(str(name).lower())
        if re.search(n + r"\W{0,3}(?:\w+\W+){0,3}?(is |are |'s )?(closed|shut|fine|ok|okay|good|locked)\b", t):
            out.append(name)
    return out


def perimeter(home: SafeHome, house: dict, trusted: Iterable[str] = ()) -> dict:
    open_, unknown, closed = [], [], []
    trusted = set(trusted or ())
    for name, eid in ((house or {}).get("perimeter") or {}).items():
        if name in trusted:
            closed.append(name)
            continue
        st = home.read(eid)
        s = str((st or {}).get("state", "")).lower()
        if st is None or s in ("", "unknown", "unavailable"):
            unknown.append(name)
        elif s in OPEN_STATES:
            open_.append(name)
        else:
            closed.append(name)
    return {"open": open_, "unknown": unknown, "closed": closed}


def _blockers(p: dict) -> str:
    bits = []
    if p["open"]:
        bits.append("open: " + ", ".join(p["open"]))
    if p["unknown"]:
        bits.append("could not be read: " + ", ".join(p["unknown"]))
    return "; ".join(bits)


def prepare(home: SafeHome, house: dict, now: float, trusted: Iterable[str] = ()) -> tuple[str, str, Optional[dict]]:
    """-> (status, text, pending). status: "stopped" | "not_configured" | "needs_confirmation"."""
    trusted = list(trusted or ())
    p = perimeter(home, house, trusted)
    if p["open"] or p["unknown"]:
        return "stopped", f"Departure stopped ({_blockers(p)}). Nothing was locked or armed.", None
    if not p["closed"]:
        return "stopped", ("Departure stopped: no doors or windows are listed in the house file, so none could be "
                           "checked. Nothing was locked or armed."), None
    d = devices(house)
    if not d["locks"] and not d["alarm"]:
        return "not_configured", ("Observed: every door and window reads closed. No lock or alarm is listed in the "
                                  "house file, so there is nothing for Merrick to lock or arm."), None
    what = []
    if d["locks"]:
        what.append(f"lock {len(d['locks'])} door lock(s)")
    if d["alarm"]:
        what.append("arm the alarm (" + ("away" if d["arm_mode"] == "arm_away" else "home") + ")")
    pending = {"created": now, "expires": now + PENDING_TTL_S, "locks": d["locks"], "alarm": d["alarm"],
               "arm_mode": d["arm_mode"], "trusted": trusted}
    word = f" ({', '.join(trusted)} on your word)" if trusted else ""
    return "needs_confirmation", (f"Observed: all {len(p['closed'])} doors and windows read closed{word}. Ready to "
                                  f"{' and '.join(what)}. Nothing is done yet: say \"confirm departure\" within 10 "
                                  "minutes to go ahead."), pending


def confirm(home: SafeHome, house: dict, pending: Optional[dict], now: float) -> tuple[str, str]:
    """-> (status, text). status: "refused" | "stopped" | "done" | "partial"."""
    if not isinstance(pending, dict) or not isinstance(pending.get("expires"), (int, float)):
        return "refused", "Refused: there is no departure waiting for confirmation. Ask to close up the house first."
    if now > pending["expires"]:
        return "refused", "Refused: that confirmation window has passed. Ask to close up the house again."
    p = perimeter(home, house, pending.get("trusted") or ())   # things may have changed since prepare()
    if p["open"] or p["unknown"] or not p["closed"]:
        return "stopped", f"Departure stopped ({_blockers(p) or 'no doors or windows listed'}). Nothing was locked or armed."
    d = devices(house)                                         # act on the house file, never on stored ids alone
    results = [home.lock(e, confirmed=True) for e in d["locks"]]
    if d["alarm"]:
        results.append(home.arm(d["alarm"], d["arm_mode"], confirmed=True))
    lines = [("Confirmed: " if r.ok else "NOT confirmed: ") + r.message for r in results]
    return ("done" if all(r.ok for r in results) else "partial"), "\n".join(lines)
