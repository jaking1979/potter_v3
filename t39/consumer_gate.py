"""T39 consumer gate: "the release builder and updater refuse any commit without
a valid receipt."

One function, `require_receipt`, that every consumer entry point calls before it
builds, deploys, updates, or rolls back to a commit. It composes three things
and fails closed on each:

1. The commit and tree the consumer is about to use, resolved by the consumer
   from git itself (not taken from the receipt or from the caller's claims).
2. A verifier (the production T39 verifier, injected) that checks both
   signatures, keys, checks, verdicts and expiry and returns the receipt's
   bound facts.
3. The durable nonce store, which must report first use.

Any exception, mismatch, missing receipt, unavailable store, or replay is a
refusal. A missing receipt on a main commit is additionally flagged as a
security incident (T39: "alerted, reverted, and blocks release").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from t39.nonce_store import Binding, NonceStore, Outcome

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class Refused(Exception):
    def __init__(self, reason: str, *, incident: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.incident = incident


@dataclass(frozen=True)
class Admitted:
    commit: str
    tree: str
    receipt_hash: str
    nonces: tuple[str, str]
    agents: Mapping[str, str]


class Verifier(Protocol):
    def __call__(self, receipt: object) -> Mapping[str, object]:
        """Verify the receipt completely; return its bound facts or raise.

        Required keys: commit, tree, receipt_hash, nonces (2-tuple), agents.
        It must not consume nonces; the gate does that once, after verifying.
        """


def require_receipt(
    *,
    commit: str,
    tree: str,
    load_receipt: Callable[[str], object | None],
    verify: Verifier,
    store: NonceStore,
    consumer: str,
    on_main: bool,
) -> Admitted:
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
        facts = dict(verify(receipt))
    except Exception as exc:
        raise Refused(f"receipt invalid: {type(exc).__name__}") from exc
    if facts.get("commit") != commit or facts.get("tree") != tree:
        raise Refused("receipt is for a different commit or tree", incident=on_main)
    nonces = facts.get("nonces")
    agents = facts.get("agents")
    try:
        binding = Binding(commit, tree, facts.get("receipt_hash"))  # type: ignore[arg-type]
        outcome = store.consume(nonces, binding, consumer)  # type: ignore[arg-type]
    except Exception as exc:
        raise Refused(f"nonce store refused: {type(exc).__name__}") from exc
    if outcome is not Outcome.CONSUMED:
        raise Refused(f"nonce already used ({outcome.value})")
    if not isinstance(agents, Mapping):
        # Nonce is now spent; that is deliberate. A malformed verifier result
        # must not become a second attempt with the same nonces.
        raise Refused("verifier returned no agent identities")
    return Admitted(commit, tree, binding.receipt_hash, nonces, dict(agents))  # type: ignore[arg-type]


def unreceipted_main_commits(main_commits: list[str], store: NonceStore) -> list[str]:
    """For the direct-push detector (T39 test 1): main commits with no consumed receipt.

    The detector runs at least hourly; anything returned is a security incident.
    A store error propagates so the detector reports itself failed, not clean.
    """
    receipted = store.commits_with_receipts(main_commits)
    return [c for c in main_commits if c not in receipted]
