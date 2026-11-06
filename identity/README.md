# Identity + service grants v0 (package C5, T11; T37 clause 5)

Who may act, on which data, through which delegation chain. Fail closed. Offline: nothing here contacts any service or holds a secret.

## Model

| Piece | Meaning |
| --- | --- |
| `Principal` | Stable typed ID `<kind>:<name>`: `human:` / `agent:` / `job:` / `test:` / `svc:`. The prefix must match the kind. `human:christie` is seeded **disabled** (placeholder). |
| `ServiceAccount` | One per connector (`svc:gmail`, …). Holds a `SecretRef` (`secretref://<store>/<name>`), never a secret value. `SecretRef` has no resolve method. |
| `Scope` | Data-domain scope: exact path or subtree `<prefix>/*`. Domains: `josh`, `household`, `test`, `system`. A bare `*`, unknown domains and `.`/`..`/empty segments are malformed. |
| `Resource` | Path plus optional opaque `ancestors` and `ancestry_verified` (for stores like Notion where hierarchy isn't in the ID). |
| `DenyRoot` | Opaque ID whose subtree no identity can reach. Checked **first**, before any principal or grant. The clinical EHR Notion root `2fba667c631680c5a852c40b076e3c46` is seeded and always re-added: a configuration can't remove it. Resources in a deny root's namespace (`*/notion/*`) also need verified ancestry, or they are denied. |
| `Grant` | Principal → capabilities + scopes. |
| `CapabilityBinding` | C1 capability → the data scope it touches (+ whether `action.target` is a resource). |

## GrantResolver

- `policy_grants(registered)` returns the `grants` mapping C1's `PolicyEngine` consumes. A capability is included only if it is granted, bound, registered, and its bound scope is covered by the principal's scopes. A test principal therefore never holds a `josh/*` capability, even if a grant names it.
- `delegation_hop(agent_id, kind, narrow_to)` returns a C1 `DelegationHop` with the hop principal's *configured* ceiling, optionally narrowed. An unknown or wrong-kind principal gets an empty set.
- `can_access(principal, chain, resource)` checks, in order: resource shape, deny roots, the origin principal (known, enabled, can originate, not quarantined), then the chain (depth ≤ 8, hop kind matches principal kind: `subagent`→agent/test, `cron`→job/test, `retry`→agent/job/test). The resource must be covered at **every** level: the origin, each hop principal's ceiling, and each hop's own narrowing. Hops can only narrow.

Fail closed:

- Unknown principals and disabled principals are denied.
- A malformed grant **quarantines the whole principal**, so it never gets a partial grant.
- A test principal with a non-`test/*` scope is quarantined.
- Unbound and unregistered capabilities are dropped.
- Any exception returns `engine_error`.

## C1 adapter (C1 API unchanged)

- `build_policy_engine(resolver, registry=…, **c1_kwargs)` builds a `PolicyEngine` whose grants come from the resolver. A caller-supplied `grants` is rejected.
- `ScopedPolicyEngine(engine, resolver).decide(request, resource=None)` runs `can_access` on `action.target` before C1, for bindings with `target_is_resource=True`. A denial reuses C1's existing `not_granted` reason. The identity-side code goes only to the trace hook, which never includes the target.

## Not in this package

- Grant configuration for real capabilities (C8 wiring).
- Secret resolution and key custody.
- Live Notion ancestry resolution. A connector must set `ancestry_verified`, and per AGENTS.md rule 11, access tests must prove the credential can't reach the root before Notion access is trusted.

Nothing here enables runtime behavior (T22).
