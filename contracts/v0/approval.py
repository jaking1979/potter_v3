"""Reference verifier for ApprovalRecord v0 (package C0).

Contract only: C1 (PolicyEngine) implements the production check against the
durable nonce store. This module fixes the semantics both sides test against:
the approval binds the exact action, is single-use, expires, and fails closed.
Stdlib only, deterministic.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, MutableSet

REQUIRED = ("schema", "approval_id", "task_id", "action_hash", "decision",
            "decided_by", "channel", "issued_at", "expires_at", "nonce")
CHANNELS = {"telegram", "potter_home", "passkey_step_up"}
SCREENSHOT_CAPS = {"browser", "computer"}


class ApprovalRefused(Exception):
    """Raised for any reason the action may not proceed. Callers treat every
    exception from verify(), including unexpected ones, as deny."""


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
        return json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


def verify(record: dict, action: BoundAction, now: int, consumed: MutableSet[str]) -> None:
    """Pass silently only if `record` approves exactly `action`, now, for the first
    time. Marks the nonce consumed on success. Raises ApprovalRefused otherwise."""
    missing = [k for k in REQUIRED if k not in record]
    if missing:
        raise ApprovalRefused(f"malformed: missing {missing}")
    if record["schema"] != "potter.approval/v0":
        raise ApprovalRefused("unknown schema")
    if record["decided_by"] != "josh" or record["channel"] not in CHANNELS:
        raise ApprovalRefused("unauthorized issuer or channel")
    if record["decision"] != "approve":
        raise ApprovalRefused("denied")
    if not (isinstance(record["issued_at"], int) and isinstance(record["expires_at"], int)):
        raise ApprovalRefused("malformed timestamps")
    if record["expires_at"] <= record["issued_at"]:
        raise ApprovalRefused("malformed window")
    if not record["issued_at"] <= now < record["expires_at"]:
        raise ApprovalRefused("outside validity window")
    if record["task_id"] != action.task_id:
        raise ApprovalRefused("task mismatch")
    if record["action_hash"] != action.hash():
        raise ApprovalRefused("action changed after approval")
    if action.capability in SCREENSHOT_CAPS and not record.get("screenshot_ref"):
        raise ApprovalRefused("T38: browser/computer approval needs a screenshot")
    if record["nonce"] in consumed:
        raise ApprovalRefused("replay")
    consumed.add(record["nonce"])
