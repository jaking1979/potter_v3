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
  boundary"). It refuses any token whose epoch is not the authority's current
  epoch, or is lower than the highest epoch it has seen. The check and the apply
  run in one write transaction, and ``reconcile`` reads under the same lock, so
  once a promotion commits, no older token can land and the new primary's
  reconcile sees everything that did land.

Limits (documented, not hidden): third-party APIs cannot check our tokens, so
the boundary in production is an egress gateway with this check, and a window
remains between the gateway's check and the third party applying the call. The
deterministic C2 idempotency key, passed to the third party, is the backstop
for that window. Staging-only (T22); nothing here performs a real effect.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from .db import Database, as_database
from .effects import (UNKNOWN, Effect, EffectError, EffectFailed, EffectInFlight,
                      EffectJournal)

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
                 *, clock: Callable[[], float] = time.time):
        if not node_id or not isinstance(node_id, str) or "@" in node_id:
            raise ValueError("node_id required (no '@')")
        self.node_id = node_id
        self.authority = authority
        self.db = as_database(db)
        self.journal = EffectJournal(self.db, clock=clock)
        self._clock = clock
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

        In-flight windows owned by a LOWER epoch are released first: the
        promotion is committed, so the boundary already refuses that dispatcher
        and the external state read by ``reconcile`` is final for it. Windows of
        our own epoch are respected. If anything is still unknown, the node
        stays PAUSED and :class:`UnresolvedEffects` is raised. ``pending``
        effects (never dispatched) need no reconciliation."""
        if self._state != PAUSED:
            raise NodeNotActive(self._state, f"unpause needs PAUSED, node is {self._state}")
        token = self.token()
        if not self.is_current(token):
            self._fence("superseded before unpause")
            raise NodeNotActive(FENCED, "authority moved past this node's epoch")
        released, resolved, blocked = [], [], []
        for eff in self.journal.unresolved():
            if eff.status != UNKNOWN:
                continue
            if eff.in_flight_owner and owner_epoch(eff.in_flight_owner) < token.epoch:
                if self.journal.release_in_flight(eff.idempotency_key, eff.in_flight_owner,
                                                  now=now):
                    released.append(eff.idempotency_key)
            try:
                after = self.journal.reconcile(eff.idempotency_key, reconcile, now=now)
            except EffectInFlight:
                blocked.append(eff.idempotency_key)
                continue
            (blocked if after.status == UNKNOWN else resolved).append(eff.idempotency_key)
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
                reconcile: Callable[[Effect], str], *, now: float | None = None) -> Effect:
        token = self.node.require_active()
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
                                   now=now, fence=fence)
        if refused:   # the journal settled it FAILED (not applied); surface the refusal
            self.node._fence(f"dispatch refused: {refused[0]}")
            raise refused[0]
        return eff


# --------------------------------------------------------------------------- boundary

class StubConnector:
    """Staging stand-in for the external system behind an epoch-checking egress
    boundary. SQLite-backed so separate processes share it. It does NOT
    deduplicate on the idempotency key, so any double execution is visible.

    ``apply`` checks the token and applies in ONE write transaction; ``reconcile``
    reads under the same write lock. Hence: a token older than a committed
    promotion never lands, and the new primary's reconcile sees every apply that
    did land."""

    def __init__(self, path: str | Path, authority: EpochAuthority):
        self.path = str(path)
        self.authority = authority
        with self._tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied ("
                      " seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
                      " node_id TEXT NOT NULL, epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS fence ("
                      " id INTEGER PRIMARY KEY CHECK (id = 1), max_epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS refused ("
                      " seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
                      " node_id TEXT NOT NULL, epoch INTEGER NOT NULL, reason TEXT NOT NULL)")
            c.execute("INSERT OR IGNORE INTO fence (id, max_epoch) VALUES (1, 0)")

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        finally:
            c.close()

    def apply(self, key: str, token: FencingToken) -> dict[str, Any]:
        if not isinstance(token, FencingToken):
            raise StaleEpochRefused("no fencing token")
        reason = None
        with self._tx() as c:
            max_seen = c.execute("SELECT max_epoch FROM fence WHERE id = 1").fetchone()[0]
            try:
                auth = self.authority.current()
            except AuthorityUnavailable:
                auth = None
            if auth is None:
                reason = "authority unavailable"
            elif token.epoch < max_seen:
                reason = f"epoch {token.epoch} < highest seen {max_seen}"
            elif token.epoch != auth.epoch or token.node_id != auth.holder:
                reason = f"epoch {token.epoch} is not the current epoch {auth.epoch}"
            if reason:
                c.execute("INSERT INTO refused (key, node_id, epoch, reason) VALUES (?,?,?,?)",
                          (key, token.node_id, token.epoch, reason))
            else:
                c.execute("UPDATE fence SET max_epoch = ? WHERE id = 1", (token.epoch,))
                c.execute("INSERT INTO applied (key, node_id, epoch) VALUES (?,?,?)",
                          (key, token.node_id, token.epoch))
        if reason:   # the refusal row is committed before we raise
            raise StaleEpochRefused(f"connector refused: {reason}")
        return {"applied": key, "epoch": token.epoch}

    # C2 / FencedEffects-compatible callables
    def perform(self, effect: Effect, token: FencingToken) -> dict[str, Any]:
        effect.action()                    # executors run only the journaled action
        return self.apply(effect.idempotency_key, token)

    def reconcile(self, effect: Effect) -> str:
        return "succeeded" if self.count(effect.idempotency_key) else "failed"

    def count(self, key: str | None = None) -> int:
        with self._tx() as c:   # write lock: serialises with in-progress applies
            if key is None:
                return c.execute("SELECT COUNT(*) FROM applied").fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM applied WHERE key = ?", (key,)).fetchone()[0]

    def applied(self) -> list[tuple[str, str, int]]:
        with self._tx() as c:
            return c.execute("SELECT key, node_id, epoch FROM applied ORDER BY seq").fetchall()

    def refusals(self) -> list[tuple[str, str, int, str]]:
        with self._tx() as c:
            return c.execute("SELECT key, node_id, epoch, reason FROM refused"
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
