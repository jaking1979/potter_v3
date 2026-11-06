"""C5 identity + service grants (T11; T37 clause 5). Fail closed. No runtime wiring."""
from .adapter import ScopedPolicyEngine, build_policy_engine
from .grants import (ACCESS_REASONS, AccessDecision, CapabilityBinding, Grant, GrantError,
                     GrantResolver, Hop)
from .principals import (IdentityError, Principal, PrincipalDirectory, PrincipalKind, SecretRef,
                         ServiceAccount)
from .scopes import (CLINICAL_EHR_ROOT, DEFAULT_OUTSIDE_STORES, AncestryResolver, DEFAULT_DENY_ROOTS, DenyRoot, Resource, Scope,
                     ScopeError)
from .seed import seed_directory

__all__ = ["ScopedPolicyEngine", "build_policy_engine", "ACCESS_REASONS", "AccessDecision",
           "CapabilityBinding", "Grant", "GrantError", "GrantResolver", "Hop", "IdentityError",
           "Principal", "PrincipalDirectory", "PrincipalKind", "SecretRef", "ServiceAccount",
           "CLINICAL_EHR_ROOT", "DEFAULT_OUTSIDE_STORES", "AncestryResolver", "DEFAULT_DENY_ROOTS", "DenyRoot", "Resource", "Scope",
           "ScopeError", "seed_directory"]
