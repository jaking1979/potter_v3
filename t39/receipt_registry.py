"""T39 receipt registry: one-time issuance, durable record, anti-rollback.

Lifecycle (Dolores review on 79e4c0e, finding 1):
  * ISSUANCE happens once, at merge admission. A fully verified attestation
    pair is turned into an issued receipt; its two nonces are consumed in the
    same transaction. This is the only place nonces are consumed.
  * USE by later consumers (release builder, host updater, rollback) does not
    consume anything. Each consumer independently re-verifies the signed
    receipt and then requires the exact issued record to exist here. Uses are
    appended to the log for audit.

Integrity (findings 2 and 3):
  * Every event (genesis, issue, use) is a row in one hash chain. Opening the
    registry re-verifies the whole chain, so an edited, deleted or forged row
    makes the registry unavailable.
  * The chain head (seq, hash) is mirrored to an external Anchor after every
    commit. On open the registry must contain the anchored head; a restored
    older backup (shorter chain) or a fork is refused. A registry ahead of its
    anchor by a crash between commit and anchor write is accepted only if it
    extends the anchored head, and the anchor is then caught up.
  * Creation is an explicit one-time `initialize`, refused if the file or an
    anchored head already exists, so state loss cannot silently restart empty.

The registry stores facts the verifier established; it does not verify
signatures itself. `issue` is only reachable through consumer_gate after
verification, and the detector reads issued receipts, never bare nonces.
"""
from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

SCHEMA_VERSION = "2"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


class RegistryUnavailable(RuntimeError):
    """Missing, corrupt, rolled back or forked. Every consumer must refuse."""


class Issue(enum.Enum):
    ISSUED = "issued"
    ALREADY_ISSUED = "already_issued"   # exact same receipt already issued (issuer retry)
    REPLAY = "replay"                   # a nonce was used for something else


@dataclass(frozen=True)
class Facts:
    """What the verifier established about one attestation pair."""
    commit: str
    tree: str
    receipt_hash: str
    nonces: tuple[str, str]
    agents: dict[str, str]              # role -> agent
    key_fingerprints: dict[str, str]    # role -> SHA256 key fingerprint
    verifier_version: str

    def __post_init__(self) -> None:
        ok = (isinstance(self.commit, str) and _HEX40.fullmatch(self.commit)
              and isinstance(self.tree, str) and _HEX40.fullmatch(self.tree)
              and isinstance(self.receipt_hash, str) and _HEX64.fullmatch(self.receipt_hash)
              and isinstance(self.nonces, tuple) and len(self.nonces) == 2
              and all(isinstance(n, str) and _HEX64.fullmatch(n) for n in self.nonces)
              and self.nonces[0] != self.nonces[1]
              and isinstance(self.agents, dict) and set(self.agents) == {"author", "reviewer"}
              and all(isinstance(v, str) and _NAME.fullmatch(v) for v in self.agents.values())
              and self.agents["author"] != self.agents["reviewer"]
              and isinstance(self.key_fingerprints, dict)
              and set(self.key_fingerprints) == {"author", "reviewer"}
              and self.key_fingerprints["author"] != self.key_fingerprints["reviewer"]
              and isinstance(self.verifier_version, str) and self.verifier_version)
        if not ok:
            raise ValueError("incomplete or malformed verified facts")

    def canonical(self) -> dict:
        return {"commit": self.commit, "tree": self.tree, "receipt_hash": self.receipt_hash,
                "nonces": sorted(self.nonces), "agents": self.agents,
                "key_fingerprints": self.key_fingerprints,
                "verifier_version": self.verifier_version}


class Anchor(Protocol):
    def read(self) -> tuple[int, str] | None: ...
    def advance(self, seq: int, head: str) -> None: ...   # monotonic


class FileAnchor:
    """Anchor kept in a separate file (in production: a separate host/location).

    `advance` is monotonic under an exclusive lock: a slower writer can never
    move the anchor backwards.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = self.path.with_suffix(self.path.suffix + ".lock")

    def read(self) -> tuple[int, str] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RegistryUnavailable("anchor unreadable") from exc
        if (not isinstance(data, dict) or not isinstance(data.get("seq"), int)
                or not isinstance(data.get("head"), str) or not _HEX64.fullmatch(data["head"])):
            raise RegistryUnavailable("anchor malformed")
        return data["seq"], data["head"]

    def advance(self, seq: int, head: str) -> None:
        with open(self._lock, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            current = self.read()
            if current is not None and current[0] >= seq:
                return
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".anchor-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"seq": seq, "head": head}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)


def _link(prev: str, seq: int, kind: str, body: dict) -> str:
    payload = json.dumps({"prev": prev, "seq": seq, "kind": kind, "body": body},
                         sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"potter.t39.chain/v1\0" + payload).hexdigest()


@dataclass(frozen=True)
class IssuedReceipt:
    seq: int
    facts: dict
    issuer: str
    issued_at: str


_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE chain (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('genesis','issue','use')),
    body TEXT NOT NULL, head TEXT NOT NULL);
CREATE TABLE nonces (nonce TEXT PRIMARY KEY, receipt_hash TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE receipts (
    receipt_hash TEXT PRIMARY KEY, commit_id TEXT NOT NULL, seq INTEGER NOT NULL UNIQUE)
    WITHOUT ROWID;
CREATE INDEX receipts_commit ON receipts (commit_id);
"""


class ReceiptRegistry:
    @classmethod
    def initialize(cls, path: Path, anchor: Anchor, registry_id: str,
                   now: datetime) -> "ReceiptRegistry":
        """One-time trusted bootstrap. Refuses if any prior state exists."""
        path = Path(path)
        if path.exists():
            raise RegistryUnavailable("registry already exists; refusing to re-initialize")
        if anchor.read() is not None:
            raise RegistryUnavailable("anchor already set; state was lost, not new")
        if not _NAME.fullmatch(registry_id):
            raise ValueError("registry_id must be a short lower-case identifier")
        db = sqlite3.connect(str(path), isolation_level=None)
        try:
            db.executescript(_DDL)
            body = {"registry_id": registry_id, "at": now.isoformat()}
            head = _link("0" * 64, 0, "genesis", body)
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            db.execute("INSERT INTO chain VALUES (0, 'genesis', ?, ?)",
                       (json.dumps(body, sort_keys=True), head))
            db.execute("COMMIT")
        finally:
            db.close()
        anchor.advance(0, head)
        return cls(path, anchor)

    def __init__(self, path: Path, anchor: Anchor, timeout_s: float = 10.0) -> None:
        path = Path(path)
        if not path.exists():
            raise RegistryUnavailable("registry missing")
        self._anchor = anchor
        try:
            self._db = sqlite3.connect(str(path), timeout=timeout_s, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            v = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if v is None or v[0] != SCHEMA_VERSION:
                raise RegistryUnavailable("registry schema version mismatch")
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RegistryUnavailable("registry failed integrity check")
            seq, head = self._verify_chain()
        except sqlite3.Error as exc:
            raise RegistryUnavailable(f"registry unusable: {exc}") from exc
        anchored = anchor.read()
        if anchored is None:
            raise RegistryUnavailable("anchor missing")
        a_seq, a_head = anchored
        row = self._db.execute("SELECT head FROM chain WHERE seq = ?", (a_seq,)).fetchone()
        if a_seq > seq or row is None or row[0] != a_head:
            raise RegistryUnavailable("registry is behind or forked from its anchor (rollback)")
        if seq > a_seq:
            anchor.advance(seq, head)       # crash between commit and anchor write

    def close(self) -> None:
        self._db.close()

    def _verify_chain(self) -> tuple[int, str]:
        prev, expected_seq = "0" * 64, 0
        issued, used_nonces = {}, {}
        for seq, kind, body_text, head in self._db.execute(
                "SELECT seq, kind, body, head FROM chain ORDER BY seq"):
            body = json.loads(body_text)
            if seq != expected_seq or (seq == 0) != (kind == "genesis"):
                raise RegistryUnavailable("chain sequence broken")
            if _link(prev, seq, kind, body) != head:
                raise RegistryUnavailable("chain hash mismatch (edited or forged row)")
            if kind == "use" and body.get("receipt_hash") not in issued:
                raise RegistryUnavailable("use event for a receipt not issued earlier in the chain")
            if kind == "issue":
                issued[body["facts"]["receipt_hash"]] = (body["facts"]["commit"], seq)
                for n in body["facts"]["nonces"]:
                    used_nonces[n] = body["facts"]["receipt_hash"]
            prev, expected_seq = head, seq + 1
        if expected_seq == 0:
            raise RegistryUnavailable("chain empty")
        # Index tables must be exactly what the chain says.
        if dict(self._db.execute("SELECT nonce, receipt_hash FROM nonces")) != used_nonces:
            raise RegistryUnavailable("nonce index disagrees with chain")
        if {r: (c, s) for r, c, s in self._db.execute(
                "SELECT receipt_hash, commit_id, seq FROM receipts")} != issued:
            raise RegistryUnavailable("receipt index disagrees with chain")
        return expected_seq - 1, prev

    def _append(self, kind: str, body: dict) -> tuple[int, str]:
        seq, prev = self._db.execute(
            "SELECT seq, head FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
        head = _link(prev, seq + 1, kind, body)
        self._db.execute("INSERT INTO chain VALUES (?, ?, ?, ?)",
                         (seq + 1, kind, json.dumps(body, sort_keys=True), head))
        return seq + 1, head

    def issue(self, facts: Facts, issuer: str, now: datetime) -> Issue:
        if not isinstance(facts, Facts):
            raise ValueError("Facts required")
        if not _NAME.fullmatch(issuer):
            raise ValueError("issuer must be a short lower-case identifier")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.get(facts.receipt_hash)
                if existing is not None:
                    self._db.execute("ROLLBACK")
                    same = existing.facts == facts.canonical()
                    return Issue.ALREADY_ISSUED if same else Issue.REPLAY
                if self._db.execute("SELECT 1 FROM nonces WHERE nonce IN (?, ?)",
                                    facts.nonces).fetchone():
                    self._db.execute("ROLLBACK")
                    return Issue.REPLAY
                body = {"facts": facts.canonical(), "issuer": issuer, "at": now.isoformat()}
                seq, head = self._append("issue", body)
                self._db.executemany("INSERT INTO nonces VALUES (?, ?)",
                                     [(n, facts.receipt_hash) for n in facts.nonces])
                self._db.execute("INSERT INTO receipts VALUES (?, ?, ?)",
                                 (facts.receipt_hash, facts.commit, seq))
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise RegistryUnavailable(f"registry error: {exc}") from exc
        self._anchor.advance(seq, head)
        return Issue.ISSUED

    def get(self, receipt_hash: str) -> IssuedReceipt | None:
        row = self._db.execute(
            "SELECT c.seq, c.body FROM receipts r JOIN chain c ON c.seq = r.seq"
            " WHERE r.receipt_hash = ?", (receipt_hash,)).fetchone()
        if row is None:
            return None
        body = json.loads(row[1])
        return IssuedReceipt(row[0], body["facts"], body["issuer"], body["at"])

    def record_use(self, receipt_hash: str, consumer: str, purpose: str, now: datetime) -> None:
        if not _NAME.fullmatch(consumer) or not _NAME.fullmatch(purpose):
            raise ValueError("consumer and purpose must be short lower-case identifiers")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if not self._db.execute("SELECT 1 FROM receipts WHERE receipt_hash = ?",
                                        (receipt_hash,)).fetchone():
                    raise RegistryUnavailable("use refused: receipt was never issued")
                seq, head = self._append("use", {"receipt_hash": receipt_hash,
                                                 "consumer": consumer, "purpose": purpose,
                                                 "at": now.isoformat()})
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise RegistryUnavailable(f"registry error: {exc}") from exc
        self._anchor.advance(seq, head)

    def receipted_commits(self, commits: list[str]) -> set[str]:
        if any(not isinstance(c, str) or not _HEX40.fullmatch(c) for c in commits):
            raise ValueError("40-hex commit SHAs required")
        found: set[str] = set()
        for i in range(0, len(commits), 500):
            chunk = commits[i:i + 500]
            found.update(r[0] for r in self._db.execute(
                f"SELECT DISTINCT commit_id FROM receipts WHERE commit_id IN "
                f"({','.join('?' * len(chunk))})", chunk))
        return found
