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
        status TEXT NOT NULL CHECK (status IN ('pending','handed_off','expired',
                                               'quarantined')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK ((status = 'pending') = (event_json IS NOT NULL)))""",
    "CREATE INDEX IF NOT EXISTS ingress_inbox_pending ON ingress_inbox (status, created_at)",
    # Attachment lifecycle. A blob is written to the (independent, non-
    # transactional) AttachmentStore only after a durable ``staged`` marker is
    # committed; the event's ``attachment_refs`` rows are written in the same
    # transaction as the event and clear that marker. A blob is garbage only
    # when it has a staged/released marker older than the grace period AND no
    # ref row - so a rolled-back or crashed ingest is reclaimable, while a blob
    # shared with any committed event is never deleted.
    """CREATE TABLE IF NOT EXISTS attachment_staged (
        stage_id TEXT NOT NULL,
        ref TEXT NOT NULL,
        staged_at INTEGER NOT NULL,
        PRIMARY KEY (stage_id, ref))""",
    """CREATE TABLE IF NOT EXISTS attachment_refs (
        ref TEXT NOT NULL,
        event_id TEXT NOT NULL,
        PRIMARY KEY (ref, event_id))""",
    # The sent_at E3 signed into its E1 envelope for one platform message
    # (keyed by sha256 of the handoff key; no body, no sender ref). Kept until
    # E1's row is settled so every retry re-presents the identical envelope.
    """CREATE TABLE IF NOT EXISTS transport_handoff (
        hkey TEXT PRIMARY KEY,
        sent_at REAL NOT NULL,
        created_at INTEGER NOT NULL)""",
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

    def seen_content(self, dedupe_key: str) -> str | None:
        """Content digest already recorded for this dedupe key, if any."""
        with self._conn() as c:
            row = c.execute("SELECT content_digest FROM ingress_seen WHERE dedupe_key=?",
                            (dedupe_key,)).fetchone()
        return row[0] if row else None

    def lookup_event(self, event_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT event_digest FROM ingress_seen WHERE event_id=? "
                            "AND decision='accepted'", (event_id,)).fetchone()
        return row[0] if row else None

    def pending_records(self, limit: int, after: int = 0) -> list[tuple[int, str, str]]:
        """(rowid, event_id, event_json) of pending rows after ``after``, oldest
        first. The rowid is a cursor, so a caller scanning in batches never
        re-reads (or gets stuck on) rows it has already seen."""
        with self._conn() as c:
            return c.execute("SELECT rowid, event_id, event_json FROM ingress_inbox WHERE "
                             "status='pending' AND rowid > ? ORDER BY rowid LIMIT ?",
                             (after, limit)).fetchall()

    def pending_record(self, event_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT event_json FROM ingress_inbox WHERE event_id=? AND "
                            "status='pending'", (event_id,)).fetchone()
        return row[0] if row else None

    def quarantine(self, event_id: str, now: int) -> bool:
        """A pending row that cannot be restored (malformed, or it does not
        match the digest minted at acceptance) is moved out of ``pending`` for
        good: it can never starve later rows, never be returned or acked, and
        its untrustworthy text is dropped. Visible via ``quarantined()``."""
        with self.transaction() as tx:
            return tx.quarantine(event_id, now)

    def quarantined(self) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT event_id FROM ingress_inbox WHERE status='quarantined' ORDER BY rowid")]

    def ack(self, event_id: str, now: int) -> bool:
        """Mark handed off and drop the text. Idempotent: True if the event is
        (now or already) handed off, False if unknown or expired."""
        with self.transaction() as tx:
            row = tx._c.execute("SELECT status FROM ingress_inbox WHERE event_id=?",
                                (event_id,)).fetchone()
            if row is None or row[0] in ("expired", "quarantined"):
                return False
            tx._c.execute("UPDATE ingress_inbox SET status='handed_off', event_json=NULL, "
                          "updated_at=? WHERE event_id=? AND status='pending'", (now, event_id))
            return True

    def expire(self, before: int, now: int) -> list[str]:
        """Retention: drop the text of pending events created before ``before``,
        and release their attachment references (blobs become GC candidates)."""
        with self.transaction() as tx:
            ids = [r[0] for r in tx._c.execute(
                "SELECT event_id FROM ingress_inbox WHERE status='pending' AND created_at<?",
                (before,))]
            tx._c.executemany("UPDATE ingress_inbox SET status='expired', event_json=NULL, "
                              "updated_at=? WHERE event_id=?", [(now, i) for i in ids])
            for event_id in ids:
                refs = [r[0] for r in tx._c.execute(
                    "SELECT ref FROM attachment_refs WHERE event_id=?", (event_id,))]
                tx._c.execute("DELETE FROM attachment_refs WHERE event_id=?", (event_id,))
                tx._c.executemany("INSERT OR REPLACE INTO attachment_staged VALUES (?,?,?)",
                                  [(f"release:{event_id}", r, now) for r in refs])
            return ids

    # ---- attachments ---------------------------------------------------------

    def stage_attachments(self, stage_id: str, refs: list[str], now: int) -> None:
        """Durably mark blobs about to be written (committed BEFORE any put)."""
        with self.transaction() as tx:
            tx._c.executemany("INSERT OR REPLACE INTO attachment_staged VALUES (?,?,?)",
                              [(stage_id, r, now) for r in refs])

    # ---- E1 handoff --------------------------------------------------------

    def handoff_sent_at(self, hkey: str, sent_at: float, now: int) -> float:
        """The recorded sent_at for this handoff, recording ``sent_at`` first
        if there is none (committed before E1 is called)."""
        with self.transaction() as tx:
            tx._c.execute("INSERT OR IGNORE INTO transport_handoff VALUES (?,?,?)",
                          (hkey, float(sent_at), now))
            return tx._c.execute("SELECT sent_at FROM transport_handoff WHERE hkey=?",
                                 (hkey,)).fetchone()[0]

    def handoff_refresh(self, hkey: str, sent_at: float) -> float:
        with self.transaction() as tx:
            tx._c.execute("UPDATE transport_handoff SET sent_at=? WHERE hkey=?",
                          (float(sent_at), hkey))
        return float(sent_at)

    def handoff_drop(self, hkey: str) -> None:
        with self.transaction() as tx:
            tx._c.execute("DELETE FROM transport_handoff WHERE hkey=?", (hkey,))

    def handoffs(self) -> list[tuple]:
        with self._conn() as c:
            return c.execute("SELECT hkey, sent_at FROM transport_handoff").fetchall()

    # ---- attachment GC -----------------------------------------------------

    def mark_orphans(self, store_refs, now: int) -> list[str]:
        """Phase 2 of GC: blobs present in the store that E3 neither
        references nor has a marker for (a late ``put`` that landed after its
        stage was collected, or one followed by a crash) get an ``orphan:``
        marker now, so phase 1 reclaims them after the grace period."""
        with self.transaction() as tx:
            known = {r[0] for r in tx._c.execute(
                "SELECT ref FROM attachment_refs UNION SELECT ref FROM attachment_staged")}
            orphans = [r for r in store_refs if r not in known]
            tx._c.executemany("INSERT OR REPLACE INTO attachment_staged VALUES (?,?,?)",
                              [(f"orphan:{r}", r, now) for r in orphans])
            return orphans

    def collect_garbage(self, older_than: int, delete) -> list[str]:
        """Delete blobs whose every staged/released marker is older than
        ``older_than`` and that no committed event references. Runs under
        ``BEGIN IMMEDIATE``, so no ingest can commit a new reference to a blob
        between the check and the delete; an ingest that staged it again
        recently has a fresh marker and is skipped. ``delete`` must be
        idempotent; if it raises, nothing is forgotten (retried next run)."""
        with self.transaction() as tx:
            refs = [r[0] for r in tx._c.execute(
                "SELECT ref FROM attachment_staged GROUP BY ref HAVING MAX(staged_at) < ? "
                "AND ref NOT IN (SELECT ref FROM attachment_refs)", (older_than,))]
            for ref in refs:
                delete(ref)
            tx._c.executemany("DELETE FROM attachment_staged WHERE ref=?", [(r,) for r in refs])
            # Markers for blobs that ended up referenced are no longer needed.
            tx._c.execute("DELETE FROM attachment_staged WHERE staged_at < ? AND ref IN "
                          "(SELECT ref FROM attachment_refs)", (older_than,))
            return refs

    def attachment_state(self) -> tuple[list[tuple], list[tuple]]:
        with self._conn() as c:
            return (c.execute("SELECT ref, staged_at FROM attachment_staged").fetchall(),
                    c.execute("SELECT ref, event_id FROM attachment_refs").fetchall())

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

    def attach(self, event_id: str, refs: list[str]) -> None:
        self._c.executemany("INSERT OR IGNORE INTO attachment_refs VALUES (?,?)",
                            [(r, event_id) for r in refs])

    def stage_intact(self, stage_id: str, refs: list[str]) -> bool:
        """True iff every marker this ingest staged is still there. GC
        deletes the markers it collects in the same transaction as the blob,
        so a missing marker means the blob may be gone: the ingest must not
        commit a reference to it."""
        have = {r[0] for r in self._c.execute(
            "SELECT ref FROM attachment_staged WHERE stage_id=?", (stage_id,))}
        return set(refs) <= have

    def unstage(self, stage_id: str) -> None:
        self._c.execute("DELETE FROM attachment_staged WHERE stage_id=?", (stage_id,))

    def quarantine(self, event_id: str, now: int) -> bool:
        cur = self._c.execute("UPDATE ingress_inbox SET status='quarantined', event_json=NULL,"
                              " updated_at=? WHERE event_id=? AND status='pending'",
                              (now, event_id))
        return cur.rowcount == 1

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

def attachment_ref(digest: str) -> str:
    """The reference format E3 owns: content-addressed by SHA-256."""
    return f"att://sha256/{digest}"


class AttachmentStore(Protocol):
    """Content-addressed blob store. Contract:

    * ``put(data)`` stores the bytes and returns ``attachment_ref(sha256(data))``
      (idempotent; E3 refuses any other return value). Must not interpret them.
    * ``delete(ref)`` removes the blob; idempotent (a missing blob is fine).
    * ``exists(ref)`` and ``refs()`` (every stored ref) let E3 verify a blob
      before committing a reference to it and find orphans.

    It is not transactional with E3's database: E3 owns the lifecycle
    (stage -> put -> commit refs; ``collect_garbage`` for anything orphaned)."""
    def put(self, data: bytes) -> str: ...

    def delete(self, ref: str) -> None: ...

    def exists(self, ref: str) -> bool: ...

    def refs(self) -> list[str]: ...


class InMemoryAttachmentStore:
    """Offline fake: content-addressed, in memory."""
    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        ref = attachment_ref(sha256(data))
        self.blobs[ref] = bytes(data)
        return ref

    def delete(self, ref: str) -> None:
        self.blobs.pop(ref, None)

    def exists(self, ref: str) -> bool:
        return ref in self.blobs

    def refs(self) -> list[str]:
        return list(self.blobs)
