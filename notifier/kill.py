"""Global kill flag (T19 PASS 2 item 6). Durable: lives in the shared SQLite
file, so every process (notifier relay, approval issuer, C8 workers) sees the
same value. Setting it is the only thing ``/stop`` needs to make true; the
consumers check it before every side effect.

Clearing is a deliberate operator act (``clear``); nothing clears it
automatically, and no inbound command clears it.
"""
from __future__ import annotations

import json
import sqlite3

from runtime import Database

from . import _schema

KEY = "kill_flag"


class KillFlag:
    def __init__(self, db: Database):
        self.db = db
        _schema.ensure(db)

    def is_set(self, c: sqlite3.Connection | None = None) -> bool:
        if c is not None:
            return _schema.get_state(c, KEY) is not None
        with self.db.read() as rc:
            return _schema.get_state(rc, KEY) is not None

    def info(self) -> dict | None:
        with self.db.read() as c:
            raw = _schema.get_state(c, KEY)
        return json.loads(raw) if raw else None

    def set(self, c: sqlite3.Connection, *, by: str, at: float) -> bool:
        """Set inside the caller's transaction. Returns False if already set."""
        if _schema.get_state(c, KEY) is not None:
            return False
        _schema.set_state(c, KEY, json.dumps({"by": by, "at": at}, sort_keys=True))
        return True

    def clear(self) -> None:
        with self.db.tx() as c:
            _schema.del_state(c, KEY)
