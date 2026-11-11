"""PolicyEngine decision core (package C1, decisions T11/T12). Fail closed.

`PolicyEngine.decide(request)` returns exactly one of three outcomes:

* ``allow``     - every gate passed; if an approval was required/presented it
                  was verified by C0 ``verify()`` and its nonce consumed together
                  with the pending ``EffectIntent`` (returned on the decision).
* ``reconcile`` - a retry of an action whose approval nonce is already consumed,
                  carrying the *same* ``EffectIntent`` that the ledger recorded.
                  The caller must reconcile per T10 (read external state first);
                  this is never permission to re-execute.
* ``deny``      - anything else, including any exception inside the engine.

Gates, in order (first failure denies):

 1. Request shape (types, non-empty ids, canonical task UUID, capability
    name == action.capability).
 2. Clock read.
 3. Capability registry lookup (unknown -> deny).
 4. Forbidden risk class -> deny (an approval cannot unlock it).
 5. Delegation: the origin principal's grants come from engine configuration,
    never from the request; each delegation hop (sub-agent / cron / retry) can
    only narrow them. Effective grants = intersection of all of them.
 6. Live-state revalidation for stateful capabilities (C0 README: "a C1 gate"):
    non-empty preconditions, required keys present, and equal to the live state
    the caller just observed. Applies to fresh execution only: a retry carrying
    a consumed intent skips it and can only reach `reconcile` or `deny`
    (the effect may already have changed that state).
 7. Context provenance: trust is decided by the engine from source *kinds*;
    any unknown or untrusted source taints the request. An untrusted request
    for any write needs a verified approval.
 8. Approval: required by the capability (or by taint) -> must be present and
    pass C0 ``verify``; presented but not required -> must still verify (a bad
    approval is never silently ignored).
    Recovery after expiry: a retry carrying a consumed intent whose approval
    has since expired reconciles from the durable consumption row (see
    _recover_expired); it can never yield `allow`.

Redaction: reasons are fixed strings chosen from a closed table. Nothing from
the action's parameters, target, preconditions, the approval record, or
caller-chosen names is ever placed in ``reason`` or in ``trace_record()``.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ._contracts import approval as c0
from .registry import Capability, CapabilityRegistry, RiskClass, UnknownCapability

ALLOW, DENY, RECONCILE = "allow", "deny", "reconcile"
_DECISION_ID = re.compile(r"dec-[0-9a-f]{32}")

TRUSTED_SOURCES = frozenset({"principal_direct", "system_policy"})
HOP_KINDS = frozenset({"subagent", "cron", "retry"})
MAX_DELEGATION_DEPTH = 8

# Closed table of reason codes -> fixed, parameter-free text.
REASONS: Mapping[str, str] = MappingProxyType({
    "ok": "all policy gates passed",
    "ok_approved": "all policy gates passed; approval verified and consumed",
    "reconcile_pending_intent": "approval already consumed; route the recorded intent to reconciliation",
    "reconcile_after_expiry": "approval consumed before it expired; route the recorded intent to reconciliation",
    "malformed_request": "request is malformed",
    "invalid_task_id": "action task ID is not a canonical task UUID",
    "capability_mismatch": "requested capability does not match the bound action",
    "unknown_capability": "capability is not registered",
    "forbidden_capability": "capability is forbidden",
    "unknown_principal": "originating principal is not configured",
    "invalid_delegation": "delegation chain is invalid",
    "not_granted": "capability is outside the caller's effective grants",
    "preconditions_missing": "stateful capability requires live-state preconditions",
    "live_state_missing": "stateful capability requires the current live state",
    "stale_preconditions": "live state differs from the action's preconditions",
    "untrusted_context_requires_approval": "untrusted context cannot trigger a write without approval",
    "approval_required": "capability requires an approval",
    "approval_replayed": "approval nonce already consumed",
    "retry_without_consumption": "retry claims an intent but the approval was never consumed",
    "retry_intent_mismatch": "retry intent does not match the recorded intent",
    "approval_malformed": "approval record is malformed",
    "approval_unauthenticated": "approval record is not authentically issued",
    "approval_no_issuer": "no trusted approval issuer configured",
    "approval_unauthorized": "approval decider or channel is not authorized",
    "approval_denied": "approval decision is deny",
    "approval_window": "approval is outside its validity window (max 24 h)",
    "approval_task_mismatch": "approval was issued for a different task",
    "approval_action_mismatch": "action changed after approval",
    "approval_screenshot_missing": "browser/computer approval needs a screenshot (T38)",
    "approval_refused": "approval refused",
    "engine_error": "policy engine error; failing closed",
    "trace_error": "decision trace failed; failing closed",
})

# C0 ApprovalRefused messages -> fixed reason codes (never echo the message:
# some C0 messages include caller-supplied key names).
_C0_PREFIXES = (
    ("malformed", "approval_malformed"),
    ("unknown schema", "approval_malformed"),
    ("action must be", "approval_malformed"),
    ("unauthenticated", "approval_unauthenticated"),
    ("no trusted issuer", "approval_no_issuer"),
    ("unauthorized", "approval_unauthorized"),
    ("denied", "approval_denied"),
    ("outside validity", "approval_window"),
    ("task mismatch", "approval_task_mismatch"),
    ("action changed", "approval_action_mismatch"),
    ("T38", "approval_screenshot_missing"),
    ("replay", "approval_replayed"),
)


def _c0_code(exc: c0.ApprovalRefused) -> str:
    msg = str(exc)
    for prefix, code in _C0_PREFIXES:
        if msg.startswith(prefix):
            return code
    return "approval_refused"


class _Deny(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- types

@dataclass(frozen=True)
class DelegationHop:
    """One step away from the originating principal. `grants` can only narrow."""
    agent_id: str
    kind: str                  # one of HOP_KINDS
    grants: frozenset[str]


@dataclass(frozen=True)
class Caller:
    principal: str                                  # originating principal id
    chain: tuple[DelegationHop, ...] = ()


@dataclass(frozen=True)
class PolicyRequest:
    caller: Caller
    capability: str
    action: c0.BoundAction
    approval: Mapping[str, Any] | None = None
    live_state: Mapping[str, Any] | None = None     # preconditions as observed now
    provenance: tuple[str, ...] = ()                # source kinds that shaped this request
    retry_intent: c0.EffectIntent | None = None     # set only when retrying a consumed approval


@dataclass(frozen=True)
class Decision:
    decision_id: str
    outcome: str                                    # ALLOW / DENY / RECONCILE
    reason_code: str
    reason: str
    capability: str | None = None                   # registered name only, else None
    risk: str | None = None
    action_hash: str | None = None
    # Canonical task UUID from request.action.task_id (C2 task_id_for format),
    # or None when no valid one could be established.
    task_id: str | None = None
    # Holds action parameters: never logged, never in repr, never traced.
    intent: c0.EffectIntent | None = field(default=None, repr=False, compare=False)

    def final_record(self) -> dict[str, Any]:
        """Superseding record emitted to final_hook after a trace failure."""
        return {"decision_id": self.decision_id, "final_outcome": self.outcome,
                "reason": self.reason_code, "supersedes": "candidate",
                "task_id": self.task_id,
                "correlation": "request" if self.task_id is not None else "missing"}

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def trace_record(self) -> dict[str, Any]:
        """Redaction-safe record for C6 tracing. `correlation` is "request" when
        `task_id` is a validated canonical UUID from the request's BoundAction,
        and "missing" (task_id None) otherwise: never guessed, never omitted."""
        return {"decision_id": self.decision_id, "outcome": self.outcome,
                "task_id": self.task_id,
                "correlation": "request" if self.task_id is not None else "missing",
                "reason_code": self.reason_code, "capability": self.capability,
                "risk": self.risk, "action_hash": self.action_hash,
                "effect_id": self.intent.effect_id if self.intent else None}


class _ProbeLedger:
    """Ledger that refuses every consumption but remembers that C0 verify
    reached it, i.e. that every check *except* single use passed."""
    def __init__(self):
        self.reached = False
        self.nonce: str | None = None

    def consume(self, nonce: str, intent: c0.EffectIntent) -> bool:
        self.reached, self.nonce = True, nonce
        return False

    def pending(self, nonce: str):
        return None


def _canonical_task_id(value: Any) -> str | None:
    """The canonical UUID string, or None. Same rule as C6 DecisionTrace:
    str(UUID(v)) == v (lowercase, hyphenated); C2 task_id_for emits this form."""
    if not isinstance(value, str):
        return None
    try:
        return value if str(uuid.UUID(value)) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _request_task_id(request: Any) -> str | None:
    """Correlation from the request itself (frozen PolicyRequest -> frozen
    BoundAction), never from caller closures or other shared state."""
    if isinstance(request, PolicyRequest) and isinstance(request.action, c0.BoundAction):
        return _canonical_task_id(request.action.task_id)
    return None


def _default_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _noop_trace(record: Mapping[str, Any]) -> None:
    return None


# --------------------------------------------------------------------------- engine

class PolicyEngine:
    def __init__(self, *, registry: CapabilityRegistry, grants: Mapping[str, frozenset[str]],
                 ledger: c0.ApprovalLedger, authenticator: c0.Authenticator | None,
                 clock: Callable[[], int],
                 verifier: Callable[..., c0.EffectIntent] = c0.verify,
                 new_id: Callable[[str], str] = _default_id,
                 trace_hook: Callable[[Mapping[str, Any]], None] = _noop_trace,
                 final_hook: Callable[[Mapping[str, Any]], None] | None = None):
        self._registry = registry
        self._grants = MappingProxyType({p: frozenset(g) for p, g in grants.items()})
        self._ledger = ledger
        self._auth = authenticator
        self._clock = clock
        self._verify = verifier
        self._new_id = new_id
        self._trace = trace_hook
        self._final = final_hook
        self._failures = 0
        self._failures_lock = threading.Lock()

    @property
    def trace_failures(self) -> int:
        """Count of failed hook calls (trace_hook or final_hook). Any increase
        means a trace record may be missing or superseded without a durable
        final record; ops should alert on it."""
        with self._failures_lock:
            return self._failures

    def _count_failure(self) -> None:
        with self._failures_lock:
            self._failures += 1

    # ---- public ----------------------------------------------------------------

    def decide(self, request: PolicyRequest) -> Decision:
        try:
            decision_id = self._new_id("dec")
            # C6 joins traces on this exact format; anything else falls back.
            if not isinstance(decision_id, str) or not _DECISION_ID.fullmatch(decision_id):
                raise TypeError
        except Exception:
            decision_id = _default_id("dec")
        try:
            task_id = _request_task_id(request)
        except Exception:
            task_id = None
        try:
            decision = self._decide(decision_id, request)
        except _Deny as d:
            decision = self._make(decision_id, DENY, d.code)
        except Exception:
            decision = self._make(decision_id, DENY, "engine_error")
        decision = dataclasses.replace(decision, task_id=task_id)
        # The returned Decision is the ONLY execution authority. trace_hook
        # records are candidate evaluations: a hook may durably commit a
        # candidate (even "allow") and then raise.
        try:
            self._trace(decision.trace_record())
        except Exception:
            self._count_failure()
            # Any consumed nonce stays recorded as a pending intent in the ledger,
            # so C2 recovery reconciles it (T10): nothing is stranded or replayed.
            decision = self._make(decision_id, DENY, "trace_error",
                                  capability=decision.capability, risk=decision.risk,
                                  action_hash=decision.action_hash, task_id=task_id)
            # One best-effort superseding record so the audit can't be left
            # holding only the candidate. Audit completeness for trace_error
            # denials depends on final_hook; its failure is swallowed (the
            # decision stays deny) but counted.
            if self._final is not None:
                try:
                    self._final(decision.final_record())
                except Exception:
                    self._count_failure()
        return decision

    # ---- core ------------------------------------------------------------------

    @staticmethod
    def _make(decision_id: str, outcome: str, code: str, **kw) -> Decision:
        return Decision(decision_id, outcome, code, REASONS[code], **kw)

    def _decide(self, decision_id: str, req: PolicyRequest) -> Decision:
        self._check_shape(req)
        now = self._clock()
        if not isinstance(now, int) or isinstance(now, bool):
            raise _Deny("engine_error")
        try:
            cap = self._registry.get(req.capability)
        except UnknownCapability:
            raise _Deny("unknown_capability")
        if not isinstance(cap, Capability) or cap.name != req.capability:
            raise _Deny("engine_error")
        ctx = {"capability": cap.name, "risk": cap.risk.value, "action_hash": req.action.hash()}

        def deny(code: str) -> Decision:
            return self._make(decision_id, DENY, code, **ctx)

        if cap.risk is RiskClass.FORBIDDEN:
            return deny("forbidden_capability")
        code = self._check_delegation(req.caller, cap.name)
        if code:
            return deny(code)
        # A retry carrying a consumed intent is routed to reconciliation before
        # the live-state gate: the original effect may already have succeeded
        # and changed the very state its preconditions bound, so requiring the
        # old preconditions here would strand the intent. This path can only
        # yield `reconcile` (exact recorded intent, authentic approval,
        # consumed nonce) or `deny`, never `allow`.
        if req.retry_intent is None:
            code = self._check_live_state(cap, req)
            if code:
                return deny(code)

        untrusted = self._untrusted(req.provenance)
        needs_approval = cap.approval_required or (untrusted and cap.risk is not RiskClass.READ)

        if req.approval is None:
            if req.retry_intent is not None:
                return deny("retry_without_consumption")
            if needs_approval:
                return deny("untrusted_context_requires_approval" if untrusted
                            else "approval_required")
            return self._make(decision_id, ALLOW, "ok", **ctx)

        outcome, code, intent = self._check_approval(req, now)
        return self._make(decision_id, outcome, code, intent=intent, **ctx)

    # ---- gates -----------------------------------------------------------------

    @staticmethod
    def _check_shape(req: Any) -> None:
        if not isinstance(req, PolicyRequest):
            raise _Deny("malformed_request")
        c = req.caller
        if not isinstance(c, Caller) or not isinstance(c.principal, str) or not c.principal:
            raise _Deny("malformed_request")
        if not isinstance(c.chain, tuple):
            raise _Deny("malformed_request")
        if not isinstance(req.capability, str) or not req.capability:
            raise _Deny("malformed_request")
        if not isinstance(req.action, c0.BoundAction) or not req.action.task_id:
            raise _Deny("malformed_request")
        if _canonical_task_id(req.action.task_id) is None:
            raise _Deny("invalid_task_id")
        if req.action.capability != req.capability:
            raise _Deny("capability_mismatch")
        if not isinstance(req.provenance, tuple) or not all(
                isinstance(s, str) and s for s in req.provenance):
            raise _Deny("malformed_request")
        if req.approval is not None and not isinstance(req.approval, Mapping):
            raise _Deny("malformed_request")
        if req.live_state is not None and not isinstance(req.live_state, Mapping):
            raise _Deny("malformed_request")
        if req.retry_intent is not None and not isinstance(req.retry_intent, c0.EffectIntent):
            raise _Deny("malformed_request")

    def _check_delegation(self, caller: Caller, capability: str) -> str | None:
        root = self._grants.get(caller.principal)
        if root is None:
            return "unknown_principal"
        if len(caller.chain) > MAX_DELEGATION_DEPTH:
            return "invalid_delegation"
        effective = set(root)
        for hop in caller.chain:
            if (not isinstance(hop, DelegationHop) or not isinstance(hop.agent_id, str)
                    or not hop.agent_id or hop.kind not in HOP_KINDS
                    or not isinstance(hop.grants, frozenset)
                    or not all(isinstance(g, str) for g in hop.grants)):
                return "invalid_delegation"
            effective &= hop.grants
        return None if capability in effective else "not_granted"

    @staticmethod
    def _check_live_state(cap: Capability, req: PolicyRequest) -> str | None:
        if not cap.stateful:
            return None
        pre = req.action.preconditions
        if not pre or any(k not in pre for k in cap.required_preconditions):
            return "preconditions_missing"
        if req.live_state is None:
            return "live_state_missing"
        try:
            same = c0._canon(dict(req.live_state)) == c0._canon(dict(pre))
        except (TypeError, ValueError):
            return "malformed_request"
        return None if same else "stale_preconditions"

    @staticmethod
    def _untrusted(provenance: tuple[str, ...]) -> bool:
        # No provenance is not evidence of trust.
        return not provenance or any(s not in TRUSTED_SOURCES for s in provenance)

    def _check_approval(self, req: PolicyRequest, now: int):
        # Private snapshot so the caller cannot mutate the record between the
        # probe and the consuming verification.
        try:
            record = json.loads(c0._canon(dict(req.approval)))
        except (TypeError, ValueError):
            return DENY, "approval_malformed", None

        # 1) Probe: run every C0 check without consuming anything.
        probe = _ProbeLedger()
        try:
            self._verify(record, req.action, now, probe, self._auth, "probe")
        except c0.ApprovalRefused as e:
            if not (probe.reached and str(e).startswith("replay")):
                code = _c0_code(e)
                if code == "approval_window" and req.retry_intent is not None:
                    return self._recover_expired(record, req)
                return DENY, code, None
        else:
            raise RuntimeError("verifier accepted without consuming")  # -> engine_error
        nonce = probe.nonce   # from C0's authenticated snapshot

        # 2) Already consumed? Then only reconciliation of the same intent.
        prior = self._ledger.pending(nonce)
        if prior is not None or req.retry_intent is not None:
            if prior is None:
                return DENY, "retry_without_consumption", None
            if req.retry_intent is None:
                return DENY, "approval_replayed", None
            if not self._intent_matches(prior, req, record):
                return DENY, "retry_intent_mismatch", None
            return RECONCILE, "reconcile_pending_intent", prior

        # 3) Fresh: consume atomically with the pending intent.
        effect_id = self._new_id("eff")
        try:
            intent = self._verify(record, req.action, now, self._ledger, self._auth, effect_id)
        except c0.ApprovalRefused as e:
            return DENY, _c0_code(e), None
        if not isinstance(intent, c0.EffectIntent) or intent.action_hash != req.action.hash():
            raise RuntimeError("verifier returned an unexpected intent")
        return ALLOW, "ok_approved", intent

    @staticmethod
    def _intent_matches(prior: c0.EffectIntent, req: PolicyRequest, record: Mapping) -> bool:
        """The retry carries exactly the durably recorded intent, for exactly
        this action and this approval. EffectIntent.action() re-checks the
        stored canonical bytes against the approved hash (tamper -> refuse)."""
        try:
            stored_action = prior.action()
        except c0.ApprovalRefused:
            return False
        return (req.retry_intent == prior and prior.action_hash == req.action.hash()
                and prior.action_hash == record.get("action_hash")
                and prior.approval_id == record.get("approval_id")
                and prior.task_id == req.action.task_id and stored_action == req.action)

    def _recover_expired(self, record: Mapping, req: PolicyRequest):
        """Recovery of an approval that was consumed while valid and has since
        expired (outage past expires_at or the 24 h cap). Only `reconcile` or
        `deny`, never `allow`.

        Authority is the trusted durable consumption record: the ledger row
        that C0 verify() wrote only after full validation, holding the intent
        whose canonical action is integrity-checked against the approved hash.
        The approval record must still authenticate against the pinned issuer
        key and match every C0 claim EXCEPT the validity window; that only
        locates the row (by its signed nonce) and binds it to this action.

        Limits (documented): if the issuer key is rotated out or revoked from
        the authenticator, this path denies (approval_unauthenticated); C2
        journal recovery, which reads the row directly, is the backstop.
        Revoking an approval after consumption cannot un-consume it;
        reconciliation reads external state first (T10) and starts no new effect.
        """
        try:
            c0.check_shape(record)
        except c0.ApprovalRefused:
            return DENY, "approval_malformed", None
        if self._auth is None:
            return DENY, "approval_no_issuer", None
        if not self._auth.verify(record["issuer_key_id"], c0.signing_payload(record),
                                 record["issuer_signature"]):
            return DENY, "approval_unauthenticated", None
        if record["schema"] != "potter.approval/v0":
            return DENY, "approval_malformed", None
        if record["decided_by"] != "josh" or record["channel"] not in c0.CHANNELS:
            return DENY, "approval_unauthorized", None
        if record["decision"] != "approve":
            return DENY, "approval_denied", None
        if record["task_id"] != req.action.task_id:
            return DENY, "approval_task_mismatch", None
        if record["action_hash"] != req.action.hash():
            return DENY, "approval_action_mismatch", None
        prior = self._ledger.pending(record["nonce"])
        if prior is None:
            # Never consumed while valid: an expired approval cannot start anything.
            return DENY, "approval_window", None
        if not self._intent_matches(prior, req, record):
            return DENY, "retry_intent_mismatch", None
        return RECONCILE, "reconcile_after_expiry", prior
