"""Durable T39 nonce store: "an unexpired, unconsumed nonce", enforced across
restarts and concurrent consumers.

Successor to experiments/b1_ssh/nonce_store.py (PR #3), keeping its
candidate-binding shape, with three changes the production gate needs:

* Explicit outcomes instead of a bare boolean. The experiment returned False
  for both a replay and any other integrity failure. Here the decision is made
  by reading the stored rows inside the same IMMEDIATE transaction, so only
  `CONSUMED` means first use, and any unexpected error is raised, not folded
  into "replay".
* Durability settings are fixed by the store, not left to the caller: WAL
  journal, synchronous=FULL, and a schema/version check plus quick_check on
  open. A store that fails these is unavailable, and consumers fail closed.
* Each record keeps the consumer identity and time, so a refused replay can
  say who used the nonce first.

This module does not verify signatures or receipts; that is the verifier's
job. It only answers "has this exact nonce pair been used, and for what".
"""
from __future__ import annotations

import enum
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CONSUMER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


class StoreUnavailable(RuntimeError):
    """The store cannot be trusted or reached. Consumers must refuse."""


class Outcome(enum.Enum):
    CONSUMED = "consumed"                 # first use; the only outcome that authorizes
    ALREADY_CONSUMED_SAME = "same"        # both nonces already used for this exact binding
    REPLAY = "replay"                     # either nonce used before for anything else


@dataclass(frozen=True)
class Binding:
    commit: str        # 40-hex commit SHA
    tree: str          # 40-hex tree SHA
    receipt_hash: str  # 64-hex hash over both signed payloads

    def __post_init__(self) -> None:
        if not (isinstance(self.commit, str) and _HEX40.fullmatch(self.commit)
                and isinstance(self.tree, str) and _HEX40.fullmatch(self.tree)
                and isinstance(self.receipt_hash, str) and _HEX64.fullmatch(self.receipt_hash)):
            raise ValueError("binding requires 40-hex commit/tree and 64-hex receipt_hash")


@dataclass(frozen=True)
class Record:
    nonce: str
    binding: Binding
    consumer: str
    consumed_at: str


def _check_nonces(nonces: object) -> tuple[str, str]:
    if (not isinstance(nonces, tuple) or len(nonces) != 2
            or any(not isinstance(n, str) or not _HEX64.fullmatch(n) for n in nonces)
            or nonces[0] == nonces[1]):
        raise ValueError("two distinct 64-hex nonces required")
    return nonces  # type: ignore[return-value]


_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS used_nonces (
    nonce        TEXT PRIMARY KEY CHECK (length(nonce) = 64),
    commit_id    TEXT NOT NULL CHECK (length(commit_id) = 40),
    tree_id      TEXT NOT NULL CHECK (length(tree_id) = 40),
    receipt_hash TEXT NOT NULL CHECK (length(receipt_hash) = 64),
    consumer     TEXT NOT NULL,
    consumed_at  TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS used_nonces_commit ON used_nonces (commit_id);
"""


class NonceStore:
    def __init__(self, path: Path, *, create: bool = False, timeout_s: float = 10.0) -> None:
        path = Path(path)
        if not create and not path.exists():
            raise StoreUnavailable("nonce store missing")  # never silently start empty
        try:
            self._db = sqlite3.connect(str(path), timeout=timeout_s, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            if create:
                self._db.executescript(_DDL)
                self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                                 (str(SCHEMA_VERSION),))
            version = self._db.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if version is None or version[0] != str(SCHEMA_VERSION):
                raise StoreUnavailable("nonce store schema version mismatch")
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreUnavailable("nonce store failed integrity check")
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"nonce store unusable: {exc}") from exc

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "NonceStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def consume(self, nonces: tuple[str, str], binding: Binding, consumer: str,
                now: datetime | None = None) -> Outcome:
        nonces = _check_nonces(nonces)
        if not isinstance(binding, Binding):
            raise ValueError("Binding required")
        if not isinstance(consumer, str) or not _CONSUMER.fullmatch(consumer):
            raise ValueError("consumer id must be a short lower-case identifier")
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT nonce, commit_id, tree_id, receipt_hash FROM used_nonces "
                    "WHERE nonce IN (?, ?)", nonces).fetchall()
                if rows:
                    same = (len(rows) == 2 and all(
                        (r[1], r[2], r[3]) == (binding.commit, binding.tree, binding.receipt_hash)
                        for r in rows))
                    self._db.execute("ROLLBACK")
                    return Outcome.ALREADY_CONSUMED_SAME if same else Outcome.REPLAY
                self._db.executemany(
                    "INSERT INTO used_nonces VALUES (?, ?, ?, ?, ?, ?)",
                    [(n, binding.commit, binding.tree, binding.receipt_hash, consumer, stamp)
                     for n in nonces])
                self._db.execute("COMMIT")
                return Outcome.CONSUMED
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"nonce store error: {exc}") from exc

    def lookup(self, nonce: str) -> Record | None:
        if not isinstance(nonce, str) or not _HEX64.fullmatch(nonce):
            raise ValueError("64-hex nonce required")
        try:
            row = self._db.execute(
                "SELECT nonce, commit_id, tree_id, receipt_hash, consumer, consumed_at "
                "FROM used_nonces WHERE nonce = ?", (nonce,)).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"nonce store error: {exc}") from exc
        if row is None:
            return None
        return Record(row[0], Binding(row[1], row[2], row[3]), row[4], row[5])

    def commits_with_receipts(self, commits: list[str]) -> set[str]:
        """Which of these commits have a consumed receipt (for the direct-push detector)."""
        if any(not isinstance(c, str) or not _HEX40.fullmatch(c) for c in commits):
            raise ValueError("40-hex commit SHAs required")
        found: set[str] = set()
        try:
            for i in range(0, len(commits), 500):
                chunk = commits[i:i + 500]
                marks = ",".join("?" * len(chunk))
                found.update(r[0] for r in self._db.execute(
                    f"SELECT DISTINCT commit_id FROM used_nonces WHERE commit_id IN ({marks})",
                    chunk))
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"nonce store error: {exc}") from exc
        return found
