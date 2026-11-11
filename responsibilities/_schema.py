"""E2 tables, created in the shared C2/C3/E1 SQLite file.

* ``responsibility_state``    - key/value: the schedule watermark per
                                responsibility (latest slot submitted).
* ``responsibility_approvals`` - the one E1 approval request issued for each
                                (task, action hash). Written in the same
                                transaction as ``ApprovalIssuer.request``; the
                                primary key forbids a second one.
* ``responsibility_progress`` - per task: when its first T16 state was
                                reported (the long-work clock, so the 30 s ack
                                survives a restart). ``history`` is unused
                                (kept for layout compatibility): what was told
                                lives only in E1's durable outbox.

``meta.responsibilities_schema`` pins the layout; any other value fails closed.
"""
from __future__ import annotations

import json
import sqlite3

from runtime import Database, JournalUnavailable

SCHEMA_VERSION = "e2.v1"
# In-place upgrades, keyed by the stored version.
MIGRATIONS = {"e2.v0": ()}   # v1 only adds responsibility_approvals (CREATE IF NOT EXISTS)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS responsibility_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    # One E1 approval request per (task, action), ever: the find-or-create is
    # decided inside the same BEGIN IMMEDIATE transaction as the E1 insert,
    # and this primary key makes a second claim for the same action impossible.
    """CREATE TABLE IF NOT EXISTS responsibility_approvals (
        task_id TEXT NOT NULL,
        action_hash TEXT NOT NULL,
        approval_id TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        PRIMARY KEY (task_id, action_hash))""",
    """CREATE TABLE IF NOT EXISTS responsibility_progress (
        task_id TEXT PRIMARY KEY,
        started_at REAL NOT NULL,
        history TEXT NOT NULL)""",
)


def ensure(db: Database) -> None:
    try:
        with db.tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt)
            c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES"
                      " ('responsibilities_schema', ?)", (SCHEMA_VERSION,))
            got = c.execute("SELECT value FROM meta WHERE key='responsibilities_schema'"
                            ).fetchone()[0]
            if got in MIGRATIONS:
                for stmt in MIGRATIONS[got]:
                    c.execute(stmt)
                c.execute("UPDATE meta SET value=? WHERE key='responsibilities_schema'",
                          (SCHEMA_VERSION,))
                got = SCHEMA_VERSION
            if got != SCHEMA_VERSION:
                raise JournalUnavailable(f"responsibilities schema {got!r},"
                                         f" expected {SCHEMA_VERSION!r}")
    except sqlite3.Error as exc:
        raise JournalUnavailable(f"responsibility tables unusable: {exc}") from exc


def get_state(c: sqlite3.Connection, key: str) -> str | None:
    row = c.execute("SELECT value FROM responsibility_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO responsibility_state (key, value) VALUES (?,?)"
              " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def load_progress(c: sqlite3.Connection, task_id: str) -> tuple[float, list] | None:
    row = c.execute("SELECT started_at, history FROM responsibility_progress WHERE task_id=?",
                    (task_id,)).fetchone()
    return (row[0], json.loads(row[1])) if row else None


def save_progress(c: sqlite3.Connection, task_id: str, started_at: float, history: list) -> None:
    c.execute("INSERT INTO responsibility_progress (task_id, started_at, history) VALUES (?,?,?)"
              " ON CONFLICT(task_id) DO UPDATE SET history=excluded.history",
              (task_id, started_at, json.dumps(history, separators=(",", ":"))))
