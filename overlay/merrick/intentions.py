"""Temporary intentions: "set the heat to 68 at 5 pm" without creating a Home Assistant automation.

An intention is a one-shot. It runs once when due and then retires for good: there is no path that re-arms it.
A period-bound instruction ("for the weekend") is two one-shots made together: the change now, and the return
at the end of the period. The return is marked must_run so that a late wake-up still puts the house back.

One recurring job calls tick() every few minutes (never one scheduled job per intention).

Statuses: pending -> completed (state read back and matched) | failed (could not be confirmed after retries)
                  | expired (missed its window; only for intentions that are not must_run) | cancelled
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .ha import SafeHome

LATE_WINDOW = timedelta(minutes=15)       # v2: an ordinary intention more than 15 minutes late is not run
MAX_ATTEMPTS = 3
ACTIONS = ("set_temperature", "set_hvac_mode", "switch")


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("times must carry a time zone")
    return d


def register(store: dict, description: str, due: str, action: dict, now: datetime, must_run: bool = False,
             group: Optional[str] = None) -> dict:
    if action.get("kind") not in ACTIONS or not action.get("entity_id"):
        raise ValueError("unknown action")
    _dt(due)
    item = {"id": uuid.uuid4().hex[:8], "description": description, "due": due, "action": dict(action),
            "status": "pending", "must_run": bool(must_run), "attempts": 0, "created_at": now.isoformat()}
    if group:
        item["group"] = group
    store.setdefault("intentions", []).append(item)
    return item


def register_period(store: dict, description: str, start: str, end: str, action: dict, revert: dict,
                    now: datetime) -> tuple[dict, dict]:
    """A period-bound instruction: apply at `start`, undo at `end`. The undo always runs, however late."""
    if _dt(end) <= _dt(start):
        raise ValueError("the period must end after it starts")
    group = uuid.uuid4().hex[:8]
    a = register(store, description, start, action, now, group=group)
    b = register(store, f"end of: {description}", end, revert, now, must_run=True, group=group)
    return a, b


def cancel(store: dict, intention_id: str) -> bool:
    for it in store.get("intentions", []):
        if it.get("id") == intention_id and it.get("status") == "pending":
            it["status"] = "cancelled"
            return True
    return False


def _execute(home: SafeHome, action: dict):
    kind = action["kind"]
    if kind == "set_temperature":
        res = home.set_temperature(action["entity_id"], action.get("temperature"))
        if res.ok and action.get("hvac_mode"):
            res = home.set_hvac_mode(action["entity_id"], action["hvac_mode"])
        return res
    if kind == "set_hvac_mode":
        return home.set_hvac_mode(action["entity_id"], action.get("hvac_mode"))
    return home.switch(action["entity_id"], bool(action.get("on")))


def tick(store: dict, home: SafeHome, now: datetime) -> list[dict]:
    """Run whatever is due. Returns one event per intention that changed: {id, description, status, detail}.
    Nothing due -> empty list -> the job says nothing."""
    events = []
    for it in store.get("intentions", []):
        if it.get("status") != "pending":
            continue                                          # retired intentions never run again
        try:
            due = _dt(it["due"])
        except (KeyError, ValueError):
            it["status"], it["detail"] = "failed", "unreadable due time"
            events.append(_event(it))
            continue
        if now < due:
            continue
        if now - due > LATE_WINDOW and not it.get("must_run") and not it.get("attempts"):
            it["status"] = "expired"
            it["detail"] = f"missed its time by {int((now - due).total_seconds() // 60)} minutes; not run"
            events.append(_event(it))
            continue
        res = _execute(home, it["action"])
        it["attempts"] = int(it.get("attempts") or 0) + 1
        it["detail"] = res.message
        if res.ok:
            it["status"], it["executed_at"] = "completed", now.isoformat()
            events.append(_event(it))
        elif res.outcome == "refused" or it["attempts"] >= MAX_ATTEMPTS:
            it["status"] = "failed"
            events.append(_event(it))
        # otherwise it stays pending and the next tick tries again
    return events


def _event(it: dict) -> dict:
    return {"id": it.get("id"), "description": it.get("description"), "status": it.get("status"),
            "detail": it.get("detail", "")}


def render(events: list[dict]) -> str:
    """What the owner is told. Completed one-shots are confirmed in one line; problems are spelled out."""
    lines = []
    for e in events:
        if e["status"] == "completed":
            lines.append(f"Done as asked: {e['description']}. {e['detail']}")
        else:
            lines.append(f"NOT done ({e['status']}): {e['description']}. {e['detail']}")
    return "\n".join(lines)


def prune(store: dict, now: datetime, keep_days: int = 30) -> None:
    """Drop retired intentions older than keep_days so the file does not grow for ever."""
    cutoff = now - timedelta(days=keep_days)

    def keep(it):
        if it.get("status") == "pending":
            return True
        try:
            return _dt(it.get("executed_at") or it.get("due")) >= cutoff
        except ValueError:
            return False
    store["intentions"] = [it for it in store.get("intentions", []) if keep(it)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
