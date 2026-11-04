"""T39 receipt registry: one-time issuance against an authoritative anchor log.

Lifecycle:
  * ISSUANCE happens once, at merge admission. A fully verified attestation
    pair is issued as a receipt; its nonces are consumed by that same event.
  * USE by later consumers (release builder, host updater, rollback) consumes
    nothing; each re-verifies the signed receipt and requires the issued
    record. Uses are appended for audit.

Authority (Dolores review on 6f63a67): the ANCHOR LOG is the authority, and
the local SQLite database is a replica of it.
  * Every event (genesis, issue, use) is appended to the anchor log FIRST,
    with compare-and-set on its sequence number, and only then to the local
    database. The anchor is meant to live on a separate host with its own
    write credential (production: Brooklyn under T35), so database write
    access alone cannot add an event.
  * On open and inside every write transaction, the database must be an exact
    prefix of the anchor log. Extra or different local rows mean tampering,
    and the registry refuses. Anchor entries missing locally (a crash after
    the anchor append, a lost database, or a restored older backup) are
    replayed from the anchor, so no crash boundary loses or invents history.
  * Every READ (get, issued_trees, receipted_commits) is answered from the
    hash-verified anchor log, never from local rows. Tampering with the
    database while a process is running cannot create a receipt.
  * `initialize` appends genesis to an empty anchor first. It is refused if
    the anchor already holds entries; a lost database is rebuilt from the
    anchor by `restore_from_anchor`, never restarted empty.

The registry stores facts the verifier established; it does not verify
signatures. `issue` is reached only through consumer_gate after verification.
"""
from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

SCHEMA_VERSION = "3"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_ZERO = "0" * 64
_KINDS = ("genesis", "issue", "use", "clear", "incident")


class RegistryUnavailable(RuntimeError):
    """Missing, tampered, forked or unreachable. Every consumer must refuse."""


class AnchorConflict(RegistryUnavailable):
    """Compare-and-set failed: the anchor's next sequence is not the one offered."""


class Issue(enum.Enum):
    ISSUED = "issued"
    ALREADY_ISSUED = "already_issued"   # identical receipt already issued (issuer retry)
    REPLAY = "replay"                   # a nonce or receipt hash was used for something else


@dataclass(frozen=True)
class Facts:
    """What the verifier established about one attestation pair."""
    commit: str
    tree: str
    receipt_hash: str
    nonces: tuple[str, str]
    agents: dict[str, str]              # role -> agent
    key_fingerprints: dict[str, str]    # role -> key fingerprint
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


def _link(prev: str, seq: int, kind: str, body: dict) -> str:
    payload = json.dumps({"prev": prev, "seq": seq, "kind": kind, "body": body},
                         sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"potter.t39.chain/v1\0" + payload).hexdigest()


def _entry(prev: str, seq: int, kind: str, body: dict) -> dict:
    return {"seq": seq, "kind": kind, "body": body, "head": _link(prev, seq, kind, body)}


class Anchor(Protocol):
    """Append-only log on an independently controlled host."""
    def entries(self) -> list[dict]: ...
    def append(self, entry: dict) -> None: ...   # CAS: entry["seq"] must equal len(log)


class FileAnchor:
    """Anchor log as a JSON-lines file. In production this is a separate host
    with its own write credential; tests keep it in a separate directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = self.path.with_suffix(self.path.suffix + ".lock")

    def entries(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise RegistryUnavailable("anchor unreadable") from exc
        try:
            return [json.loads(line) for line in lines]
        except ValueError as exc:
            raise RegistryUnavailable("anchor malformed") from exc

    def append(self, entry: dict) -> None:
        with open(self._lock, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            if entry["seq"] != len(self.entries()):
                raise AnchorConflict("anchor sequence moved")
            new = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            if new:
                dfd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)


def _verified_log(anchor: Anchor) -> list[dict]:
    """Anchor entries with sequence and hash chain checked."""
    try:
        log = anchor.entries()
    except RegistryUnavailable:
        raise
    except Exception as exc:
        raise RegistryUnavailable(f"anchor unreachable: {type(exc).__name__}") from exc
    prev, issued = _ZERO, set()
    for i, e in enumerate(log):
        if (not isinstance(e, dict) or e.get("seq") != i
                or e.get("kind") not in _KINDS
                or (i == 0) != (e["kind"] == "genesis")
                or _link(prev, i, e["kind"], e["body"]) != e.get("head")):
            raise RegistryUnavailable(f"anchor log broken at {i}")
        if e["kind"] == "issue":
            issued.add(e["body"]["facts"]["receipt_hash"])
        if e["kind"] == "use" and e["body"].get("receipt_hash") not in issued:
            raise RegistryUnavailable("anchor log has a use before its issuance")
        prev = e["head"]
    return log


@dataclass(frozen=True)
class IssuedReceipt:
    seq: int
    facts: dict
    issuer: str
    issued_at: str


_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE chain (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL
        CHECK (kind IN ('genesis','issue','use','clear','incident')),
    body TEXT NOT NULL, head TEXT NOT NULL);
CREATE TABLE nonces (nonce TEXT PRIMARY KEY, receipt_hash TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE receipts (
    receipt_hash TEXT PRIMARY KEY, commit_id TEXT NOT NULL, seq INTEGER NOT NULL UNIQUE)
    WITHOUT ROWID;
"""


class ReceiptRegistry:
    @classmethod
    def initialize(cls, path: Path, anchor: Anchor, registry_id: str,
                   now: datetime) -> "ReceiptRegistry":
        """One-time bootstrap: genesis goes to an EMPTY anchor first."""
        path = Path(path)
        if not _NAME.fullmatch(registry_id):
            raise ValueError("registry_id must be a short lower-case identifier")
        if path.exists():
            raise RegistryUnavailable("registry already exists; refusing to re-initialize")
        if _verified_log(anchor):
            raise RegistryUnavailable("anchor already has history; use restore_from_anchor")
        anchor.append(_entry(_ZERO, 0, "genesis",
                             {"registry_id": registry_id, "at": now.isoformat()}))
        return cls.restore_from_anchor(path, anchor)

    @classmethod
    def restore_from_anchor(cls, path: Path, anchor: Anchor) -> "ReceiptRegistry":
        """Build a fresh local replica from the authoritative anchor log."""
        path = Path(path)
        if path.exists():
            raise RegistryUnavailable("refusing to overwrite an existing registry file")
        if not _verified_log(anchor):
            raise RegistryUnavailable("anchor is empty; nothing to restore")
        db = sqlite3.connect(str(path), isolation_level=None)
        try:
            db.executescript(_DDL)
            db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        finally:
            db.close()
        return cls(path, anchor)

    def __init__(self, path: Path, anchor: Anchor, timeout_s: float = 10.0) -> None:
        path = Path(path)
        if not path.exists():
            raise RegistryUnavailable("registry missing (use restore_from_anchor)")
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
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._reconcile()
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise RegistryUnavailable(f"registry unusable: {exc}") from exc

    def close(self) -> None:
        self._db.close()

    # ---- replica maintenance (call inside BEGIN IMMEDIATE) -------------------

    def _apply(self, e: dict) -> None:
        self._db.execute("INSERT INTO chain VALUES (?, ?, ?, ?)",
                         (e["seq"], e["kind"], json.dumps(e["body"], sort_keys=True), e["head"]))
        if e["kind"] == "issue":
            f = e["body"]["facts"]
            self._db.executemany("INSERT INTO nonces VALUES (?, ?)",
                                 [(n, f["receipt_hash"]) for n in f["nonces"]])
            self._db.execute("INSERT INTO receipts VALUES (?, ?, ?)",
                             (f["receipt_hash"], f["commit"], e["seq"]))

    def _reconcile(self) -> list[dict]:
        """Local replica must be an exact prefix of the anchor; replay any tail."""
        log = _verified_log(self._anchor)
        if not log:
            raise RegistryUnavailable("anchor empty")
        local = self._db.execute("SELECT seq, kind, body, head FROM chain ORDER BY seq").fetchall()
        if len(local) > len(log):
            raise RegistryUnavailable("local registry has events the anchor never recorded "
                                      "(tampering)")
        for (seq, kind, body, head), e in zip(local, log):
            if (seq, kind, head) != (e["seq"], e["kind"], e["head"]) or json.loads(body) != e["body"]:
                raise RegistryUnavailable(f"local registry diverges from anchor at {seq}")
        for e in log[len(local):]:
            self._apply(e)            # crash tail, lost rows or older backup
        nonces, receipts = {}, {}
        for e in log:
            if e["kind"] == "issue":
                f = e["body"]["facts"]
                for n in f["nonces"]:
                    nonces[n] = f["receipt_hash"]
                receipts[f["receipt_hash"]] = (f["commit"], e["seq"])
        if dict(self._db.execute("SELECT nonce, receipt_hash FROM nonces")) != nonces:
            raise RegistryUnavailable("nonce index diverges from anchor")
        if {r: (c, s) for r, c, s in self._db.execute(
                "SELECT receipt_hash, commit_id, seq FROM receipts")} != receipts:
            raise RegistryUnavailable("receipt index diverges from anchor")
        return log

    def _append(self, log: list[dict], kind: str, body: dict) -> None:
        e = _entry(log[-1]["head"], len(log), kind, body)
        self._anchor.append(e)        # authority first; CAS on seq
        self._apply(e)

    def _write(self, fn):
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                log = self._reconcile()
                result = fn(log)
                self._db.execute("COMMIT")
                return result
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise RegistryUnavailable(f"registry error: {exc}") from exc

    # ---- writes ---------------------------------------------------------------

    def issue(self, facts: Facts, issuer: str, now: datetime) -> Issue:
        if not isinstance(facts, Facts):
            raise ValueError("Facts required")
        if not _NAME.fullmatch(issuer):
            raise ValueError("issuer must be a short lower-case identifier")

        def do(log):
            existing = self._issued_in(log).get(facts.receipt_hash)
            if existing is not None:
                return (Issue.ALREADY_ISSUED if existing.facts == facts.canonical()
                        else Issue.REPLAY)
            if self._db.execute("SELECT 1 FROM nonces WHERE nonce IN (?, ?)",
                                facts.nonces).fetchone():
                return Issue.REPLAY
            self._append(log, "issue", {"facts": facts.canonical(), "issuer": issuer,
                                        "at": now.isoformat()})
            return Issue.ISSUED
        return self._write(do)

    def record_use(self, receipt_hash: str, consumer: str, purpose: str, now: datetime) -> None:
        if not _NAME.fullmatch(consumer) or not _NAME.fullmatch(purpose):
            raise ValueError("consumer and purpose must be short lower-case identifiers")

        def do(log):
            if receipt_hash not in self._issued_in(log):
                raise RegistryUnavailable("use refused: receipt was never issued")
            self._append(log, "use", {"receipt_hash": receipt_hash, "consumer": consumer,
                                      "purpose": purpose, "at": now.isoformat()})
        self._write(do)

    def record_detector(self, kind: str, body: dict) -> None:
        """Anchor a detector outcome: 'clear' (branch, tip) or 'incident'."""
        if kind not in ("clear", "incident"):
            raise ValueError("detector events are 'clear' or 'incident'")
        if not (isinstance(body.get("branch"), str) and isinstance(body.get("tip"), str)
                and isinstance(body.get("at"), str)):
            raise ValueError("detector event needs branch, tip and at")
        self._write(lambda log: self._append(log, kind, body))

    def last_detector_event(self, branch: str) -> dict | None:
        """Latest anchored 'clear' or 'incident' for this branch, from the anchor log."""
        for e in reversed(_verified_log(self._anchor)):
            if e["kind"] in ("clear", "incident") and e["body"].get("branch") == branch:
                return e
        return None

    # ---- reads: answered from the verified anchor log only --------------------

    @staticmethod
    def _issued_in(log: list[dict]) -> dict[str, IssuedReceipt]:
        return {e["body"]["facts"]["receipt_hash"]:
                IssuedReceipt(e["seq"], e["body"]["facts"], e["body"]["issuer"], e["body"]["at"])
                for e in log if e["kind"] == "issue"}

    def get(self, receipt_hash: str) -> IssuedReceipt | None:
        return self._issued_in(_verified_log(self._anchor)).get(receipt_hash)

    def issued_trees(self, commits: list[str]) -> dict[str, str]:
        if any(not isinstance(c, str) or not _HEX40.fullmatch(c) for c in commits):
            raise ValueError("40-hex commit SHAs required")
        wanted = set(commits)
        return {r.facts["commit"]: r.facts["tree"]
                for r in self._issued_in(_verified_log(self._anchor)).values()
                if r.facts["commit"] in wanted}

    def receipted_commits(self, commits: list[str]) -> set[str]:
        return set(self.issued_trees(commits))
