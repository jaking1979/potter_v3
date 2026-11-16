"""C1 PolicyEngine wired into the shadow turn loop.

The shadow install has exactly one grantable capability, ``chat.turn``. Every
capability that could send, buy, trade, write or control something is
registered FORBIDDEN, so no grant can ever allow it. The engine decides each
turn before any model call; its decision is traced durably before the turn
proceeds, and a trace failure denies the turn.
"""
from __future__ import annotations

import time
from typing import Callable

from policy import (ALLOW, Caller, Capability, CapabilityRegistry, PolicyEngine, PolicyRequest,
                    RiskClass)
from policy._contracts import approval as c0

R = RiskClass
PRINCIPAL = "potter"

CAPABILITIES = (
    Capability("chat.turn", R.READ, False, False),
    # Forbidden outright in the shadow install; registered so they are explicit.
    Capability("mail.send", R.FORBIDDEN, False, False),
    Capability("payments.transfer", R.FORBIDDEN, False, False),
    Capability("trade.place", R.FORBIDDEN, False, False),
    Capability("notion.write", R.FORBIDDEN, False, False),
    Capability("home.control", R.FORBIDDEN, False, False),
    Capability("secrets.export", R.FORBIDDEN, False, False),
)
GRANTS = {PRINCIPAL: frozenset({"chat.turn"})}


def make_engine(*, trace_hook, clock: Callable[[], float] | None = None, new_id=None) -> PolicyEngine:
    kw = dict(registry=CapabilityRegistry(CAPABILITIES), grants=GRANTS,
              ledger=c0.MemoryApprovalLedger(), authenticator=None,
              clock=clock or (lambda: int(time.time())), trace_hook=trace_hook)
    if new_id is not None:
        kw["new_id"] = new_id
    return PolicyEngine(**kw)


def engine_policy(engine: PolicyEngine) -> Callable:
    """The ShadowRuntime ``policy`` seam: True only for an ALLOW decision."""
    def policy(msg) -> bool:
        action = c0.BoundAction(msg.task_id, "chat.turn", "converse", "shadow:chat",
                                {"text_chars": len(msg.text)})
        decision = engine.decide(PolicyRequest(Caller(PRINCIPAL), "chat.turn", action, None, None,
                                               tuple(msg.provenance), None))
        return decision.outcome == ALLOW
    return policy
