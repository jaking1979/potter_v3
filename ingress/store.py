"""Dedupe store + ingress log (SQLite) and the attachment store seam.

``ingress_seen`` holds one row per (channel, dedupe key): the event id, the
content digest, the minted event digest and the decision. It never stores
message text, sender references or parameters. The ingress log is the same
row: a fixed reason code per decision.

``ingress_inbox`` is the durable handoff: an accepted event's full local
record, ``pending`` until the runner acks a durable enqueue. Its text is
dropped on ack (``handed_off``) or by retention (``expired``).

Every decision runs in one ``BEGIN IMMEDIATE`` transaction: look up the key,
insert the dedupe row (and the inbox row), emit the C6 trace, commit. If the trace sink raises, the
transaction rolls back, so the re-delivery is processed afresh.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from .events import sha256

SCHEMA_VERSION = "e3.v0"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS ingress_seen (
        dedupe_key TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        event_id TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        event_digest TEXT,
        decision TEXT NOT NULL CHECK (decision IN ('accepted','rejected')),
        reason TEXT NOT NULL,
        trust TEXT NOT NULL CHECK (trust IN ('principal_direct','untrusted','none')),
        received_at INTEGER NOT NULL)""",
    # Durable handoff: an accepted event is written here in the SAME
    # transaction as its dedupe row, and stays recoverable until the runner
    # acks a durable enqueue. The text is dropped on ack or expiry.
    """CREATE TABLE IF NOT EXISTS ingress_inbox (
        event_id TEXT PRIMARY KEY,
        event_json TEXT,
        status TEXT NOT NULL CHECK (status IN ('pending','handed_off','expired')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK ((status = 'pending') = (event_json IS NOT NULL)))""",
    "CREATE INDEX IF NOT EXISTS ingress_inbox_pending ON ingress_inbox (status, created_at)",
)


@dataclass(frozen=True)
class SeenRow:
    event_id: str
    content_digest: str
    event_digest: str | None
    decision: str
    reason: str


class IngressStore:
    def __init__(self, path: str | Path):
        self._path = str(path)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for stmt in _SCHEMA:
                c.execute(stmt)
            row = c.execute("SELECT value FROM meta WHERE key='ingress_schema'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta VALUES ('ingress_schema', ?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                c.execute("ROLLBACK")
                raise RuntimeError("unsupported ingress schema")
            c.execute("COMMIT")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self._path, isolation_level=None, timeout=30)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            yield c
        finally:
            c.close()

    @contextmanager
    def transaction(self) -> Iterator["_Tx"]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield _Tx(c)
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    def lookup_event(self, event_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT event_digest FROM ingress_seen WHERE event_id=? "
                            "AND decision='accepted'", (event_id,)).fetchone()
        return row[0] if row else None

    def pending_records(self, limit: int) -> list[tuple[str, str]]:
        with self._conn() as c:
            return c.execute("SELECT event_id, event_json FROM ingress_inbox WHERE "
                             "status='pending' ORDER BY created_at, rowid LIMIT ?",
                             (limit,)).fetchall()

    def pending_record(self, event_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT event_json FROM ingress_inbox WHERE event_id=? AND "
                            "status='pending'", (event_id,)).fetchone()
        return row[0] if row else None

    def ack(self, event_id: str, now: int) -> bool:
        """Mark handed off and drop the text. Idempotent: True if the event is
        (now or already) handed off, False if unknown or expired."""
        with self.transaction() as tx:
            row = tx._c.execute("SELECT status FROM ingress_inbox WHERE event_id=?",
                                (event_id,)).fetchone()
            if row is None or row[0] == "expired":
                return False
            tx._c.execute("UPDATE ingress_inbox SET status='handed_off', event_json=NULL, "
                          "updated_at=? WHERE event_id=? AND status='pending'", (now, event_id))
            return True

    def expire(self, before: int, now: int) -> list[str]:
        """Retention: drop the text of pending events created before ``before``."""
        with self.transaction() as tx:
            ids = [r[0] for r in tx._c.execute(
                "SELECT event_id FROM ingress_inbox WHERE status='pending' AND created_at<?",
                (before,))]
            tx._c.executemany("UPDATE ingress_inbox SET status='expired', event_json=NULL, "
                              "updated_at=? WHERE event_id=?", [(now, i) for i in ids])
            return ids

    def log(self) -> list[tuple]:
        with self._conn() as c:
            return c.execute("SELECT channel, event_id, decision, reason, trust, received_at "
                             "FROM ingress_seen ORDER BY rowid").fetchall()


class _Tx:
    def __init__(self, c: sqlite3.Connection):
        self._c = c

    def inbox_status(self, event_id: str) -> tuple[str, str | None] | None:
        row = self._c.execute("SELECT status, event_json FROM ingress_inbox WHERE event_id=?",
                              (event_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def inbox_put(self, event_id: str, event_json: str, now: int) -> None:
        self._c.execute("INSERT INTO ingress_inbox VALUES (?,?,'pending',?,?)",
                        (event_id, event_json, now, now))

    def get(self, dedupe_key: str) -> SeenRow | None:
        row = self._c.execute("SELECT event_id, content_digest, event_digest, decision, reason "
                              "FROM ingress_seen WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        return SeenRow(*row) if row else None

    def put(self, *, dedupe_key: str, channel: str, event_id: str, content_digest: str,
            event_digest: str | None, decision: str, reason: str, trust: str,
            received_at: int) -> None:
        self._c.execute("INSERT INTO ingress_seen VALUES (?,?,?,?,?,?,?,?,?)",
                        (dedupe_key, channel, event_id, content_digest, event_digest,
                         decision, reason, trust, received_at))


# ---- attachments -------------------------------------------------------------------

class AttachmentStore(Protocol):
    def put(self, data: bytes) -> str:
        """Store bytes; return an opaque reference. Must not interpret them."""
        ...


class InMemoryAttachmentStore:
    """Offline fake: content-addressed, in memory."""
    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        ref = f"att://sha256/{sha256(data)}"
        self.blobs[ref] = bytes(data)
        return ref
