"""Data-domain scopes, resources and deny roots (package C5, T11; T37 clause 5).

* A **resource** is a path ``<domain>/<store>/...`` (e.g. ``josh/mail/msg-1``,
  ``josh/notion/<page-id>``). It may carry caller-*claimed* ``ancestors``;
  claims can only add denials, never establish that a resource is safe.
* A **scope** is either an exact resource path or a subtree ``<prefix>/*``.
  ``*`` alone, unknown domains, empty / ``.`` / ``..`` segments are malformed.
* A **deny root** is an opaque resource ID (e.g. a Notion page ID). Any
  resource whose path, claimed ancestry or *trusted* ancestry contains it is
  denied, before any grant is consulted, and no grant can override it.
* Trusted ancestry comes only from an injected ``AncestryResolver`` at the
  enforcement boundary. A resource is positively outside every deny root only
  if its store is in ``outside_stores`` (stores whose connectors never address
  Notion pages) and no segment names a deny-root namespace. Anything else
  needs the resolver; with none configured, or when it does not know the
  resource, it is denied.

Nothing here contacts any external system.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

KNOWN_DOMAINS = frozenset({"josh", "household", "test", "system"})
_SEG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")
_HEXID = re.compile(r"^[0-9a-f]{32}$")
MAX_SEGMENTS = 16


class ScopeError(ValueError):
    pass


def _segments(path: object, *, allow_star: bool) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise ScopeError("path must be a non-empty string")
    segs = tuple(path.split("/"))
    if len(segs) > MAX_SEGMENTS or segs[0] not in KNOWN_DOMAINS:
        raise ScopeError("unknown domain")
    for i, s in enumerate(segs[1:], start=1):
        if s == "*" and allow_star and i == len(segs) - 1:
            continue
        if not _SEG.match(s) or s in (".", ".."):
            raise ScopeError("malformed segment")
    return segs


def canonical_opaque_id(value: str) -> str:
    """Canonical form for opaque IDs: Notion IDs compare without hyphens/case."""
    v = value.replace("-", "").lower()
    return v if _HEXID.match(v) else value


@dataclass(frozen=True)
class Scope:
    pattern: str

    def __post_init__(self):
        segs = _segments(self.pattern, allow_star=True)
        if segs[-1] == "*" and len(segs) < 2:
            raise ScopeError("bare wildcard is not a scope")
        object.__setattr__(self, "_segs", segs)

    @property
    def is_subtree(self) -> bool:
        return self._segs[-1] == "*"

    def _base(self) -> tuple[str, ...]:
        return self._segs[:-1] if self.is_subtree else self._segs

    def covers_path(self, segs: tuple[str, ...]) -> bool:
        base = self._base()
        if self.is_subtree:
            return len(segs) > len(base) and segs[:len(base)] == base
        return segs == base

    def covers_scope(self, other: "Scope") -> bool:
        """True iff every resource ``other`` covers is covered by ``self``."""
        if not self.is_subtree:
            return not other.is_subtree and other._segs == self._segs
        base, obase = self._base(), other._base()
        if other.is_subtree:
            return len(obase) >= len(base) and obase[:len(base)] == base
        return self.covers_path(obase)

    @property
    def domain(self) -> str:
        return self._segs[0]


@dataclass(frozen=True)
class Resource:
    """A resource as named by a caller. ``ancestors`` is an untrusted claim:
    it is consulted only to *deny*. There is deliberately no "verified" flag
    a caller could set."""
    resource_id: str
    ancestors: tuple[str, ...] = ()        # caller-claimed opaque ancestor IDs

    def __post_init__(self):
        segs = _segments(self.resource_id, allow_star=False)
        if not isinstance(self.ancestors, tuple) or not all(
                isinstance(a, str) and _SEG.match(a) for a in self.ancestors):
            raise ScopeError("ancestors must be a tuple of opaque ids")
        object.__setattr__(self, "_segs", segs)

    @property
    def segments(self) -> tuple[str, ...]:
        return self._segs

    @property
    def domain(self) -> str:
        return self._segs[0]


@dataclass(frozen=True)
class DenyRoot:
    """A subtree no Potter identity may reach. Matched by opaque ID."""
    root_id: str
    namespace: str            # resource segment naming the store, e.g. "notion"
    label: str

    def __post_init__(self):
        if not isinstance(self.root_id, str) or not _SEG.match(self.root_id):
            raise ScopeError("deny root id must be an opaque id")
        if not isinstance(self.namespace, str) or not _SEG.match(self.namespace):
            raise ScopeError("deny root namespace must be a segment")
        object.__setattr__(self, "root_id", canonical_opaque_id(self.root_id))

    def in_namespace(self, res: Resource) -> bool:
        return self.namespace in res.segments[1:]

    def contains(self, res: Resource, extra_ancestors: tuple[str, ...] = ()) -> bool:
        ids = [canonical_opaque_id(s) for s in res.segments[1:]]
        ids += [canonical_opaque_id(a) for a in res.ancestors + tuple(extra_ancestors)]
        return self.root_id in ids


class AncestryResolver(Protocol):
    """Trusted, injected at the enforcement boundary (e.g. the Notion
    connector's own page-tree lookup). Returns the *complete* opaque ancestor
    chain up to the workspace root, nearest first, or ``None`` if it cannot
    establish it. Never constructed from request data."""

    def ancestors_of(self, resource: Resource) -> tuple[str, ...] | None: ...


# Clinical EHR Notion root (AGENTS.md rule 11). Opaque ID only; never contacted.
CLINICAL_EHR_ROOT = DenyRoot("2fba667c631680c5a852c40b076e3c46", "notion", "clinical-ehr-root")
DEFAULT_DENY_ROOTS: tuple[DenyRoot, ...] = (CLINICAL_EHR_ROOT,)


# Stores whose connectors never address Notion pages. A Notion-backed store
# must use the ``notion`` store segment so it always goes through ancestry.
DEFAULT_OUTSIDE_STORES = frozenset({"mail", "calendar", "contacts", "files", "notes"})


def deny_root_code(res: Resource, roots: Iterable[DenyRoot],
                   ancestry: AncestryResolver | None,
                   outside_stores: frozenset[str] = DEFAULT_OUTSIDE_STORES) -> str | None:
    """Return a deny code unless ``res`` is positively outside every deny root."""
    roots = tuple(roots)
    if any(root.contains(res) for root in roots):
        return "deny_root"
    segs = res.segments
    positively_outside = (len(segs) >= 3 and segs[1] in outside_stores
                          and not any(root.in_namespace(res) for root in roots))
    if positively_outside:
        return None
    if ancestry is None:
        return "ancestry_unverified"
    chain = ancestry.ancestors_of(res)
    if not isinstance(chain, tuple) or not all(isinstance(a, str) and a for a in chain):
        return "ancestry_unverified"
    if any(root.contains(res, chain) for root in roots):
        return "deny_root"
    return None
