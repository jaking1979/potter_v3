"""Thin C1 adapter (package C5). C1's public API is unchanged.

* ``build_policy_engine(resolver, registry=..., **c1_kwargs)`` constructs a
  ``policy.PolicyEngine`` whose ``grants`` come from the resolver.
* ``ScopedPolicyEngine`` wraps an engine with the same ``decide(request)``
  shape and, for capabilities whose binding says the target is a data
  resource, runs ``can_access`` on the target (deny roots, scope intersection
  down the chain) *before* C1. A resource denial reuses C1's existing
  ``not_granted`` reason (no new C1 reason codes); the identity-side code is
  available from ``resolver.can_access`` for C6 tracing.

Ancestry gate: ``ScopedPolicyEngine`` refuses to construct unless the
resolver has a trusted ``AncestryResolver`` injected, or ``test_mode=True``
is passed explicitly. Test mode only permits construction; resources that
are not positively outside every deny root are still denied
(``ancestry_unverified``). Caller-supplied ``Resource`` ancestry is never
trusted: it can only add denials.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from policy import DENY, REASONS, CapabilityRegistry, Decision, PolicyEngine, PolicyRequest
from policy.engine import _default_id

from .grants import GrantResolver
from .scopes import Resource, ScopeError


def build_policy_engine(resolver: GrantResolver, *, registry: CapabilityRegistry,
                        **c1_kwargs: Any) -> PolicyEngine:
    if "grants" in c1_kwargs:
        raise TypeError("grants come from the GrantResolver")
    if not isinstance(resolver, GrantResolver):
        raise TypeError("resolver must be a GrantResolver")
    grants = resolver.policy_grants(registry.names())
    return PolicyEngine(registry=registry, grants=grants, **c1_kwargs)


def _noop(record: Mapping[str, Any]) -> None:
    return None


class ScopedPolicyEngine:
    def __init__(self, engine: PolicyEngine, resolver: GrantResolver, *,
                 trace_hook: Callable[[Mapping[str, Any]], None] = _noop,
                 test_mode: bool = False):
        if not isinstance(resolver, GrantResolver):
            raise TypeError("resolver must be a GrantResolver")
        if not resolver.has_ancestry_resolver and test_mode is not True:
            raise ValueError("ScopedPolicyEngine needs a trusted AncestryResolver "
                             "(or explicit test_mode=True)")
        self._engine = engine
        self._resolver = resolver
        self._trace = trace_hook

    def decide(self, request: PolicyRequest, resource: Resource | None = None) -> Decision:
        try:
            code = self._resource_gate(request, resource)
        except Exception:
            code = "engine_error"
        if code is None:
            return self._engine.decide(request)
        c1_code = "engine_error" if code == "engine_error" else "not_granted"
        decision = Decision(_default_id("dec"), DENY, c1_code, REASONS[c1_code])
        try:
            self._trace({**decision.trace_record(), "identity_reason_code": code})
        except Exception:
            pass   # already a deny
        return decision

    def _resource_gate(self, request: Any, resource: Resource | None) -> str | None:
        if not isinstance(request, PolicyRequest):
            return None    # C1 rejects the shape itself
        binding = self._resolver.binding(request.capability)
        if binding is None or not binding.target_is_resource:
            return None
        if resource is None:
            try:
                resource = Resource(request.action.target)
            except ScopeError:
                return "malformed_resource"
        elif not isinstance(resource, Resource) or resource.resource_id != request.action.target:
            return "malformed_resource"
        if not binding.scope.covers_path(resource.segments):
            return "out_of_scope"
        access = self._resolver.can_access(request.caller.principal, request.caller.chain,
                                           resource, capability=request.capability)
        return None if access.allowed else access.reason_code
