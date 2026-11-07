"""Fencing epoch and node promotion (package C4, decision T35 item 10).

C2 checks leases and in-flight windows inside the journal DB only, so a
dispatcher that stalls past its window (GC pause, VM freeze, partition) and then
resumes can still land an effect after another node has taken over. C4 closes
that with a monotonic **fencing epoch**:

* :class:`EpochAuthority` is a small SQLite file that stands in for the shared
  authority (in Phase F, whatever survives the loss of either container). It
  holds one row ``(epoch, holder)``. :meth:`EpochAuthority.promote` is a
  compare-and-set: it increments the epoch only if the caller saw the current
  one, so concurrent promotions have exactly one winner and epochs never repeat.
* :class:`Node` is one runtime instance. Every activation takes a fresh epoch,
  so an older incarnation of the same node is fenced like any other node.
  Lifecycle::

      start ──▶ FENCED ──rejoin() (authority still names us)──┐
                  │                                           ▼
                  └──promote() (takeover, CAS)────────────▶ PAUSED ──unpause(reconcile)──▶ ACTIVE
      any state ──authority epoch moved past ours──▶ FENCED

  A node always starts FENCED: a returning node does not assume it is still
  primary, and a node restored from a replica (:class:`FileCopyReplicator`) can
  leave FENCED only by promoting. A promoted node starts PAUSED: it may not
  dispatch until :meth:`Node.unpause` has reconciled every ``unknown`` effect in
  its journal (C2 ``unresolved()``).
* :class:`FencedEffects` wraps :class:`~runtime.effects.EffectJournal`. Every
  dispatch carries the node's :class:`FencingToken`; it is checked before the
  journal is touched, inside the journal's claim transaction, and just before
  the external call. The token is passed to ``perform(effect, token)`` so the
  connector can check it too.
* :class:`StubConnector` is the external-side check ("fencing token at the
  boundary"). Its state lives in the authority's DB, and the check plus the
  apply run inside :meth:`EpochAuthority.boundary`, the same ``BEGIN
  IMMEDIATE`` lock that promotion takes. It refuses any token below the
  highest epoch it has seen, or not equal to the committed epoch. Promotion and
  apply are therefore strictly ordered: once a promotion commits, no older
  token can land, and the new primary's reconcile sees everything that did.

Limits (documented, not hidden): third-party APIs cannot check our tokens, so
the boundary in production is an egress gateway with this check, and a window
remains between the gateway's check and the third party applying the call. The
deterministic C2 idempotency key, passed to the third party, is the backstop
for that window. Staging-only (T22); nothing here performs a real effect.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol

from .db import Database, as_database
from .effects import (DEFAULT_DISPATCH_TIMEOUT_S, UNKNOWN, Effect, EffectConflict, EffectError,
                      adapter_name,
                      EffectFailed, EffectInFlight, EffectJournal)

FENCED, PAUSED, ACTIVE = "fenced", "paused", "active"
NODE_STATES = (FENCED, PAUSED, ACTIVE)

_META_EPOCH = "fencing.epoch"
_META_NODE = "fencing.node_id"
_META_RESTORED = "fencing.restored_from"
_OWNER_RE = re.compile(r"@epoch:(\d+)$")


# --------------------------------------------------------------------------- errors

class FencingError(EffectError):
    pass


class StaleEpoch(EffectFailed, FencingError):
    """Refused because the dispatching epoch is not the authority's current one.
    Subclasses EffectFailed: a refused dispatch definitely did not apply."""


class StaleEpochRefused(StaleEpoch):
    """Refused at the external boundary (connector side)."""


class NodeNotActive(FencingError):
    """The node is FENCED or PAUSED; it may not dispatch effects."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


class PromotionConflict(FencingError):
    """Another node promoted first (compare-and-set lost)."""


class UnresolvedEffects(FencingError):
    """Unpause refused: these effects are still ``unknown`` after reconciliation."""

    def __init__(self, keys: list[str]):
        super().__init__(f"{len(keys)} effect(s) still unresolved; node stays paused")
        self.keys = keys


class AuthorityUnavailable(FencingError):
    """The epoch authority cannot be read; callers fail closed."""


# --------------------------------------------------------------------------- authority

@dataclass(frozen=True)
class Epoch:
    epoch: int
    holder: str | None


@dataclass(frozen=True)
class FencingToken:
    node_id: str
    epoch: int

    @property
    def owner(self) -> str:
        """The C2 in-flight owner string; records the epoch with the dispatch."""
        return f"{self.node_id}@epoch:{self.epoch}"


def owner_epoch(owner: str | None) -> int:
    """Epoch encoded in a C2 in-flight owner; 0 for pre-C4 (untagged) owners."""
    m = _OWNER_RE.search(owner or "")
    return int(m.group(1)) if m else 0


class EpochAuthority:
    """Monotonic epoch with compare-and-set promotion, in one SQLite file."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 busy_timeout_s: float = 30.0):
        self.path = str(path)
        self._clock = clock
        self._timeout = busy_timeout_s
        try:
            with self._tx() as c:
                c.execute("CREATE TABLE IF NOT EXISTS authority ("
                          " id INTEGER PRIMARY KEY CHECK (id = 1),"
                          " epoch INTEGER NOT NULL CHECK (epoch >= 0),"
                          " holder TEXT, updated_at REAL NOT NULL)")
                c.execute("CREATE TABLE IF NOT EXISTS promotions ("
                          " epoch INTEGER PRIMARY KEY, holder TEXT NOT NULL,"
                          " promoted_at REAL NOT NULL)")
                c.execute("INSERT OR IGNORE INTO authority (id, epoch, holder, updated_at)"
                          " VALUES (1, 0, NULL, ?)", (self._clock(),))
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"epoch authority unusable: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        return c

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        finally:
            c.close()

    @contextlib.contextmanager
    def boundary(self) -> Iterator[tuple[sqlite3.Connection, Epoch]]:
        """One ``BEGIN IMMEDIATE`` transaction on the authority DB, with the
        committed epoch read inside it. :meth:`promote` takes the same write
        lock, so whatever runs in this block linearizes with promotion: no
        promotion can commit between the epoch read and the block's commit.
        The external boundary (:class:`StubConnector`) keeps its state in this
        DB and checks + applies here."""
        try:
            with self._tx() as c:
                row = c.execute("SELECT epoch, holder FROM authority WHERE id = 1").fetchone()
                if row is None:
                    raise AuthorityUnavailable("epoch authority has no row")
                yield c, Epoch(int(row[0]), row[1])
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"epoch authority boundary failed: {exc}") from exc

    def current(self) -> Epoch:
        try:
            c = self._connect()
            try:
                row = c.execute("SELECT epoch, holder FROM authority WHERE id = 1").fetchone()
            finally:
                c.close()
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"epoch authority unreadable: {exc}") from exc
        if row is None:
            raise AuthorityUnavailable("epoch authority has no row")
        return Epoch(int(row[0]), row[1])

    def promote(self, node_id: str, expected_epoch: int) -> int:
        """CAS: move to ``expected_epoch + 1`` held by ``node_id`` only if the
        authority is still at ``expected_epoch``. Returns the new epoch."""
        if not node_id or not isinstance(node_id, str):
            raise ValueError("node_id required")
        if not isinstance(expected_epoch, int) or isinstance(expected_epoch, bool) \
                or expected_epoch < 0:
            raise ValueError("expected_epoch must be a non-negative int")
        now = self._clock()
        try:
            with self._tx() as c:
                cur = c.execute("UPDATE authority SET epoch = epoch + 1, holder = ?,"
                                " updated_at = ? WHERE id = 1 AND epoch = ?",
                                (node_id, now, expected_epoch))
                if cur.rowcount != 1:
                    raise PromotionConflict(
                        f"authority moved past epoch {expected_epoch}; promotion lost")
                c.execute("INSERT INTO promotions (epoch, holder, promoted_at) VALUES (?,?,?)",
                          (expected_epoch + 1, node_id, now))
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"promotion failed: {exc}") from exc
        return expected_epoch + 1

    def history(self) -> list[Epoch]:
        c = self._connect()
        try:
            rows = c.execute("SELECT epoch, holder FROM promotions ORDER BY epoch").fetchall()
        finally:
            c.close()
        return [Epoch(int(e), h) for e, h in rows]


# --------------------------------------------------------------------------- boundary registry

DEFAULT_FENCING_SEMANTICS = (
    "refuses any token below the epoch authority's committed epoch; the check and"
    " the external apply are one transaction linearized with promotion")


@dataclass(frozen=True)
class FencedBoundaryContract:
    """A reviewed connector boundary that PROVABLY refuses stale fencing tokens.

    Mirrors C2's ``ConnectorContract``: ``adapter`` is the exact
    ``perform(effect, token)`` callable reviewed against the guarantee,
    ``contract_ref`` points at the evidence, ``fencing_semantics`` states the
    guarantee. ``contract_digest`` is sha256 over the canonical JSON of
    (kind, identity, contract_ref, adapter qualified name, semantics). It is
    recorded on each effect when it is dispatched through
    :class:`FencedEffects`, so a rotated or re-mapped contract stops vouching
    for effects dispatched under the old one."""
    identity: str
    adapter: Callable[[Effect, "FencingToken"], Any]
    contract_ref: str
    fencing_semantics: str = DEFAULT_FENCING_SEMANTICS
    contract_digest: str = field(init=False)

    def __post_init__(self):
        if not (isinstance(self.identity, str) and self.identity):
            raise ValueError("identity required")
        if not (isinstance(self.contract_ref, str) and self.contract_ref):
            raise ValueError("contract_ref required")
        if not (isinstance(self.fencing_semantics, str) and self.fencing_semantics):
            raise ValueError("fencing_semantics required")
        if not callable(self.adapter):
            raise TypeError("adapter must be callable")
        canon = json.dumps(["potter.c4.fenced_boundary/v0", self.identity, self.contract_ref,
                            adapter_name(self.adapter), self.fencing_semantics],
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        object.__setattr__(self, "contract_digest", hashlib.sha256(canon).hexdigest())


class FencedBoundaryRegistry(Mapping):
    """Frozen identity -> FencedBoundaryContract mapping, given to a
    :class:`Node` at construction. Empty by default: then no effect is ever
    cleared by the fence, and C2 keeps ambiguous effects unresolved."""

    def __init__(self, contracts: Iterable[FencedBoundaryContract] = ()):
        entries: dict[str, FencedBoundaryContract] = {}
        for k in contracts:
            if not isinstance(k, FencedBoundaryContract):
                raise TypeError("registry entries must be FencedBoundaryContract")
            if k.identity in entries:
                raise ValueError(f"duplicate boundary identity {k.identity!r}")
            entries[k.identity] = k
        self._entries = MappingProxyType(entries)

    def __getitem__(self, identity: str) -> FencedBoundaryContract:
        return self._entries[identity]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __setattr__(self, name, value):
        if name != "_entries" or "_entries" in self.__dict__:
            raise AttributeError("FencedBoundaryRegistry is frozen")
        object.__setattr__(self, name, value)


_BINDINGS_DDL = ("CREATE TABLE IF NOT EXISTS fenced_bindings ("
                 " idempotency_key TEXT PRIMARY KEY, connector TEXT NOT NULL,"
                 " boundary_digest TEXT, bound_at REAL NOT NULL)")


# --------------------------------------------------------------------------- node

def _meta_get(db: Database, key: str) -> str | None:
    with db.read() as c:
        row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(c: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        c.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                  " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


@dataclass(frozen=True)
class Observation:
    current: bool              # authority names this node at the epoch it holds
    authority: Epoch
    held_epoch: int | None
    restored_from: str | None


class Node:
    """One runtime instance. Always starts FENCED (see module docstring).

    The epoch a node last held is persisted in its own journal DB (``meta``), so
    a returning node knows which epoch it had, and a replica restored from that
    DB carries the marker that keeps it fenced."""

    def __init__(self, node_id: str, authority: EpochAuthority, db: Database | str | Path,
                 *, clock: Callable[[], float] = time.time,
                 dispatch_timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
                 boundaries: FencedBoundaryRegistry | None = None):
        if not node_id or not isinstance(node_id, str) or "@" in node_id:
            raise ValueError("node_id required (no '@')")
        if boundaries is None:
            boundaries = FencedBoundaryRegistry()
        if not isinstance(boundaries, FencedBoundaryRegistry):
            raise TypeError("boundaries must be a FencedBoundaryRegistry")
        self.boundaries = boundaries   # fixed for this node's lifetime
        self.node_id = node_id
        self.authority = authority
        self.db = as_database(db)
        with self.db.tx() as c:   # C4-owned table in the journal DB (replicated with it)
            c.execute(_BINDINGS_DDL)
        self.journal = EffectJournal(self.db, clock=clock, dispatch_timeout_s=dispatch_timeout_s,
                                     fence=self.c2_fence)
        self._clock = clock
        self._reconciling = False
        # (idempotency_key, owner judged, decision) for every C2 fence call: audit trail
        self.fence_log: list[tuple[str, str | None, bool]] = []
        held_node = _meta_get(self.db, _META_NODE)
        held = _meta_get(self.db, _META_EPOCH)
        self._epoch: int | None = int(held) if held is not None and held_node == node_id else None
        self._restored = _meta_get(self.db, _META_RESTORED)
        self._state = FENCED
        self.fence_reason = ("restored from replica" if self._restored
                             else "returning node" if self._epoch is not None else "new node")

    # ---- state --------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def epoch(self) -> int | None:
        return self._epoch

    def token(self) -> FencingToken:
        if self._epoch is None:
            raise NodeNotActive(FENCED, "node holds no epoch")
        return FencingToken(self.node_id, self._epoch)

    def _fence(self, reason: str) -> None:
        self._state, self.fence_reason = FENCED, reason

    def observe(self) -> Observation:
        """Read the authority. Demotes to FENCED if the epoch moved past ours."""
        auth = self.authority.current()
        current = (self._epoch is not None and auth.epoch == self._epoch
                   and auth.holder == self.node_id and not self._restored)
        if not current and self._state != FENCED:
            self._fence(f"authority at epoch {auth.epoch}, node held {self._epoch}")
        return Observation(current, auth, self._epoch, self._restored)

    def is_current(self, token: FencingToken | None = None) -> bool:
        """True if ``token`` (default: ours) is the authority's current epoch."""
        token = token or (FencingToken(self.node_id, self._epoch)
                          if self._epoch is not None else None)
        if token is None:
            return False
        auth = self.authority.current()
        return auth.epoch == token.epoch and auth.holder == token.node_id

    # ---- transitions --------------------------------------------------------

    def rejoin(self) -> bool:
        """A returning node leaves FENCED only if the authority still names it at
        the epoch it last held, and then it takes a FRESH epoch (CAS), so any
        stalled earlier incarnation of itself is fenced too. Never steals
        leadership: a superseded or restored node stays FENCED (returns False)."""
        obs = self.observe()
        if not obs.current:
            self._fence("superseded" if not self._restored else "restored from replica")
            return False
        self._take_epoch(obs.authority.epoch)
        return True

    def promote(self) -> int:
        """Takeover: CAS the authority from the epoch we observe to the next one.
        The node starts PAUSED. Raises PromotionConflict if another node won."""
        auth = self.authority.current()
        return self._take_epoch(auth.epoch)

    def _take_epoch(self, expected: int) -> int:
        try:
            new = self.authority.promote(self.node_id, expected)
        except PromotionConflict:
            self._fence("promotion lost")
            raise
        with self.db.tx() as c:
            _meta_set(c, _META_NODE, self.node_id)
            _meta_set(c, _META_EPOCH, str(new))
            _meta_set(c, _META_RESTORED, None)
        self._epoch, self._restored = new, None
        self._state, self.fence_reason = PAUSED, ""
        return new

    def pause(self) -> None:
        if self._state == ACTIVE:
            self._state = PAUSED

    def unpause(self, reconcile: Callable[[Effect], str], *,
                now: float | None = None) -> dict[str, Any]:
        """Reconcile every ``unknown`` effect, then go ACTIVE.

        Live in-flight windows owned by a LOWER epoch, on a proven fenced
        boundary, are ended first (the owner stays on the row): the promotion is committed, so the boundary already
        refuses that dispatcher. C2 then accepts a ``failed`` verdict only
        because :meth:`c2_fence` confirms the earlier dispatcher's epoch is
        older than ours. Windows of our own epoch are respected. If anything is still unknown, the node
        stays PAUSED and :class:`UnresolvedEffects` is raised. ``pending``
        effects (never dispatched) need no reconciliation."""
        if self._state != PAUSED:
            raise NodeNotActive(self._state, f"unpause needs PAUSED, node is {self._state}")
        token = self.token()
        if not self.is_current(token):
            self._fence("superseded before unpause")
            raise NodeNotActive(FENCED, "authority moved past this node's epoch")
        released, resolved, blocked = [], [], []
        self._reconciling = True
        try:
            for eff in self.journal.unresolved():
                if eff.status != UNKNOWN:
                    continue
                if eff.in_flight_owner and owner_epoch(eff.in_flight_owner) < token.epoch \
                        and self._boundary_proven(eff):   # unproven: keep the window
                    if self.journal.expire_in_flight(eff.idempotency_key, eff.in_flight_owner,
                                                     now=now):
                        released.append(eff.idempotency_key)
                try:
                    after = self.journal.reconcile(eff.idempotency_key, reconcile, now=now)
                except EffectInFlight:
                    blocked.append(eff.idempotency_key)
                    continue
                (blocked if after.status == UNKNOWN else resolved).append(eff.idempotency_key)
        finally:
            self._reconciling = False
        if blocked:
            raise UnresolvedEffects(blocked)
        if not self.is_current(token):
            self._fence("superseded during unpause")
            raise NodeNotActive(FENCED, "authority moved past this node's epoch")
        self._state = ACTIVE
        return {"epoch": token.epoch, "released": released, "resolved": resolved}

    # ---- gates --------------------------------------------------------------

    def require_active(self, token: FencingToken | None = None) -> FencingToken:
        """Raise unless ACTIVE and (``token`` or our epoch) is current."""
        if self._state != ACTIVE:
            raise NodeNotActive(self._state, f"node is {self._state}")
        token = token or self.token()
        if token.epoch != self._epoch or not self.is_current(token):
            self._fence(f"epoch {token.epoch} is stale")
            raise StaleEpoch(f"epoch {token.epoch} of {token.node_id} is no longer current")
        return token

    def require_current(self, token: FencingToken) -> None:
        """Epoch-only check (no state check): raises StaleEpoch, which is a
        definite "not applied", so the journal settles the effect FAILED."""
        if not self.is_current(token):
            self._fence(f"epoch {token.epoch} is stale")
            raise StaleEpoch(f"epoch {token.epoch} of {token.node_id} is no longer current")

    def binding(self, key: str) -> tuple[str, str | None] | None:
        """(connector, boundary_digest) recorded for ``key`` by FencedEffects."""
        with self.db.read() as c:
            row = c.execute("SELECT connector, boundary_digest FROM fenced_bindings"
                            " WHERE idempotency_key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def bind(self, key: str, connector: str, perform: Callable[..., Any]) -> str | None:
        """Record which boundary dispatches ``key``, before any dispatch.

        A registered identity requires ``perform`` to be that contract's
        reviewed adapter, and records its digest. An unregistered identity is
        recorded with no digest: it may dispatch, but the fence will never
        clear a re-dispatch for it. A different binding for the same key is
        refused (``EffectConflict``)."""
        contract = self.boundaries.get(connector)
        if contract is not None and perform != contract.adapter:
            raise EffectConflict(f"perform is not the registered fenced adapter for {connector!r}")
        digest = contract.contract_digest if contract is not None else None
        with self.db.tx() as c:
            row = c.execute("SELECT connector, boundary_digest FROM fenced_bindings"
                            " WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                c.execute("INSERT INTO fenced_bindings (idempotency_key, connector,"
                          " boundary_digest, bound_at) VALUES (?,?,?,?)",
                          (key, connector, digest, self._clock()))
            elif row[0] != connector or row[1] != digest:
                raise EffectConflict(f"effect {key} is bound to boundary {row[0]!r}"
                                     f" (digest {row[1]!r}); refusing {connector!r}")
        return digest

    def _boundary_proven(self, effect: Effect) -> bool:
        """The effect's recorded connector is a registered fenced boundary whose
        contract digest matches the one recorded when it was dispatched."""
        recorded = getattr(effect, "connector", None)
        bound = self.binding(effect.idempotency_key)
        if recorded is None or bound is None or bound[0] != recorded or bound[1] is None:
            return False
        contract = self.boundaries.get(recorded)
        return contract is not None and hmac.compare_digest(contract.contract_digest, bound[1])

    def c2_fence(self, effect: Effect) -> bool:
        """C2 ``EffectJournal(fence=...)`` seam. True only if a late landing of
        the earlier dispatch of ``effect`` is ruled out by the epoch authority:

        * this node is not FENCED, and is ACTIVE (or PAUSED but inside
          :meth:`unpause`, which reconciles and never dispatches);
        * the authority's current epoch is this node's epoch; and
        * the row names the earlier dispatcher (``node@epoch:N``, or a pre-C4
          untagged owner, read as epoch 0), and that epoch is older than ours;
          and
        * the effect's RECORDED connector (C2 ``effect.connector``) is bound,
          with a matching contract digest, to a boundary in this node's
          :class:`FencedBoundaryRegistry`: one that provably refuses tokens
          below the committed epoch, linearized with promotion. Only then can
          that older dispatcher no longer land the effect.

        Anything else returns False and C2 keeps the effect unresolved. That
        includes: an unknown dispatcher (no owner on the row), a dispatcher at
        our own epoch (a timed-out call of ours may still be on the wire), and
        an unproven, unregistered, rotated or unbound connector (an adapter
        that ignores its token is not fenced by any epoch)."""
        owner = getattr(effect, "in_flight_owner", None)
        decision = False
        try:
            if (self._state == ACTIVE or (self._state == PAUSED and self._reconciling)) \
                    and self._epoch is not None and owner \
                    and owner_epoch(owner) < self._epoch \
                    and self._boundary_proven(effect):
                decision = self.is_current()
                if not decision:
                    self._fence("superseded (seen by fence)")
        except Exception:
            decision = False
        self.fence_log.append((getattr(effect, "idempotency_key", "?"), owner, decision))
        return decision

    def policy_state(self) -> str:
        """C1 seam (``PolicyEngine(node_state=...)``): the live node state,
        re-checked against the authority. Fails closed to FENCED."""
        try:
            if self._state != FENCED:
                self.observe()
        except Exception:
            self._fence("epoch authority unreadable")
        return self._state


# --------------------------------------------------------------------------- dispatch

class FencedEffects:
    """Epoch-checked front door to the C2 effect journal.

    ``perform(effect, token)`` receives the node's fencing token and must pass
    it, with ``effect.idempotency_key``, to the external boundary."""

    def __init__(self, node: Node):
        self.node = node
        self.journal = node.journal

    def execute(self, key: str, perform: Callable[[Effect, FencingToken], Any],
                reconcile: Callable[[Effect], str], *, connector: str,
                now: float | None = None) -> Effect:
        """``connector`` is the C2 connector identity (recorded on the effect;
        every dispatch must match it). Before anything is claimed, the key is
        bound to that boundary (:meth:`Node.bind`): for an identity in the
        node's :class:`FencedBoundaryRegistry`, ``perform`` must be the
        registered adapter and the contract digest is recorded. Only such
        effects can later be cleared by :meth:`Node.c2_fence`. Fencing does not
        use C2's trusted idempotent registry or its digest.

        Because ``perform`` is wrapped (to pass the token and re-check the
        epoch), a connector in C2's trusted idempotent registry cannot be
        routed through here: C2 requires ``perform`` to be its own adapter."""
        token = self.node.require_active()
        if not (isinstance(connector, str) and connector):
            raise ValueError("connector identity required")
        self.node.bind(key, connector, perform)
        refused: list[StaleEpoch] = []

        def fence() -> None:
            self.node.require_active(token)

        def fenced_perform(eff: Effect) -> Any:
            try:
                self.node.require_current(token)   # last check before the boundary
                return perform(eff, token)
            except StaleEpoch as exc:
                refused.append(exc)
                raise

        eff = self.journal.execute(key, fenced_perform, reconcile, owner=token.owner,
                                   connector=connector, now=now, guard=fence)
        if refused:   # the journal settled it FAILED (not applied); surface the refusal
            self.node._fence(f"dispatch refused: {refused[0]}")
            raise refused[0]
        return eff


# --------------------------------------------------------------------------- boundary

class StubConnector:
    """Staging stand-in for the external system behind an epoch-checking egress
    boundary. It does NOT deduplicate on the idempotency key, so any double
    execution is visible.

    Fencing-token pattern, linearized with promotion: the boundary's state
    (highest epoch seen, applied and refused rows) lives in the AUTHORITY's
    SQLite DB, and :meth:`apply` runs entirely inside
    :meth:`EpochAuthority.boundary`, one ``BEGIN IMMEDIATE`` transaction that
    :meth:`EpochAuthority.promote` also needs. In that one transaction it
    refuses a token below the highest epoch seen, or not equal to the
    authority's committed epoch, and otherwise records the apply. So an apply
    and a promotion are strictly ordered: once a promotion has committed, no
    older token lands, and an apply in progress delays the promotion until it
    has committed. ``reconcile`` reads under the same lock.

    Test seams: ``before_apply(effect, token)`` runs in :meth:`perform` before
    the boundary transaction (a stall or transport error on the way in; it
    holds no lock). ``after_check(conn, token)`` runs inside the transaction
    after the token passed, before the apply is recorded."""

    IDENTITY = "stub.epoch-gateway"

    def __init__(self, authority: EpochAuthority, *, identity: str = IDENTITY):
        if not isinstance(authority, EpochAuthority):
            raise TypeError("StubConnector needs the EpochAuthority whose DB it shares")
        if not (isinstance(identity, str) and identity):
            raise ValueError("identity required")
        self.identity = identity   # C2 connector identity; NOT trusted-idempotent
        self.authority = authority
        self.after_check: Callable[[sqlite3.Connection, FencingToken], None] | None = None
        # test seam: runs in the reviewed adapter BEFORE the boundary transaction
        # (network latency / a stalled caller / a transport error on the way in)
        self.before_apply: Callable[[Effect, FencingToken], None] | None = None
        with authority.boundary() as (c, _):
            c.execute("CREATE TABLE IF NOT EXISTS ext_applied ("
                      " seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
                      " node_id TEXT NOT NULL, epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS ext_fence ("
                      " id INTEGER PRIMARY KEY CHECK (id = 1), max_epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS ext_refused ("
                      " seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
                      " node_id TEXT NOT NULL, epoch INTEGER NOT NULL, reason TEXT NOT NULL)")
            c.execute("INSERT OR IGNORE INTO ext_fence (id, max_epoch) VALUES (1, 0)")

    def apply(self, key: str, token: FencingToken) -> dict[str, Any]:
        if not isinstance(token, FencingToken):
            raise StaleEpochRefused("no fencing token")
        reason = None
        try:
            with self.authority.boundary() as (c, auth):
                max_seen = c.execute("SELECT max_epoch FROM ext_fence WHERE id = 1").fetchone()[0]
                if token.epoch < max_seen:
                    reason = f"epoch {token.epoch} < highest seen {max_seen}"
                elif token.epoch != auth.epoch or token.node_id != auth.holder:
                    reason = f"epoch {token.epoch} is not the committed epoch {auth.epoch}"
                if reason:
                    c.execute("INSERT INTO ext_refused (key, node_id, epoch, reason)"
                              " VALUES (?,?,?,?)", (key, token.node_id, token.epoch, reason))
                else:
                    if self.after_check is not None:
                        self.after_check(c, token)
                    c.execute("UPDATE ext_fence SET max_epoch = ? WHERE id = 1", (token.epoch,))
                    c.execute("INSERT INTO ext_applied (key, node_id, epoch) VALUES (?,?,?)",
                              (key, token.node_id, token.epoch))
        except AuthorityUnavailable as exc:   # fail closed: nothing was applied
            raise StaleEpochRefused(f"connector refused: {exc}") from exc
        if reason:   # the refusal row is committed before we raise
            raise StaleEpochRefused(f"connector refused: {reason}")
        return {"applied": key, "epoch": token.epoch}

    def contract(self) -> FencedBoundaryContract:
        """This boundary's reviewed fencing contract, for a node's
        :class:`FencedBoundaryRegistry`. The adapter is ``self.perform``; the
        evidence is the linearization tests in ``tests/test_fencing.py``."""
        return FencedBoundaryContract(
            self.identity, self.perform,
            "runtime/fencing.py:StubConnector + tests/test_fencing.py:BoundaryLinearization")

    # C2 / FencedEffects-compatible callables
    def perform(self, effect: Effect, token: FencingToken) -> dict[str, Any]:
        effect.action()                    # executors run only the journaled action
        if self.before_apply is not None:
            self.before_apply(effect, token)
        return self.apply(effect.idempotency_key, token)

    def perform_unfenced(self, effect: Effect) -> dict[str, Any]:
        """A C2-shaped ``perform(effect)`` with no token: always refused here."""
        return self.apply(effect.idempotency_key, None)  # type: ignore[arg-type]

    def reconcile(self, effect: Effect) -> str:
        return "succeeded" if self.count(effect.idempotency_key) else "failed"

    def count(self, key: str | None = None) -> int:
        with self.authority.boundary() as (c, _):   # serialises with applies and promotions
            if key is None:
                return c.execute("SELECT COUNT(*) FROM ext_applied").fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM ext_applied WHERE key = ?",
                             (key,)).fetchone()[0]

    def applied(self) -> list[tuple[str, str, int]]:
        with self.authority.boundary() as (c, _):
            return c.execute("SELECT key, node_id, epoch FROM ext_applied ORDER BY seq").fetchall()

    def refusals(self) -> list[tuple[str, str, int, str]]:
        with self.authority.boundary() as (c, _):
            return c.execute("SELECT key, node_id, epoch, reason FROM ext_refused"
                             " ORDER BY seq").fetchall()


# --------------------------------------------------------------------------- replication

class Replicator(Protocol):
    """Seam for journal replication (Litestream in Phase F)."""

    def replicate(self, db_path: str | Path) -> str: ...
    def latest(self) -> str | None: ...
    def restore(self, target_path: str | Path, generation: str | None = None) -> Path: ...


class FileCopyReplicator:
    """In-process simulation of Litestream: each ``replicate`` writes a
    consistent snapshot (SQLite online backup, so WAL content is included) to
    ``replica_dir`` as a numbered generation, published by atomic rename.
    ``restore`` materialises a generation at a NEW path and marks it as
    restored, so a :class:`Node` opened on it starts FENCED and cannot
    :meth:`Node.rejoin` (it must promote).

    Like Litestream, a replica can lag: effects journaled after the last
    snapshot are absent from a restore. The C2 idempotency key, passed to the
    external system, is the backstop for re-recorded intents."""

    _PREFIX, _SUFFIX = "gen-", ".sqlite"

    def __init__(self, replica_dir: str | Path):
        self.dir = Path(replica_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _generations(self) -> list[str]:
        return sorted(p.name[len(self._PREFIX):-len(self._SUFFIX)]
                      for p in self.dir.glob(f"{self._PREFIX}*{self._SUFFIX}"))

    def _path(self, generation: str) -> Path:
        return self.dir / f"{self._PREFIX}{generation}{self._SUFFIX}"

    def latest(self) -> str | None:
        gens = self._generations()
        return gens[-1] if gens else None

    def replicate(self, db_path: str | Path) -> str:
        last = self.latest()
        gen = f"{(int(last) if last else 0) + 1:06d}"
        tmp = self.dir / f".{gen}.tmp"
        src = sqlite3.connect(str(db_path), timeout=30)
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")   # self-contained single file
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self._path(gen))
        return gen

    def restore(self, target_path: str | Path, generation: str | None = None) -> Path:
        gen = generation or self.latest()
        if gen is None or not self._path(gen).exists():
            raise FileNotFoundError("no replica generation to restore")
        target = Path(target_path)
        if target.exists():
            raise FileExistsError(f"restore target {target} exists; refusing to overwrite")
        src = sqlite3.connect(str(self._path(gen)))
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        db = Database(target)      # validates schema, switches to WAL
        with db.tx() as c:
            _meta_set(c, _META_RESTORED, gen)
        return target
