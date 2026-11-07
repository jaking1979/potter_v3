"""E1 tables, created next to C2/C3 in the same SQLite file (one Database).

* ``notifier_state``    - key/value: the global kill flag and primary-channel
                          health. Survives restarts.
* ``approval_requests`` - one row per approval prompt (action ID). Records
                          issuance in C2's database so each request is
                          answered at most once (single use), and stores the
                          exact canonical BoundAction it was raised for.
* ``notifier_sending``  - sends past the kill gate and not yet returned
                          (the /stop in-flight boundary, see service.py).
* ``inbound_log``       - commands that were ignored (non-allowlisted or
                          unauthenticated senders). Sender reference and
                          reason only; message text is never stored.

``meta.notifier_schema`` pins the layout; a different value fails closed.
E1 also reads/updates C3's ``outbox`` table directly for three operations C3
has no API for (fallback scan, outage deferral, /stop sweep); it refuses to run
against any outbox schema other than ``c3.v0``.
"""
from __future__ import annotations

import sqlite3

from runtime import Database, JournalUnavailable, Outbox

NOTIFIER_SCHEMA_VERSION = "e1.v0"
EXPECTED_OUTBOX_SCHEMA = "c3.v0"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS notifier_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS approval_requests (
        approval_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        action_hash TEXT NOT NULL,
        action_canonical TEXT NOT NULL,
        capability TEXT NOT NULL,
        screenshot_ref TEXT,
        notification_id TEXT NOT NULL,
        requested_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('pending','approved','denied','expired','voided','cancelled')),
        nonce TEXT UNIQUE,
        decided_at INTEGER,
        decided_channel TEXT,
        status_reason TEXT,
        record TEXT)""",
    "CREATE INDEX IF NOT EXISTS approval_requests_task ON approval_requests (task_id, status)",
    # One row per send that has passed the kill gate and not yet returned.
    # Inserted in the same BEGIN IMMEDIATE transaction as the kill check, so
    # stop() (which sets the flag under the same write lock) sees every send
    # that got past the gate and waits for it before returning.
    """CREATE TABLE IF NOT EXISTS notifier_sending (
        message_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        since REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS inbound_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        channel TEXT NOT NULL,
        sender_ref TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT NOT NULL)""",
)


def ensure(db: Database) -> None:
    Outbox(db)   # idempotent: creates C3's table + meta.outbox_schema if absent
    try:
        with db.tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt)
            c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('notifier_schema', ?)",
                      (NOTIFIER_SCHEMA_VERSION,))
            got = c.execute("SELECT value FROM meta WHERE key='notifier_schema'").fetchone()[0]
            if got != NOTIFIER_SCHEMA_VERSION:
                raise JournalUnavailable(
                    f"notifier schema {got!r}, expected {NOTIFIER_SCHEMA_VERSION!r}")
            row = c.execute("SELECT value FROM meta WHERE key='outbox_schema'").fetchone()
            if row is None or row[0] != EXPECTED_OUTBOX_SCHEMA:
                raise JournalUnavailable("notifier needs the C3 outbox schema c3.v0")
    except sqlite3.Error as exc:
        raise JournalUnavailable(f"notifier tables unusable: {exc}") from exc


def get_state(c: sqlite3.Connection, key: str) -> str | None:
    row = c.execute("SELECT value FROM notifier_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO notifier_state (key, value) VALUES (?,?)"
              " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def del_state(c: sqlite3.Connection, key: str) -> None:
    c.execute("DELETE FROM notifier_state WHERE key=?", (key,))
