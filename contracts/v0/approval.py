"""Reference verifier for ApprovalRecord v0 (package C0).

Contract only: C1 (PolicyEngine) implements the production check. This module
fixes the semantics both sides test against:

* Provenance: a record is evidence of Josh's approval only if it carries a valid
  issuer signature from the trusted T19 issuer (E1). Fields such as
  `decided_by` and `channel` are claims *inside* that signed envelope, never
  proof by themselves. No authenticator configured -> refuse.
* Binding: the approval covers exactly sha256(canonical BoundAction).
* Single use: the nonce is consumed through a NonceStore whose consume_once()
  must be atomic. Production (C1/C2) consumes it in the same durable transaction
  that records the effect intent in the effect journal, so a crash or a
  concurrent consumer cannot use one approval twice.
* Fail closed: any malformed, unauthenticated, stale, mismatched or reused record
  raises ApprovalRefused, and nothing is consumed unless every check passes.

Stdlib only, deterministic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

REQUIRED = ("schema", "approval_id", "task_id", "action_hash", "decision",
            "decided_by", "channel", "issued_at", "expires_at", "nonce",
            "issuer_key_id", "issuer_signature")
CHANNELS = {"telegram", "potter_home", "passkey_step_up"}
SCREENSHOT_CAPS = {"browser", "computer"}
ALLOWED_KEYS = set(REQUIRED) | {"screenshot_ref"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[0-9a-f]{32,}$")


class ApprovalRefused(Exception):
    """Any reason the action may not proceed. Callers treat every exception
    from verify(), including unexpected ones, as deny."""


class Authenticator(Protocol):
    """Trusted-issuance boundary. Production: the E1 issuer's signing key, with
    the verifier holding only what it needs to verify (selected under T11/T12)."""
    def verify(self, key_id: str, payload: bytes, signature: str) -> bool: ...


class NonceStore(Protocol):
    def consume_once(self, nonce: str) -> bool:
        """Atomically mark `nonce` used. True only for the first caller, ever,
        across threads, processes and restarts (production: durable, C2)."""
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


class MemoryNonceStore:
    """Reference single-process store (atomic via lock, not durable)."""
    def __init__(self):
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def consume_once(self, nonce: str) -> bool:
        with self._lock:
            if nonce in self._used:
                return False
            self._used.add(nonce)
            return True

    def __contains__(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._used


@dataclass(frozen=True)
class BoundAction:
    task_id: str
    capability: str          # registry capability, e.g. "gmail.send", "computer"
    operation: str
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def canonical_json(self) -> bytes:
        body = {"task_id": self.task_id, "capability": self.capability,
                "operation": self.operation, "target": self.target,
                "parameters": self.parameters}
        return _canon(body)

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


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


def verify(record: dict, action: BoundAction, now: int,
           nonces: NonceStore, authenticator: Authenticator | None) -> None:
    """Pass silently only if `record` is an authentic E1-issued approval of exactly
    `action`, valid now, used for the first time. Raises ApprovalRefused otherwise."""
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
    if not record["issued_at"] <= now < record["expires_at"]:
        raise ApprovalRefused("outside validity window")
    if record["task_id"] != action.task_id:
        raise ApprovalRefused("task mismatch")
    if record["action_hash"] != action.hash():
        raise ApprovalRefused("action changed after approval")
    if action.capability in SCREENSHOT_CAPS and "screenshot_ref" not in record:
        raise ApprovalRefused("T38: browser/computer approval needs a screenshot")
    if not nonces.consume_once(record["nonce"]):
        raise ApprovalRefused("replay")
