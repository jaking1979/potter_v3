# Identity + service grants v0 (package C5, T11; T37 clause 5)

Who may act, on which data, through which delegation chain. Fail closed. Offline: nothing here contacts any service or holds a secret.

## Model

| Piece | Meaning |
| --- | --- |
| `Principal` | Stable typed ID `<kind>:<name>`: `human:` / `agent:` / `job:` / `test:` / `svc:`. The prefix must match the kind. `human:christie` is seeded **disabled** (placeholder). |
| `ServiceAccount` | One per connector (`svc:gmail`, …). Holds a `SecretRef` (`secretref://<store>/<name>`), never a secret value. `SecretRef` has no resolve method. |
| `Scope` | Data-domain scope: exact path or subtree `<prefix>/*`. Domains: `josh`, `household`, `test`, `system`. A bare `*`, unknown domains and `.`/`..`/empty segments are malformed. |
| `Resource` | Path `<domain>/<store>/…` plus optional caller-*claimed* `ancestors`. Claims can only add denials. There is no "verified" flag. |
| `AncestryResolver` | Trusted, **injected** at the enforcement boundary (the connector's own page-tree lookup). Returns the complete ancestor chain, or `None`. It is the only source of ancestry that can clear a resource. |
| `DenyRoot` | Opaque ID whose subtree no identity can reach. Checked **first**, before any principal or grant. The clinical EHR Notion root `2fba667c631680c5a852c40b076e3c46` is seeded and always re-added: a configuration can't remove it. A resource clears deny roots only if it is **positively outside** them: its store is in `outside_stores` (default `mail`, `calendar`, `contacts`, `files`, `notes`), and no segment names a deny-root namespace. Otherwise the injected resolver must return a chain without the root. With no resolver, or when the resolver doesn't know the resource, it is denied (`ancestry_unverified`). |
| `Grant` | Principal → capabilities × scopes **of that grant only**. It is stored as (capability, scope) pairs end to end, so two independent grants never combine into a pair neither authorized. |
| `CapabilityBinding` | C1 capability → the data scope it touches (+ whether `action.target` is a resource). |

## GrantResolver

- `policy_grants(registered)` returns the `grants` mapping C1's `PolicyEngine` consumes. A capability is included only if it is bound, registered, and a **single granted pair** covers its bound scope. A test principal therefore never holds a `josh/*` capability, even if a grant names it.
- `delegation_hop(agent_id, kind, narrow_to)` returns a C1 `DelegationHop` with the hop principal's *configured* ceiling, optionally narrowed. An unknown or wrong-kind principal gets an empty set.
- `can_access(principal, chain, resource, capability=None)` checks, in order:
  1. Resource shape.
  2. Deny roots, using trusted ancestry only.
  3. The origin principal: known, enabled, can originate, not quarantined.
  4. The chain: depth ≤ 8, and the hop kind must match the principal kind (`subagent`→agent/test, `cron`→job/test, `retry`→agent/job/test).

  At each level (the origin, each hop principal, and each hop's optional `scopes` / `capabilities` narrowing), it computes the set of capabilities whose granted pair covers the resource **and** whose binding touches it. It then intersects those sets down the chain. Access is allowed only if a capability survives every level, and it must be the requested one if one was named. Hops narrow pairs, not unions.

Fail closed:

- Unknown principals and disabled principals are denied.
- A malformed grant **quarantines the whole principal**, so it never gets a partial grant.
- A test principal with a non-`test/*` scope is quarantined.
- Unbound and unregistered capabilities are dropped.
- Any exception returns `engine_error`.

## C1 adapter (C1 API unchanged)

- `build_policy_engine(resolver, registry=…, **c1_kwargs)` builds a `PolicyEngine` whose grants come from the resolver. A caller-supplied `grants` is rejected.
- `ScopedPolicyEngine(engine, resolver, *, test_mode=False).decide(request, resource=None)` runs `can_access(..., capability=request.capability)` on `action.target` before C1, for bindings with `target_is_resource=True`.
- **Ancestry gate:** construction raises unless the resolver has an injected `AncestryResolver`. The only exception is an explicit `test_mode=True`. Test mode only permits construction. It is for offline tests and fixtures, and it never weakens a decision: anything not positively outside the deny roots is still denied.
- A resource denial reuses C1's existing `not_granted` reason. The identity-side code goes only to the trace hook, which never includes the target.

## Not in this package

- Grant configuration for real capabilities (C8 wiring).
- Secret resolution and key custody.
- A real `AncestryResolver`, which is a Notion-connector concern. Per AGENTS.md rule 11, access tests must prove the credential can't reach the root before Notion access is trusted.

Nothing here enables runtime behavior (T22).
