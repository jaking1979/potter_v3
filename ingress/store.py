"""Dedupe store + ingress log (SQLite) and the attachment store seam.

``ingress_seen`` holds one row per (channel, dedupe key): the event id, the
content digest, the minted event digest and the decision. It never stores
message text, sender references or parameters. The ingress log is the same
row: a fixed reason code per decision.

Every decision runs in one ``BEGIN IMMEDIATE`` transaction: look up the key,
insert the row, emit the C6 trace, commit. If the trace sink raises, the
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

    def log(self) -> list[tuple]:
        with self._conn() as c:
            return c.execute("SELECT channel, event_id, decision, reason, trust, received_at "
                             "FROM ingress_seen ORDER BY rowid").fetchall()


class _Tx:
    def __init__(self, c: sqlite3.Connection):
        self._c = c

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
