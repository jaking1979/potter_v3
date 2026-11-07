"""Synthetic-only ModelBroker: no network, secrets, prompts or runtime wiring.

Money is integer microdollars. Store records across process restarts; callers
must provide authoritative task IDs, periods and cost estimates in production.
This prototype does not establish their provenance or enforce provider costs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    model: str
    provider: str
    approved: bool
    validated_tasks: frozenset[str]
    privacy_classes: frozenset[str]
    expected_usd_micros: int
    worst_usd_micros: int
    available: bool = True


@dataclass(frozen=True)
class Request:
    call_id: str
    task_id: str
    task_class: str
    privacy_class: str
    day: str
    month: str


@dataclass(frozen=True)
class Caps:
    request: int
    task: int
    day: int
    month: int


class Broker:
    """Select and reserve atomically in SQLite. Each instance uses its own connection."""

    def __init__(self, path: str | Path, caps: Caps):
        if any(type(x) is not int or x < 0 for x in vars(caps).values()):
            raise ValueError("caps must be nonnegative integer microdollars")
        self.path, self.caps = str(path), caps
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS broker_calls (
                call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                task_class TEXT NOT NULL, privacy_class TEXT NOT NULL,
                day TEXT NOT NULL, month TEXT NOT NULL, model TEXT NOT NULL,
                provider TEXT NOT NULL, expected INTEGER NOT NULL,
                worst INTEGER NOT NULL, actual INTEGER,
                state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','settled','overrun'))
            )""")

    def _db(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def reserve(self, req: Request, candidates: list[Candidate]):
        if not all(isinstance(v, str) and v for v in vars(req).values()):
            raise Refused("invalid_request")
        eligible = []
        for c in candidates:
            if (c.approved is True and c.available is True and req.task_class in c.validated_tasks
                and req.privacy_class in c.privacy_classes and c.model and c.provider
                and type(c.expected_usd_micros) is int and type(c.worst_usd_micros) is int
                and 0 <= c.expected_usd_micros <= c.worst_usd_micros):
                eligible.append(c)
        eligible.sort(key=lambda x: (x.expected_usd_micros, x.worst_usd_micros, x.model, x.provider))
        if not eligible:
            raise Refused("no_eligible_candidate")
        # No implicit price escalation: if the cheapest eligible candidate
        # exceeds any ceiling, refuse rather than picking a dearer route.
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM broker_calls WHERE state='overrun' LIMIT 1").fetchone():
                raise Refused("unresolved_cost_variance")
            if db.execute("SELECT 1 FROM broker_calls WHERE call_id=?", (req.call_id,)).fetchone():
                raise Refused("duplicate_call_id")
            # Actual for settled calls; worst-case reservation for every other
            # state, including crashes after dispatch. Never expire on restart.
            def total(where, value):
                row = db.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual "
                                 "ELSE worst END),0) FROM broker_calls WHERE " + where + "=?", (value,)).fetchone()
                return row[0]
            task_used = total("task_id", req.task_id)
            day_used = total("day", req.day)
            month_used = total("month", req.month)
            c = eligible[0]
            w = c.worst_usd_micros
            if not (w <= self.caps.request and task_used + w <= self.caps.task
                    and day_used + w <= self.caps.day and month_used + w <= self.caps.month):
                raise Refused("budget_ceiling")
            db.execute("INSERT INTO broker_calls VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'reserved')",
                       (req.call_id, req.task_id, req.task_class, req.privacy_class,
                        req.day, req.month, c.model, c.provider, c.expected_usd_micros, w))
            db.commit()
            return c

    def dispatch_once(self, call_id: str):
        """Record the point after which a charge may exist; do not retry blindly."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE broker_calls SET state='dispatched' WHERE call_id=? AND state='reserved'", (call_id,)).rowcount
            if changed != 1:
                raise Refused("not_reserved")
            db.commit()

    def settle(self, call_id: str, actual_usd_micros: int):
        if type(actual_usd_micros) is not int or actual_usd_micros < 0:
            raise Refused("unknown_or_invalid_cost")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,worst FROM broker_calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None or row[0] != "dispatched":
                raise Refused("not_dispatched")
            state = "overrun" if actual_usd_micros > row[1] else "settled"
            db.execute("UPDATE broker_calls SET state=?,actual=? WHERE call_id=?", (state,actual_usd_micros,call_id))
            db.commit()
            if state == "overrun":
                raise Refused("cost_variance")

    def status(self, call_id: str):
        with self._db() as db:
            return db.execute("SELECT model,provider,worst,actual,state FROM broker_calls WHERE call_id=?", (call_id,)).fetchone()
