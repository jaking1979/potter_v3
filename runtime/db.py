"""One SQLite file holds the task store, the effect journal and the approval
nonces, so an approval consumption and its pending effect intent are written in
ONE transaction (C0 "single use without stranding" gate).

Durability: WAL journal with ``synchronous=FULL``, so every COMMIT is fsynced
to the WAL before it returns. Every write runs under ``BEGIN IMMEDIATE`` (one
writer at a time across threads and processes; readers are not blocked).
Each operation opens its own short-lived connection, so a Database object is
safe to share between threads and cheap to rebuild after a restart.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "c2.v2"

# In-place upgrades of an existing journal, keyed by the stored version.
# Upgraded rows carry no contract digest, so they are never trusted for a
# re-dispatch after an ambiguous outcome (they need a fence or reconciliation).
MIGRATIONS = {
    "c2.v0": ("ALTER TABLE effects ADD COLUMN connector TEXT",
              "ALTER TABLE effects ADD COLUMN connector_digest TEXT"),
    "c2.v1": ("ALTER TABLE effects ADD COLUMN connector_digest TEXT",),
}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS jobs (
        name TEXT PRIMARY KEY,
        cadence_s REAL NOT NULL CHECK (cadence_s > 0),
        tolerance REAL NOT NULL CHECK (tolerance >= 1),
        registered_at REAL NOT NULL,
        last_success_at REAL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        escalated_at REAL)""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        trigger_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        job TEXT REFERENCES jobs(name),
        payload TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('queued','running','waiting_approval','done','failed','escalated')),
        attempts INTEGER NOT NULL DEFAULT 0,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        lease_owner TEXT,
        lease_expires_at REAL,
        checkpoint TEXT,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS tasks_claimable ON tasks (state, created_at)",
    """CREATE TABLE IF NOT EXISTS effects (
        idempotency_key TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        effect_id TEXT NOT NULL,
        action_hash TEXT NOT NULL,
        action_canonical TEXT NOT NULL,
        approval_id TEXT,
        status TEXT NOT NULL CHECK (status IN ('pending','unknown','succeeded','failed')),
        dispatches INTEGER NOT NULL DEFAULT 0,
        in_flight_owner TEXT,
        in_flight_until REAL,
        result TEXT,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        connector TEXT,
        connector_digest TEXT,
        UNIQUE (task_id, effect_id))""",
    "CREATE INDEX IF NOT EXISTS effects_status ON effects (status)",
    """CREATE TABLE IF NOT EXISTS approval_nonces (
        nonce TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE REFERENCES effects(idempotency_key),
        consumed_at REAL NOT NULL)""",
)


class JournalUnavailable(RuntimeError):
    """The store cannot guarantee its durability contract; callers fail closed."""


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_s: float = 30.0):
        self.path = str(path)
        self._timeout = busy_timeout_s
        try:
            c = self._connect()
            try:
                mode = c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise JournalUnavailable(f"journal_mode is {mode!r}, need WAL")
            finally:
                c.close()
            with self.tx() as c:
                for stmt in SCHEMA:
                    c.execute(stmt)
                c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                          (SCHEMA_VERSION,))
                got = c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
                if got in MIGRATIONS:
                    for stmt in MIGRATIONS[got]:
                        c.execute(stmt)
                    c.execute("UPDATE meta SET value=? WHERE key='schema'", (SCHEMA_VERSION,))
                    got = SCHEMA_VERSION
                if got != SCHEMA_VERSION:
                    raise JournalUnavailable(f"schema {got!r}, expected {SCHEMA_VERSION!r}")
        except sqlite3.Error as exc:
            raise JournalUnavailable(f"journal unusable: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; committed (and fsynced) only if the block
        completes, rolled back on any exception."""
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        finally:
            c.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            yield c
        finally:
            c.close()


def as_database(db: "Database | str | Path") -> Database:
    return db if isinstance(db, Database) else Database(db)
