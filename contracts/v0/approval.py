"""Reference verifier for ApprovalRecord v0 (package C0).

Contract only: C1 (PolicyEngine) implements the production check. This module
fixes the semantics both sides test against:

* Provenance: a record is evidence of Josh's approval only if it carries a valid
  issuer signature from the trusted T19 issuer (E1). Fields such as
  `decided_by` and `channel` are claims *inside* that signed envelope, never
  proof by themselves. No authenticator configured -> refuse.
* Binding: the approval covers exactly sha256(canonical BoundAction).
* Validity: at most 24 h from issue (T37 S1: "an approval after 24 h is
  refused"), regardless of the signed expires_at; a narrower policy TTL may
  apply on top.
* Stale state: BoundAction.preconditions (e.g. the version/etag of the thing
  being changed) is inside the hashed action, so an approval given against old
  state does not match the action computed against new state (T37 S1).
* Single use, without stranding: consumption and recording of the intended
  effect are ONE atomic step, ApprovalLedger.consume(nonce, intent). After a
  crash the pending intent is still there, bound to this approval, and C2
  recovery reconciles it per T10 (read external state first; never blind
  retry). A crash therefore can neither replay the approval nor strand it.
* Fail closed: any malformed, unauthenticated, stale, mismatched or reused record
  raises ApprovalRefused, and nothing is consumed unless every check passes.

Stdlib only, deterministic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

REQUIRED = ("schema", "approval_id", "task_id", "action_hash", "decision",
            "decided_by", "channel", "issued_at", "expires_at", "nonce",
            "issuer_key_id", "issuer_signature")
CHANNELS = {"telegram", "potter_home", "passkey_step_up"}
SCREENSHOT_CAPS = {"browser", "computer"}
ALLOWED_KEYS = set(REQUIRED) | {"screenshot_ref"}
MAX_VALIDITY_S = 24 * 3600   # T37 S1
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[0-9a-f]{32,}$")


class ApprovalRefused(Exception):
    """Any reason the action may not proceed. Callers treat every exception
    from verify(), including unexpected ones, as deny."""


class Authenticator(Protocol):
    """Trusted-issuance boundary. Production: the E1 issuer's signing key, with
    the verifier holding only what it needs to verify (selected under T11/T12)."""
    def verify(self, key_id: str, payload: bytes, signature: str) -> bool: ...


@dataclass(frozen=True)
class EffectIntent:
    """What will be done under this approval; written atomically with consumption.
    Carries the exact canonical action so recovery can reconstruct the external
    operation after a restart. C2's effect journal is its production home.

    Redaction boundary: action_canonical holds operation parameters (e.g. an email
    body) and lives only in the local effect journal, never in model context or
    logs. Secrets never appear in parameters; T11/T12 SecretRefs are used instead.
    """
    effect_id: str
    task_id: str
    action_hash: str
    approval_id: str
    action_canonical: str

    def action(self) -> "BoundAction":
        """Rebuild the exact approved action, refusing if it was altered."""
        if hashlib.sha256(self.action_canonical.encode("utf-8")).hexdigest() != self.action_hash:
            raise ApprovalRefused("intent integrity: canonical action does not match approved hash")
        return BoundAction.from_canonical(self.action_canonical)


class ApprovalLedger(Protocol):
    def consume(self, nonce: str, intent: EffectIntent) -> bool:
        """Atomically: if `nonce` is unused, mark it used AND durably record
        `intent` as pending, then return True. Otherwise change nothing and
        return False. Must hold across threads, processes and restarts
        (production: one C2 transaction). Mandatory C1/C2 gate."""
        ...

    def pending(self, nonce: str) -> EffectIntent | None:
        """The intent recorded at consumption, for crash recovery (T10)."""
        ...


class HmacAuthenticator:
    """Test/reference authenticator. Not a production key-custody design."""
    def __init__(self, keys: dict[str, bytes]):
        self._keys = dict(keys)

    def sign(self, key_id: str, payload: bytes) -> str:
        return hmac.new(self._keys[key_id], payload, hashlib.sha256).hexdigest()

    def verify(self, key_id: str, payload: bytes, signature: str) -> bool:
        key = self._keys.get(key_id)
        if key is None or not isinstance(signature, str):
            return False
        return hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).hexdigest(), signature)


class MemoryApprovalLedger:
    """Reference single-process ledger: atomic via one lock, NOT durable.
    Proves the semantics only; production durability is a C1/C2 gate."""
    def __init__(self):
        self._intents: dict[str, EffectIntent] = {}
        self._lock = threading.Lock()

    def consume(self, nonce: str, intent: EffectIntent) -> bool:
        with self._lock:
            if nonce in self._intents:
                return False
            self._intents[nonce] = intent
            return True

    def pending(self, nonce: str) -> EffectIntent | None:
        with self._lock:
            return self._intents.get(nonce)

    def __contains__(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._intents


class SqliteApprovalLedger:
    """Reference durable ledger: one SQLite transaction marks the nonce used and
    stores the intent (PRIMARY KEY on nonce gives atomic single use across
    connections and processes). Shows restart-surviving semantics; the
    production store is C2's effect journal."""
    SCHEMA = """CREATE TABLE IF NOT EXISTS approval_intents (
        nonce TEXT PRIMARY KEY, effect_id TEXT NOT NULL, task_id TEXT NOT NULL,
        action_hash TEXT NOT NULL, approval_id TEXT NOT NULL,
        action_canonical TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')"""

    def __init__(self, path: str):
        self._path = path
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(self.SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30, isolation_level=None)

    def consume(self, nonce: str, intent: EffectIntent) -> bool:
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("INSERT INTO approval_intents (nonce, effect_id, task_id, action_hash,"
                          " approval_id, action_canonical) VALUES (?,?,?,?,?,?)",
                          (nonce, intent.effect_id, intent.task_id, intent.action_hash,
                           intent.approval_id, intent.action_canonical))
            except sqlite3.IntegrityError:
                c.execute("ROLLBACK")
                return False
            c.execute("COMMIT")
            return True
        finally:
            c.close()

    def pending(self, nonce: str) -> EffectIntent | None:
        c = self._conn()
        try:
            row = c.execute("SELECT effect_id, task_id, action_hash, approval_id, action_canonical"
                            " FROM approval_intents WHERE nonce=? AND status='pending'", (nonce,)).fetchone()
        finally:
            c.close()
        return EffectIntent(*row) if row else None


@dataclass(frozen=True)
class BoundAction:
    task_id: str
    capability: str          # registry capability, e.g. "gmail.send", "computer"
    operation: str
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)
    preconditions: dict[str, Any] = field(default_factory=dict)  # state the approval assumed

    def canonical_json(self) -> bytes:
        body = {"task_id": self.task_id, "capability": self.capability,
                "operation": self.operation, "target": self.target,
                "parameters": self.parameters, "preconditions": self.preconditions}
        return _canon(body)

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()

    @classmethod
    def from_canonical(cls, text: str) -> "BoundAction":
        d = json.loads(text)
        return cls(d["task_id"], d["capability"], d["operation"], d["target"],
                   d["parameters"], d["preconditions"])


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def signing_payload(record: dict) -> bytes:
    """Bytes the issuer signs: every field except the signature itself."""
    return _canon({k: v for k, v in record.items() if k != "issuer_signature"})


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and len(v) > 0


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_shape(record: Any) -> None:
    """Enforce approval_record.schema.json constraints (stdlib, no jsonschema)."""
    if not isinstance(record, dict):
        raise ApprovalRefused("malformed: not an object")
    missing = [k for k in REQUIRED if k not in record]
    if missing:
        raise ApprovalRefused(f"malformed: missing {missing}")
    extra = set(record) - ALLOWED_KEYS
    if extra:
        raise ApprovalRefused(f"malformed: unexpected {sorted(extra)}")
    if not (_nonempty_str(record["approval_id"]) and len(record["approval_id"]) >= 8):
        raise ApprovalRefused("malformed: approval_id")
    if not _nonempty_str(record["task_id"]):
        raise ApprovalRefused("malformed: task_id")
    if not (isinstance(record["action_hash"], str) and HEX64.match(record["action_hash"])):
        raise ApprovalRefused("malformed: action_hash")
    if not (isinstance(record["nonce"], str) and NONCE.match(record["nonce"])):
        raise ApprovalRefused("malformed: nonce")
    if not (_int(record["issued_at"]) and _int(record["expires_at"])):
        raise ApprovalRefused("malformed timestamps")
    if not (_nonempty_str(record["issuer_key_id"]) and _nonempty_str(record["issuer_signature"])):
        raise ApprovalRefused("malformed: issuer fields")
    if "screenshot_ref" in record and not _nonempty_str(record["screenshot_ref"]):
        raise ApprovalRefused("malformed: screenshot_ref")


def verify(record: dict, action: BoundAction, now: int, ledger: ApprovalLedger,
           authenticator: Authenticator | None, effect_id: str) -> EffectIntent:
    """Return the recorded EffectIntent only if `record` is an authentic E1-issued
    approval of exactly `action`, issued at most 24 h ago and unexpired, used for
    the first time. Raises ApprovalRefused otherwise (nothing recorded)."""
    check_shape(record)
    if authenticator is None:
        raise ApprovalRefused("no trusted issuer configured")
    if not authenticator.verify(record["issuer_key_id"], signing_payload(record),
                                record["issuer_signature"]):
        raise ApprovalRefused("unauthenticated: issuer signature invalid")
    if record["schema"] != "potter.approval/v0":
        raise ApprovalRefused("unknown schema")
    if record["decided_by"] != "josh" or record["channel"] not in CHANNELS:
        raise ApprovalRefused("unauthorized decider or channel")
    if record["decision"] != "approve":
        raise ApprovalRefused("denied")
    if record["expires_at"] <= record["issued_at"]:
        raise ApprovalRefused("malformed window")
    deadline = min(record["expires_at"], record["issued_at"] + MAX_VALIDITY_S)
    if not record["issued_at"] <= now < deadline:
        raise ApprovalRefused("outside validity window (max 24 h)")
    if record["task_id"] != action.task_id:
        raise ApprovalRefused("task mismatch")
    if record["action_hash"] != action.hash():
        raise ApprovalRefused("action changed after approval")
    if action.capability in SCREENSHOT_CAPS and "screenshot_ref" not in record:
        raise ApprovalRefused("T38: browser/computer approval needs a screenshot")
    if not _nonempty_str(effect_id):
        raise ApprovalRefused("no effect id")
    intent = EffectIntent(effect_id, action.task_id, record["action_hash"], record["approval_id"],
                          action.canonical_json().decode("utf-8"))
    if not ledger.consume(record["nonce"], intent):
        raise ApprovalRefused("replay")
    return intent
