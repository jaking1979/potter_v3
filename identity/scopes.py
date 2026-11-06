"""Data-domain scopes, resources and deny roots (package C5, T11; T37 clause 5).

* A **resource** is a path ``<domain>/<seg>/...`` (e.g. ``josh/mail/msg-1``,
  ``josh/notion/<page-id>``) plus optional declared ancestors for stores whose
  hierarchy is not visible in the ID (Notion pages).
* A **scope** is either an exact resource path or a subtree ``<prefix>/*``.
  ``*`` alone, unknown domains, empty / ``.`` / ``..`` segments are malformed.
* A **deny root** is an opaque resource ID (e.g. a Notion page ID). Any
  resource whose path or declared ancestry contains it is denied, before any
  grant is consulted, and no grant can override it. Resources in a deny root's
  namespace (e.g. ``*/notion/*``) must also carry *verified* ancestry, or they
  are denied: an unverifiable Notion page might sit beneath the EHR root.

Nothing here contacts any external system.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

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
    resource_id: str
    ancestors: tuple[str, ...] = ()        # opaque ancestor IDs, nearest first
    ancestry_verified: bool = False        # set only by a connector that resolved it

    def __post_init__(self):
        segs = _segments(self.resource_id, allow_star=False)
        if not isinstance(self.ancestors, tuple) or not all(
                isinstance(a, str) and _SEG.match(a) for a in self.ancestors):
            raise ScopeError("ancestors must be a tuple of opaque ids")
        if not isinstance(self.ancestry_verified, bool):
            raise ScopeError("ancestry_verified must be bool")
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

    def contains(self, res: Resource) -> bool:
        ids = [canonical_opaque_id(s) for s in res.segments[1:]]
        ids += [canonical_opaque_id(a) for a in res.ancestors]
        return self.root_id in ids


# Clinical EHR Notion root (AGENTS.md rule 11). Opaque ID only; never contacted.
CLINICAL_EHR_ROOT = DenyRoot("2fba667c631680c5a852c40b076e3c46", "notion", "clinical-ehr-root")
DEFAULT_DENY_ROOTS: tuple[DenyRoot, ...] = (CLINICAL_EHR_ROOT,)


def deny_root_code(res: Resource, roots: Iterable[DenyRoot]) -> str | None:
    """Return a deny code if ``res`` is (or may be) beneath a deny root."""
    roots = tuple(roots)
    for root in roots:
        if root.contains(res):
            return "deny_root"
    for root in roots:
        if root.in_namespace(res) and not res.ancestry_verified:
            return "ancestry_unverified"
    return None
