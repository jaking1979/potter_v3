"""E1 tables, created next to C2/C3 in the same SQLite file (one Database).

* ``notifier_state``    - key/value: the global kill flag and primary-channel
                          health. Survives restarts.
* ``approval_requests`` - one row per approval prompt (action ID). Records
                          issuance in C2's database so each request is
                          answered at most once (single use), and stores the
                          exact canonical BoundAction it was raised for.
* ``notifier_sending``  - durable in-flight record per (message, dispatch):
                          ``gated`` -> ``dispatching`` (flipped atomically with
                          the final kill check) -> removed only when the
                          outcome is known; an unknown outcome stays as
                          ``unknown`` until the outbox resolves the message.
                          Never removed because of age or an expired lease.
* ``inbound_seen``      - (channel, update_id) of every authentic adapter
                          update, with a sha256 digest of the exact signed
                          envelope (never its body) and a status:
                          ``pending`` until the command's effect commits
                          (marked ``processed`` in the SAME transaction),
                          so a crash between authentication and effect is
                          resumed on redelivery, and a processed update is
                          never executed twice. Its signed ``sent_at`` drives
                          pruning (against the transport high-water mark).
* ``notifier_state['hw:<channel>']`` - transport high-water mark: max
                          accepted ``sent_at`` and max observed clock.
* ``inbound_log``       - commands that were ignored (non-allowlisted or
                          unauthenticated senders). Sender reference and
                          reason only; message text is never stored.

``meta.notifier_schema`` pins the layout (``e1.v1``). ``ensure()`` migrates an
``e1.v0`` database (layout as of 225e7df, detected column-for-column) inside
ONE ``BEGIN IMMEDIATE`` transaction, and refuses anything else (an unknown or
future version, or an ``e1.v0`` database whose tables don't match) before any
table is created or any traffic is routed. Migration of ``inbound_seen``
(which had no ``sent_at``): envelope times are never invented.
* old ``processed`` rows stay as burned replay records: ``sent_at`` is set to
  the migration time (their floor) and ``migration_flag='sent_at_unknown'``;
  they can never become replayable.
* old ``pending`` rows are NOT auto-recovered: status ``needs_operator``, no
  effect, listed by ``ChannelAuth.needs_operator()``; an operator may only
  burn them (``ChannelAuth.burn_needs_operator``), never execute them.
* the high-water mark is seeded (``hw:__migration__``, applied to every
  transport) from max(now, latest ``processed_at``), so any pre-migration
  envelope whose row is gone is below the floor.
A crash mid-migration rolls the whole transaction back: the database is
still ``e1.v0`` and the next start migrates it cleanly.
E1 also reads/updates C3's ``outbox`` table directly for three operations C3
has no API for (fallback scan, outage deferral, /stop sweep); it refuses to run
against any outbox schema other than ``c3.v0``.
"""
from __future__ import annotations

import sqlite3

from runtime import Database, JournalUnavailable, Outbox

NOTIFIER_SCHEMA_VERSION = "e1.v1"
PREVIOUS_SCHEMA_VERSION = "e1.v0"          # layout as of 225e7df; migrated by ensure()
SEED_KEY = "hw:__migration__"              # high-water seeded at migration (all transports)
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
        message_id TEXT NOT NULL,
        dispatch INTEGER NOT NULL,
        owner TEXT NOT NULL,
        since REAL NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('gated','dispatching','unknown')),
        dispatched_at REAL,
        PRIMARY KEY (message_id, dispatch))""",
    # Transport replay protection: each adapter update is accepted once.
    """CREATE TABLE IF NOT EXISTS inbound_seen (
        channel TEXT NOT NULL,
        update_id TEXT NOT NULL,
        at REAL NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','processed','needs_operator')),
        principal TEXT,
        envelope_digest TEXT NOT NULL,
        sent_at REAL NOT NULL,
        processed_at REAL,
        migration_flag TEXT CHECK (migration_flag IS NULL OR migration_flag='sent_at_unknown'),
        PRIMARY KEY (channel, update_id))""",
    """CREATE TABLE IF NOT EXISTS inbound_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        channel TEXT NOT NULL,
        sender_ref TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT NOT NULL)""",
)


V0_COLUMNS = {
    "inbound_seen": ["channel", "update_id", "at", "status", "principal", "envelope_digest",
                     "processed_at"],
    "notifier_sending": ["message_id", "dispatch", "owner", "since", "status",
                         "dispatched_at"],
    "notifier_state": ["key", "value"],
    "inbound_log": ["seq", "at", "channel", "sender_ref", "outcome", "reason"],
}
V1_INBOUND_COLUMNS = ["channel", "update_id", "at", "status", "principal", "envelope_digest",
                      "sent_at", "processed_at", "migration_flag"]

# Test seam: called inside the migration transaction after the table rebuild
# and before the version bump (simulates a crash mid-migration).
_MIGRATION_FAULT = None


def _columns(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _migrate_v0(c: sqlite3.Connection, now: float) -> None:
    for table, cols in V0_COLUMNS.items():
        got = _columns(c, table)
        if got and got != cols:
            raise JournalUnavailable(f"e1.v0 table {table} has an unrecognised layout; refusing")
    if not _columns(c, "inbound_seen"):
        raise JournalUnavailable("e1.v0 database without inbound_seen; refusing")
    latest = c.execute("SELECT MAX(processed_at) FROM inbound_seen").fetchone()[0]
    seed = max(now, latest) if latest is not None else now
    c.execute("ALTER TABLE inbound_seen RENAME TO inbound_seen_v0")
    for stmt in SCHEMA:
        if "inbound_seen" in stmt:
            c.execute(stmt)
    c.execute("INSERT INTO inbound_seen (channel, update_id, at, status, principal,"
              " envelope_digest, sent_at, processed_at, migration_flag)"
              " SELECT channel, update_id, at,"
              " CASE status WHEN 'pending' THEN 'needs_operator' ELSE 'processed' END,"
              " principal, envelope_digest, ?, processed_at, 'sent_at_unknown'"
              " FROM inbound_seen_v0", (seed,))
    c.execute("DROP TABLE inbound_seen_v0")
    import json
    c.execute("INSERT INTO notifier_state (key, value) VALUES (?,?)"
              " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (SEED_KEY, json.dumps({"max_sent_at": seed, "max_clock": seed}, sort_keys=True)))
    if _MIGRATION_FAULT is not None:
        _MIGRATION_FAULT()
    c.execute("UPDATE meta SET value=? WHERE key='notifier_schema'", (NOTIFIER_SCHEMA_VERSION,))


def ensure(db: Database, *, clock=None) -> None:
    import time as _time
    Outbox(db)   # idempotent: creates C3's table + meta.outbox_schema if absent
    try:
        with db.tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key='notifier_schema'").fetchone()
            version = row[0] if row else None
            if version not in (None, PREVIOUS_SCHEMA_VERSION, NOTIFIER_SCHEMA_VERSION):
                raise JournalUnavailable(
                    f"notifier schema {version!r} is not supported (expected"
                    f" {NOTIFIER_SCHEMA_VERSION!r}); refusing")
            if version == PREVIOUS_SCHEMA_VERSION:
                _migrate_v0(c, (clock or _time.time)())
            for stmt in SCHEMA:
                c.execute(stmt)
            if version is None:
                c.execute("INSERT INTO meta (key, value) VALUES ('notifier_schema', ?)",
                          (NOTIFIER_SCHEMA_VERSION,))
            got = c.execute("SELECT value FROM meta WHERE key='notifier_schema'").fetchone()[0]
            if got != NOTIFIER_SCHEMA_VERSION:
                raise JournalUnavailable(
                    f"notifier schema {got!r}, expected {NOTIFIER_SCHEMA_VERSION!r}")
            if _columns(c, "inbound_seen") != V1_INBOUND_COLUMNS:
                raise JournalUnavailable("inbound_seen layout does not match e1.v1; refusing")
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
