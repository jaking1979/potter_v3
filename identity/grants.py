"""Grant model and GrantResolver (package C5, T11; T37 clause 5).

A ``Grant`` gives one principal a set of capabilities over a set of
data-domain scopes. It authorizes exactly the pairs ``capabilities x scopes``
*of that grant*. The resolver keeps every grant as (capability, scope) pairs
end to end; two independent grants never combine into a pair that neither
authorized (Dolores review, PR #26).

A ``CapabilityBinding`` says which data scope a capability touches.

* ``policy_grants()`` -> the ``{principal_id: frozenset[capability]}`` mapping
  C1's ``PolicyEngine`` consumes. A capability is included only if a single
  pair grants it on a scope covering its bound scope. So a test-domain
  principal can never hold a ``josh/*`` capability.
* ``delegation_hop()`` -> a C1 ``DelegationHop`` whose grants come from the
  hop principal's *configured* pairs (never from the request), optionally
  narrowed. C1 intersects names; because every name is derived from pairs
  against the same per-capability bound scope, the intersection is pairwise.
* ``can_access(principal, chain, resource, capability=None)`` -> resource
  check. Deny roots first (trusted ancestry only from the injected
  ``AncestryResolver``); then the set of capabilities that cover the resource
  is computed per level and intersected down the chain (origin, each hop
  principal, each hop's own narrowing). Allowed iff a capability survives
  every level (the requested one, if given). Hops can only narrow.

Fail closed throughout: unknown or disabled principals, malformed grants (the
whole principal is quarantined, not partially granted), malformed chains or
resources, and any internal exception all deny.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from policy import DelegationHop

from .principals import (HOP_PRINCIPAL_KINDS, Principal, PrincipalDirectory, PrincipalKind,
                         valid_principal_id)
from .scopes import (DEFAULT_DENY_ROOTS, DEFAULT_OUTSIDE_STORES, AncestryResolver, DenyRoot,
                     Resource, Scope, ScopeError, deny_root_code)

HOP_KINDS = frozenset(HOP_PRINCIPAL_KINDS)
MAX_DELEGATION_DEPTH = 8   # mirrors policy.engine.MAX_DELEGATION_DEPTH

ACCESS_REASONS: Mapping[str, str] = MappingProxyType({
    "ok": "a capability covering the resource survives every level of the chain",
    "deny_root": "resource is beneath a deny root",
    "ancestry_unverified": "resource is not positively outside every deny root",
    "malformed_resource": "resource is malformed",
    "unknown_principal": "principal is not configured",
    "principal_disabled": "principal is disabled",
    "not_a_caller": "principal kind cannot originate requests",
    "principal_quarantined": "principal has a malformed grant; all access denied",
    "invalid_delegation": "delegation chain is invalid",
    "out_of_scope": "resource is outside the effective scope",
    "engine_error": "identity resolver error; failing closed",
})

Pair = tuple[str, Scope]


class GrantError(ValueError):
    pass


def _name_set(values: Any, what: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (set, frozenset, tuple, list)):
        raise GrantError(f"{what} must be a collection of strings")
    out = frozenset(values)
    if not all(isinstance(v, str) and v for v in out):
        raise GrantError(f"{what} must be non-empty strings")
    return out


def _scope_set(values: Any) -> frozenset[Scope]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (set, frozenset, tuple, list)):
        raise GrantError("scopes must be a collection")
    try:
        return frozenset(s if isinstance(s, Scope) else Scope(s) for s in values)
    except ScopeError as e:
        raise GrantError("malformed scope") from e


@dataclass(frozen=True)
class Grant:
    """Authorizes ``capabilities x scopes`` of this grant only."""
    principal_id: str
    capabilities: frozenset[str]
    scopes: frozenset[Scope]

    def __post_init__(self):
        if not valid_principal_id(self.principal_id):
            raise GrantError("grant principal id is malformed")
        object.__setattr__(self, "capabilities", _name_set(self.capabilities, "capabilities"))
        object.__setattr__(self, "scopes", _scope_set(self.scopes))

    @property
    def pairs(self) -> frozenset[Pair]:
        return frozenset((c, s) for c in self.capabilities for s in self.scopes)

    @classmethod
    def from_config(cls, raw: Mapping[str, Any]) -> "Grant":
        if not isinstance(raw, Mapping) or set(raw) != {"principal", "capabilities", "scopes"}:
            raise GrantError("grant config needs exactly principal, capabilities, scopes")
        return cls(raw["principal"], raw["capabilities"], raw["scopes"])


@dataclass(frozen=True)
class CapabilityBinding:
    """Which data scope a C1 capability touches.

    ``target_is_resource``: the action's ``target`` names a resource in that
    scope (e.g. a file path), so the adapter also checks it with
    ``can_access``. False for targets that are not data resources (an email
    recipient).
    """
    capability: str
    scope: Scope
    target_is_resource: bool = False

    def __post_init__(self):
        if not isinstance(self.capability, str) or not self.capability:
            raise GrantError("binding capability must be a name")
        if not isinstance(self.scope, Scope):
            try:
                object.__setattr__(self, "scope", Scope(self.scope))
            except ScopeError as e:
                raise GrantError("malformed binding scope") from e
        if not isinstance(self.target_is_resource, bool):
            raise GrantError("target_is_resource must be bool")


@dataclass(frozen=True)
class Hop:
    """Identity-side delegation hop. ``scopes`` / ``capabilities`` (optional)
    narrow further; ``None`` means "no extra narrowing" on that axis."""
    agent_id: str
    kind: str
    scopes: frozenset[Scope] | None = None
    capabilities: frozenset[str] | None = None


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason_code: str
    reason: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "reason", ACCESS_REASONS[self.reason_code])


def _deny(code: str) -> AccessDecision:
    return AccessDecision(False, code)


@dataclass(frozen=True)
class _Resolved:
    principal: Principal
    pairs: frozenset[Pair]


class GrantResolver:
    def __init__(self, directory: PrincipalDirectory, grants: Iterable[Any],
                 bindings: Iterable[CapabilityBinding],
                 deny_roots: Iterable[DenyRoot] = DEFAULT_DENY_ROOTS, *,
                 ancestry: AncestryResolver | None = None,
                 outside_stores: Iterable[str] = DEFAULT_OUTSIDE_STORES):
        if not isinstance(directory, PrincipalDirectory):
            raise GrantError("directory must be a PrincipalDirectory")
        self._dir = directory
        roots = tuple(deny_roots)
        if not all(isinstance(r, DenyRoot) for r in roots):
            raise GrantError("deny roots must be DenyRoot")
        # The clinical EHR root cannot be configured away.
        self._roots = tuple(dict.fromkeys(DEFAULT_DENY_ROOTS + roots))
        if ancestry is not None and not callable(getattr(ancestry, "ancestors_of", None)):
            raise GrantError("ancestry must be an AncestryResolver")
        self._ancestry = ancestry
        self._outside = _name_set(outside_stores, "outside_stores")
        if any(r.namespace in self._outside for r in self._roots):
            raise GrantError("a deny-root namespace cannot be an outside store")

        table: dict[str, CapabilityBinding] = {}
        for b in bindings:
            if not isinstance(b, CapabilityBinding) or b.capability in table:
                raise GrantError("bindings must be unique CapabilityBinding")
            table[b.capability] = b
        self._bindings: Mapping[str, CapabilityBinding] = MappingProxyType(table)

        pairs: dict[str, set[Pair]] = {}
        quarantined: set[str] = set()
        for raw in grants:
            try:
                g = raw if isinstance(raw, Grant) else Grant.from_config(raw)
            except Exception:
                pid = raw.get("principal") if isinstance(raw, Mapping) else getattr(
                    raw, "principal_id", None)
                if isinstance(pid, str):
                    quarantined.add(pid)
                continue
            p = self._dir.get(g.principal_id)
            if p is None:
                continue   # grants for unknown principals confer nothing
            if p.kind is PrincipalKind.TEST and any(s.domain != "test" for s in g.scopes):
                quarantined.add(g.principal_id)   # test principals live in test/* only
                continue
            pairs.setdefault(g.principal_id, set()).update(g.pairs)
        self._quarantined = frozenset(quarantined)
        self._resolved: Mapping[str, _Resolved] = MappingProxyType({
            pid: _Resolved(self._dir.get(pid), frozenset(ps)) for pid, ps in pairs.items()})

    # ---- configuration -----------------------------------------------------------

    @property
    def deny_roots(self) -> tuple[DenyRoot, ...]:
        return self._roots

    @property
    def has_ancestry_resolver(self) -> bool:
        return self._ancestry is not None

    def binding(self, capability: str) -> CapabilityBinding | None:
        return self._bindings.get(capability) if isinstance(capability, str) else None

    def _usable(self, pid: str) -> _Resolved | None:
        r = self._resolved.get(pid)
        if r is None or pid in self._quarantined or not r.principal.enabled:
            return None
        return r

    def _pairs_of(self, pid: str) -> frozenset[Pair]:
        r = self._usable(pid)
        return r.pairs if r is not None else frozenset()

    # ---- capability view (feeds C1) -------------------------------------------

    def capabilities_for(self, principal_id: str,
                         registered: frozenset[str] | None = None) -> frozenset[str]:
        """Capabilities with a single granted pair covering their bound scope."""
        r = self._usable(principal_id)
        if r is None or r.principal.kind is PrincipalKind.SERVICE_ACCOUNT:
            return frozenset()
        out = set()
        for cap, scope in r.pairs:
            b = self._bindings.get(cap)
            if b is None or (registered is not None and cap not in registered):
                continue
            if scope.covers_scope(b.scope):
                out.add(cap)
        return frozenset(out)

    def policy_grants(self, registered: frozenset[str] | None = None
                      ) -> Mapping[str, frozenset[str]]:
        """The ``grants`` mapping for ``policy.PolicyEngine``. Only principals
        that may originate requests appear; anyone else is ``unknown_principal``
        to C1."""
        out = {}
        for pid, r in self._resolved.items():
            if self._usable(pid) is not None and r.principal.can_originate:
                out[pid] = self.capabilities_for(pid, registered)
        return MappingProxyType(out)

    def delegation_hop(self, agent_id: str, kind: str,
                       narrow_to: Iterable[str] | None = None) -> DelegationHop:
        """A C1 ``DelegationHop`` carrying the hop principal's configured
        ceiling (optionally narrowed). Unknown / wrong-kind principals get an
        empty grant set, so C1 denies."""
        ceiling = frozenset()
        p = self._dir.get(agent_id)
        if p is not None and kind in HOP_KINDS and p.kind in HOP_PRINCIPAL_KINDS[kind]:
            ceiling = self.capabilities_for(agent_id)
        if narrow_to is not None:
            ceiling &= frozenset(narrow_to)
        return DelegationHop(agent_id, kind, ceiling)

    # ---- resource view ---------------------------------------------------------

    def can_access(self, principal_id: str, chain: Iterable[Any],
                   resource: Resource | str, capability: str | None = None) -> AccessDecision:
        try:
            return self._can_access(principal_id, tuple(chain), resource, capability)
        except Exception:
            return _deny("engine_error")

    def _can_access(self, principal_id, chain, resource, capability) -> AccessDecision:
        # 1. Resource shape, then deny roots: before any principal or grant.
        if isinstance(resource, str):
            try:
                resource = Resource(resource)
            except ScopeError:
                return _deny("malformed_resource")
        if not isinstance(resource, Resource):
            return _deny("malformed_resource")
        code = deny_root_code(resource, self._roots, self._ancestry, self._outside)
        if code:
            return _deny(code)
        if capability is not None and not isinstance(capability, str):
            return _deny("out_of_scope")

        # 2. Origin principal.
        p = self._dir.get(principal_id)
        if p is None:
            return _deny("unknown_principal")
        if not p.enabled:
            return _deny("principal_disabled")
        if not p.can_originate:
            return _deny("not_a_caller")
        if principal_id in self._quarantined:
            return _deny("principal_quarantined")

        segs = resource.segments

        # A capability counts only where its binding touches this resource and a
        # single granted pair covers it; unbound capabilities confer nothing.
        def covering(pid: str) -> set[str]:
            return {c for c, s in self._pairs_of(pid) if s.covers_path(segs)
                    and (b := self._bindings.get(c)) is not None and b.scope.covers_path(segs)}

        # 3. Pairwise intersection: capabilities covering the resource at every level.
        live = covering(principal_id)
        if len(chain) > MAX_DELEGATION_DEPTH:
            return _deny("invalid_delegation")
        for hop in chain:
            agent_id, kind = getattr(hop, "agent_id", None), getattr(hop, "kind", None)
            if not isinstance(agent_id, str) or kind not in HOP_KINDS:
                return _deny("invalid_delegation")
            hp = self._dir.get(agent_id)
            if hp is None or not hp.enabled or hp.kind not in HOP_PRINCIPAL_KINDS[kind]:
                return _deny("invalid_delegation")
            if agent_id in self._quarantined:
                return _deny("principal_quarantined")
            live &= covering(agent_id)
            if isinstance(hop, Hop):
                if hop.scopes is not None:
                    if not isinstance(hop.scopes, frozenset) or not all(
                            isinstance(s, Scope) for s in hop.scopes):
                        return _deny("invalid_delegation")
                    if not any(s.covers_path(segs) for s in hop.scopes):
                        live = set()
                if hop.capabilities is not None:
                    if not isinstance(hop.capabilities, frozenset) or not all(
                            isinstance(c, str) for c in hop.capabilities):
                        return _deny("invalid_delegation")
                    live &= hop.capabilities
        if capability is not None:
            b = self._bindings.get(capability)
            if b is None or not b.scope.covers_path(segs):
                return _deny("out_of_scope")
            live &= {capability}
        return AccessDecision(True, "ok") if live else _deny("out_of_scope")
