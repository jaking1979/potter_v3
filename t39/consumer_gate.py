"""T39 consumer gates.

`admit_merge`  — CI/merge admission. Verifies the attestation pair and ISSUES
                 the receipt (the one place nonces are consumed).
`require_issued` — release builder, host updater, rollback, manual deploy.
                 Each independently re-verifies the signed receipt (T39: "CI
                 and the host gate both verify receipts"), then requires the
                 exact issued record in the registry, then logs the use. It
                 never consumes nonces, so CI -> builder -> host -> rollback
                 all succeed on one receipt, while a never-issued or
                 mismatched receipt fails everywhere.
`unreceipted_main_commits` — detector input; reads issued receipts only.

Every path fails closed: any exception, mismatch, missing receipt or
unavailable registry is a refusal. A missing or mismatched receipt for a
`main` commit is flagged as a security incident.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from t39.receipt_registry import Facts, Issue, ReceiptRegistry

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")

Verifier = Callable[[object], Mapping[str, object]]
"""Fully verifies a receipt (signatures, distinct keys, roles, verdicts, checks,
expiry, revocation policy) and returns its facts; raises on any failure.
Required keys: commit, tree, receipt_hash, nonces, agents, key_fingerprints,
verifier_version. It must not consume or record anything."""


class Refused(Exception):
    def __init__(self, reason: str, *, incident: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.incident = reason, incident


@dataclass(frozen=True)
class Admitted:
    commit: str
    receipt_hash: str
    agents: dict


def _verified(commit: str, tree: str, load_receipt: Callable[[str], object | None],
              verify: Verifier, on_main: bool) -> Facts:
    if not (isinstance(commit, str) and _HEX40.fullmatch(commit)
            and isinstance(tree, str) and _HEX40.fullmatch(tree)):
        raise Refused("consumer must pass the 40-hex commit and tree it resolved from git")
    try:
        receipt = load_receipt(commit)
    except Exception as exc:
        raise Refused(f"receipt lookup failed: {type(exc).__name__}") from exc
    if receipt is None:
        raise Refused("unreceipted commit", incident=on_main)
    try:
        raw = dict(verify(receipt))
        facts = Facts(
            commit=raw["commit"], tree=raw["tree"], receipt_hash=raw["receipt_hash"],
            nonces=tuple(raw["nonces"]) if isinstance(raw["nonces"], (list, tuple)) else None,
            agents=dict(raw["agents"]), key_fingerprints=dict(raw["key_fingerprints"]),
            verifier_version=raw["verifier_version"])
    except Exception as exc:
        raise Refused(f"receipt invalid: {type(exc).__name__}") from exc
    if facts.commit != commit or facts.tree != tree:
        raise Refused("receipt is for a different commit or tree", incident=on_main)
    return facts


def admit_merge(*, commit: str, tree: str, load_receipt, verify: Verifier,
                registry: ReceiptRegistry, issuer: str,
                now: datetime | None = None) -> Admitted:
    facts = _verified(commit, tree, load_receipt, verify, on_main=False)
    try:
        outcome = registry.issue(facts, issuer, now or datetime.now(timezone.utc))
    except Exception as exc:
        raise Refused(f"registry refused: {type(exc).__name__}") from exc
    if outcome is Issue.REPLAY:
        raise Refused("attestation nonce already used for a different receipt")
    # ISSUED, or ALREADY_ISSUED with byte-identical canonical facts (issuer retry
    # after a crash): either way the one issued record matches this candidate.
    return Admitted(facts.commit, facts.receipt_hash, dict(facts.agents))


def require_issued(*, commit: str, tree: str, load_receipt, verify: Verifier,
                   registry: ReceiptRegistry, consumer: str, purpose: str, on_main: bool,
                   now: datetime | None = None) -> Admitted:
    facts = _verified(commit, tree, load_receipt, verify, on_main)
    try:
        issued = registry.get(facts.receipt_hash)
    except Exception as exc:
        raise Refused(f"registry unavailable: {type(exc).__name__}") from exc
    if issued is None:
        raise Refused("receipt was never issued at merge admission", incident=on_main)
    if issued.facts != facts.canonical():
        raise Refused("issued receipt does not match the presented receipt", incident=on_main)
    try:
        registry.record_use(facts.receipt_hash, consumer, purpose,
                            now or datetime.now(timezone.utc))
    except Exception as exc:
        raise Refused(f"could not record use: {type(exc).__name__}") from exc
    return Admitted(facts.commit, facts.receipt_hash, dict(facts.agents))


def unreceipted_main_commits(main_commits: list[str], registry: ReceiptRegistry) -> list[str]:
    """Main commits with no issued receipt; registry errors propagate (detector fails loud)."""
    receipted = registry.receipted_commits(main_commits)
    return [c for c in main_commits if c not in receipted]
