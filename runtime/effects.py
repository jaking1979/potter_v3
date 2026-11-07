"""Effect journal (package C2, decision T10).

Every external effect is journaled as an intent BEFORE it is executed, under a
deterministic idempotency key that every attempt reuses (so a connector can
pass it to the external system and the system can deduplicate).

Statuses:

* ``pending``   - intent recorded, never dispatched.
* ``unknown``   - dispatched and no outcome recorded. This is set and committed
  *before* ``perform`` is called, so a crash, kill or ambiguous error during the
  call leaves ``unknown``, never ``pending``.
* ``succeeded`` - the external system applied it. Never executed again.
* ``failed``    - the external system definitely did not apply it (``perform``
  raised :class:`EffectFailed`, or ``reconcile`` said so). May be retried with
  the SAME idempotency key.

Never blind retry: an ``unknown`` effect is only re-dispatched after the
injected ``reconcile(effect)`` reads the external state and returns
``"failed"`` AND a late landing of the earlier dispatch is excluded - by a
``fence`` (C4 seam; default refuses) or because the effect was first
dispatched under a trusted ``ConnectorContract`` whose identity AND
``contract_digest`` (persisted on the effect; it covers the verified adapter
source artifact) still match an entry in the
``trusted_idempotent`` registry fixed at construction (default: none). A
contract rotated across a restart, or a row with no digest, is not trusted.
No caller-supplied flag or attribute can grant this. ``"succeeded"`` resolves it without
re-execution; ``"still_unknown"`` (or anything unrecognised, or an unfenced
``"failed"``) refuses the retry and leaves it unresolved.

Executors run only ``effect.action()``: the persisted canonical action,
integrity-checked against its hash (C0 "one snapshot" rule).

C4 (``runtime/fencing.py``) fills the ``fence`` seam from its epoch authority.
It also uses two hooks here. ``execute(guard=...)`` runs a raise-to-refuse check
before anything else and again inside the claim transaction.
``expire_in_flight`` ends a lower-epoch dispatcher's in-flight window after a
takeover; the dispatcher's identity stays on the row so the fence can judge it.

Not here: the outbox (C3); real connectors.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import sqlite3
import time
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping

from ._contract import BoundAction, EffectIntent
from .db import Database, as_database

# Fixed namespace: the same (task, step, action) always yields the same key.
EFFECT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:potter:v3:c2:effect-journal")

PENDING, UNKNOWN, SUCCEEDED, FAILED = "pending", "unknown", "succeeded", "failed"
STILL_UNKNOWN = "still_unknown"
RECONCILE_VERDICTS = (SUCCEEDED, FAILED, STILL_UNKNOWN)
DEFAULT_DISPATCH_TIMEOUT_S = 300.0
NOT_APPLIED_UNFENCED = ("reconcile reports not applied, but the earlier dispatch may still land;"
                        " re-dispatch needs a fence (C4) or a connector in the trusted"
                        " idempotent registry")


DEFAULT_IDEMPOTENCY_SEMANTICS = "provider applies each idempotency key at most once"


def adapter_name(adapter: Any) -> str:
    """Stable qualified name of an adapter: the bound object's class (if any)
    plus the function's module-qualified name."""
    owner = getattr(adapter, "__self__", None)
    func = getattr(adapter, "__func__", adapter)
    if not hasattr(func, "__qualname__"):            # callable instance
        func = type(func).__call__
    name = f"{func.__module__}.{func.__qualname__}"
    if owner is not None:
        cls = owner if isinstance(owner, type) else type(owner)
        name = f"{cls.__module__}.{cls.__qualname__}#{name}"
    return name


class AdapterVerificationError(ValueError):
    """A registry entry's adapter implementation cannot be verified against
    its declared artifact digest; the registry is not built."""


_HEX64 = frozenset("0123456789abcdef")


def adapter_artifact_digest(adapter: Any) -> str:
    """sha256 of the adapter's implementation: the bytes of the source file
    that defines its function, plus (if different) the file defining the class
    it is bound to. Raises AdapterVerificationError if any source file cannot
    be located and read (builtins, C extensions, REPL/exec-defined code), or if
    the loaded function's bytecode is not what that file compiles to."""
    owner = getattr(adapter, "__self__", None)
    func = getattr(adapter, "__func__", adapter)
    objs = [func if hasattr(func, "__code__") else type(func)]
    if owner is not None:
        objs.append(owner if isinstance(owner, type) else type(owner))
    files: list[str] = []
    for obj in objs:
        try:
            path = inspect.getsourcefile(obj)
        except (TypeError, OSError):
            path = None
        if not path or not os.path.isfile(path):
            raise AdapterVerificationError(f"no readable source for adapter {adapter_name(adapter)!r}")
        path = os.path.realpath(path)
        if path not in files:
            files.append(path)
    parts, sources = [], {}
    for path in files:
        try:
            with open(path, "rb") as f:
                sources[path] = f.read()
        except OSError as exc:
            raise AdapterVerificationError(f"cannot read adapter source {path}: {exc}") from exc
        parts.append(hashlib.sha256(sources[path]).hexdigest())
    code = getattr(objs[0], "__code__", None)
    if code is not None:
        _require_loaded_matches_source(code, sources[files[0]], adapter)
    return parts[0] if len(parts) == 1 else hashlib.sha256(":".join(parts).encode()).hexdigest()


def _require_loaded_matches_source(code: Any, source: bytes, adapter: Any) -> None:
    """The file on disk is only evidence for the code actually running if the
    loaded function compiles identically from it; a file rewritten after
    import (or code patched in memory) is refused."""
    try:
        module_code = compile(source, code.co_filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        raise AdapterVerificationError(f"adapter source does not compile: {exc}") from exc
    # Structural equality, not marshal bytes: marshal output depends on
    # reference counts and string interning (FLAG_REF), so equal code can
    # serialize differently across interpreters and runs. CPython's code
    # equality covers bytecode, constants (recursively, incl. nested code),
    # names, locals, flags, first line, line table and exception table.
    stack = [module_code]
    while stack:
        c = stack.pop()
        if c.co_qualname == code.co_qualname and c.co_filename == code.co_filename \
                and c.co_firstlineno == code.co_firstlineno and c == code:
            return
        stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
    raise AdapterVerificationError(
        f"loaded adapter {adapter_name(adapter)!r} differs from its source file")


@dataclass(frozen=True)
class IdempotencyScope:
    """The provider-side namespace in which an idempotency key is deduplicated.
    Non-secret by contract: identifiers and a hash of the relevant config,
    never credentials. Two dispatches with the same key dedupe only if their
    scopes are equal."""
    provider: str            # e.g. "gmail"
    account: str             # account / tenant id the adapter is bound to
    endpoint: str            # API base the adapter calls
    dedupe_scope: str        # what the provider dedupes over, e.g. "per-account"
    dedupe_version: str      # provider's dedupe contract version
    retention_window_s: int  # how long the provider remembers a key
    config_hash: str         # sha256 of the adapter's dedupe-relevant config

    def __post_init__(self):
        for name in ("provider", "account", "endpoint", "dedupe_scope", "dedupe_version"):
            v = getattr(self, name)
            if not (isinstance(v, str) and v):
                raise ValueError(f"idempotency scope {name} required")
        r = self.retention_window_s
        if not (isinstance(r, int) and not isinstance(r, bool) and r > 0):
            raise ValueError("retention_window_s must be a positive int")
        h = self.config_hash
        if not (isinstance(h, str) and len(h) == 64 and set(h) <= _HEX64):
            raise ValueError("config_hash must be 64 lowercase hex characters")

    def canonical(self) -> list:
        return [self.provider, self.account, self.endpoint, self.dedupe_scope,
                self.dedupe_version, self.retention_window_s, self.config_hash]


def current_idempotency_scope(adapter: Any) -> IdempotencyScope:
    """Ask the adapter for the scope it is bound to RIGHT NOW: the
    ``idempotency_scope()`` method of the object a bound adapter belongs to,
    or a module-level ``idempotency_scope()`` beside a plain function. Raises
    AdapterVerificationError if it is missing, fails, or returns anything but
    an IdempotencyScope."""
    owner = getattr(adapter, "__self__", None)
    if owner is not None:
        report = getattr(owner, "idempotency_scope", None)
    else:
        report = getattr(adapter, "__globals__", {}).get("idempotency_scope")
    if not callable(report):
        raise AdapterVerificationError(f"adapter {adapter_name(adapter)!r} cannot report its idempotency scope")
    try:
        scope = report()
    except Exception as exc:
        raise AdapterVerificationError(f"adapter {adapter_name(adapter)!r} failed to report its"
                                       f" idempotency scope: {exc!r}") from exc
    if not isinstance(scope, IdempotencyScope):
        raise AdapterVerificationError(f"adapter {adapter_name(adapter)!r} reported a non-IdempotencyScope")
    return scope


@dataclass(frozen=True)
class ConnectorContract:
    """A reviewed connector whose provider applies an idempotency key at most
    once. ``adapter`` is the exact perform callable that was reviewed against
    that contract; ``contract_ref`` points at the verification evidence;
    ``adapter_artifact_digest`` is the sha256 of the reviewed adapter artifact,
    supplied by trusted configuration (see ``adapter_artifact_digest()``);
    ``idempotency_scope`` is the provider namespace (provider, account,
    endpoint, dedupe scope/version, retention, config hash) the guarantee
    holds in; ``idempotency_semantics`` states the verified guarantee.

    ``contract_digest`` (computed, immutable) is sha256 over the canonical JSON
    of (identity, contract_ref, adapter qualified name, adapter artifact
    digest, idempotency scope, semantics). It is persisted on an effect at first dispatch, so a
    registry rebuilt after a restart must present the SAME contract, including
    the same adapter implementation, before its trust applies.
    TrustedIdempotentRegistry verifies the declared artifact digest against the
    adapter's actual source when it is built."""
    identity: str
    adapter: Callable[["Effect"], Any]
    contract_ref: str
    adapter_artifact_digest: str
    idempotency_scope: IdempotencyScope
    idempotency_semantics: str = DEFAULT_IDEMPOTENCY_SEMANTICS
    contract_digest: str = field(init=False)

    def __post_init__(self):
        if not (isinstance(self.identity, str) and self.identity):
            raise ValueError("identity required")
        if not (isinstance(self.contract_ref, str) and self.contract_ref):
            raise ValueError("contract_ref required")
        if not (isinstance(self.idempotency_semantics, str) and self.idempotency_semantics):
            raise ValueError("idempotency_semantics required")
        if not callable(self.adapter):
            raise TypeError("adapter must be callable")
        d = self.adapter_artifact_digest
        if not (isinstance(d, str) and len(d) == 64 and set(d) <= _HEX64):
            raise ValueError("adapter_artifact_digest must be 64 lowercase hex characters")
        if not isinstance(self.idempotency_scope, IdempotencyScope):
            raise TypeError("idempotency_scope must be an IdempotencyScope")
        canon = json.dumps([self.identity, self.contract_ref, adapter_name(self.adapter),
                            self.adapter_artifact_digest, self.idempotency_scope.canonical(),
                            self.idempotency_semantics],
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        object.__setattr__(self, "contract_digest", hashlib.sha256(canon).hexdigest())


class TrustedIdempotentRegistry(Mapping):
    """Frozen identity -> ConnectorContract mapping, injected by configuration
    when an EffectJournal is constructed. It cannot be changed afterwards.

    Each entry's declared ``adapter_artifact_digest`` must equal the digest of
    the adapter's actual source; otherwise (or if the source is unreadable)
    construction raises AdapterVerificationError."""

    def __init__(self, contracts: Iterable[ConnectorContract] = ()):
        entries: dict[str, ConnectorContract] = {}
        for k in contracts:
            if not isinstance(k, ConnectorContract):
                raise TypeError("registry entries must be ConnectorContract")
            if k.identity in entries:
                raise ValueError(f"duplicate connector identity {k.identity!r}")
            actual = adapter_artifact_digest(k.adapter)
            if not hmac.compare_digest(actual, k.adapter_artifact_digest):
                raise AdapterVerificationError(
                    f"adapter for {k.identity!r} does not match its declared artifact digest")
            if current_idempotency_scope(k.adapter) != k.idempotency_scope:
                raise AdapterVerificationError(
                    f"adapter for {k.identity!r} is bound to a different idempotency scope")
            entries[k.identity] = k
        self._entries = MappingProxyType(entries)

    def __getitem__(self, identity: str) -> ConnectorContract:
        return self._entries[identity]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __setattr__(self, name, value):
        if name != "_entries" or "_entries" in self.__dict__:
            raise AttributeError("TrustedIdempotentRegistry is frozen")
        object.__setattr__(self, name, value)


def idempotency_key(task_id: str, effect_id: str, action_hash: str) -> str:
    """UUIDv5 over the canonical JSON of (task, step, approved action hash)."""
    name = json.dumps([task_id, effect_id, action_hash], separators=(",", ":"), ensure_ascii=False)
    return str(uuid.uuid5(EFFECT_NAMESPACE, name))


class EffectError(Exception):
    pass


class EffectFailed(EffectError):
    """Raised by ``perform`` when the external system DEFINITELY did not apply
    the effect (e.g. a 4xx validation error before any write). Any other
    exception from ``perform`` is treated as an unknown outcome."""


class EffectOutcomeUnknown(EffectError):
    """The effect may or may not have happened; reconcile before any retry."""


class EffectInFlight(EffectError):
    """Another dispatcher holds this effect right now."""


class EffectConflict(EffectError):
    """This task step is already bound to a different action, or a dispatch
    does not match the connector the effect is bound to."""


@dataclass(frozen=True)
class Effect:
    idempotency_key: str
    task_id: str
    effect_id: str
    action_hash: str
    action_canonical: str
    approval_id: str | None
    status: str
    dispatches: int
    in_flight_owner: str | None
    in_flight_until: float | None
    result: Any
    last_error: str | None
    connector: str | None = None   # identity the effect was (first) dispatched through
    connector_digest: str | None = None   # trusted contract digest at that dispatch, if any

    def intent(self) -> EffectIntent:
        return EffectIntent(self.effect_id, self.task_id, self.action_hash,
                            self.approval_id or "", self.action_canonical)

    def action(self) -> BoundAction:
        """The exact journaled action; raises ApprovalRefused if altered."""
        return self.intent().action()


_COLS = ("idempotency_key, task_id, effect_id, action_hash, action_canonical, approval_id,"
         " status, dispatches, in_flight_owner, in_flight_until, result, last_error, connector,"
         " connector_digest")


def _effect(row: tuple) -> Effect:
    r = list(row)
    r[10] = json.loads(r[10]) if r[10] is not None else None
    return Effect(*r)


def load_effect(c: sqlite3.Connection, key: str) -> Effect | None:
    row = c.execute(f"SELECT {_COLS} FROM effects WHERE idempotency_key=?", (key,)).fetchone()
    return _effect(row) if row else None


def insert_intent(c: sqlite3.Connection, intent: EffectIntent, key: str, now: float) -> None:
    """Insert a pending intent inside the caller's transaction (IntegrityError
    if the key or the (task, step) pair already exists)."""
    c.execute("INSERT INTO effects (idempotency_key, task_id, effect_id, action_hash,"
              " action_canonical, approval_id, status, created_at, updated_at)"
              " VALUES (?,?,?,?,?,?,?,?,?)",
              (key, intent.task_id, intent.effect_id, intent.action_hash,
               intent.action_canonical, intent.approval_id or None, PENDING, now, now))


class EffectJournal:
    def __init__(self, db: Database | str | Path, *, clock: Callable[[], float] = time.time,
                 dispatch_timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
                 fence: Callable[[Effect], bool] | None = None,
                 trusted_idempotent: TrustedIdempotentRegistry | None = None):
        self.db = as_database(db)
        self._fence = fence   # C4 seam; None = no fencing = refuse unfenced re-dispatch
        if trusted_idempotent is None:
            trusted_idempotent = TrustedIdempotentRegistry()
        if not isinstance(trusted_idempotent, TrustedIdempotentRegistry):
            raise TypeError("trusted_idempotent must be a TrustedIdempotentRegistry")
        self._trusted = trusted_idempotent   # fixed for this journal's lifetime
        self._clock = clock
        self._dispatch_timeout = dispatch_timeout_s

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    # -- recording ---------------------------------------------------------
    def record_intent(self, task_id: str, effect_id: str, action: BoundAction,
                      *, now: float | None = None) -> Effect:
        """Journal the intent before execution. Idempotent: re-recording the
        same step with the same action (e.g. after a resume) returns the
        existing entry unchanged; a different action for that step is refused."""
        if not isinstance(action, BoundAction):
            raise TypeError("action must be a BoundAction")
        if not effect_id or action.task_id != task_id:
            raise EffectConflict("effect_id required and action.task_id must match task_id")
        canon = action.canonical_json().decode("utf-8")
        intent = EffectIntent(effect_id, task_id, action.hash(), "", canon)
        key = idempotency_key(task_id, effect_id, intent.action_hash)
        now = self._now(now)
        with self.db.tx() as c:
            existing = c.execute("SELECT idempotency_key FROM effects WHERE task_id=? AND effect_id=?",
                                 (task_id, effect_id)).fetchone()
            if existing is None:
                insert_intent(c, intent, key, now)
            elif existing[0] != key:
                raise EffectConflict(f"step {effect_id!r} of {task_id!r} is bound to another action")
            return load_effect(c, key)

    # -- reading -----------------------------------------------------------
    def get(self, key: str) -> Effect | None:
        with self.db.read() as c:
            return load_effect(c, key)

    def for_step(self, task_id: str, effect_id: str) -> Effect | None:
        with self.db.read() as c:
            row = c.execute(f"SELECT {_COLS} FROM effects WHERE task_id=? AND effect_id=?",
                            (task_id, effect_id)).fetchone()
        return _effect(row) if row else None

    def unresolved(self) -> list[Effect]:
        """Recovery sweep input: everything not yet known to have succeeded or failed."""
        with self.db.read() as c:
            rows = c.execute(f"SELECT {_COLS} FROM effects WHERE status IN (?,?)"
                             " ORDER BY created_at", (PENDING, UNKNOWN)).fetchall()
        return [_effect(r) for r in rows]

    # -- reconciliation ----------------------------------------------------
    def reconcile(self, key: str, reconcile: Callable[[Effect], str], *,
                  now: float | None = None) -> Effect:
        """Resolve an ``unknown`` effect from external state, without dispatching.
        Returns the effect, whose status may still be ``unknown``.

        ``succeeded`` is always accepted. ``failed`` is accepted only when a late
        landing of the earlier dispatch is excluded (see ``execute``); otherwise
        the effect stays ``unknown`` for authoritative reconciliation."""
        now = self._now(now)
        eff = self._require(key)
        if eff.status != UNKNOWN:
            return eff
        self._refuse_if_in_flight(eff, now)
        return self._reconcile(eff, reconcile, self._late_landing_excluded(eff), now)

    def _reconcile(self, eff: Effect, reconcile: Callable[[Effect], str], allow_failed: bool,
                   now: float) -> Effect:
        key = eff.idempotency_key
        try:
            verdict = reconcile(eff)
        except Exception as exc:  # a failed read is not evidence of anything
            verdict, err = STILL_UNKNOWN, f"reconcile error: {exc!r}"
        else:
            err = None
        if verdict not in RECONCILE_VERDICTS:
            verdict, err = STILL_UNKNOWN, f"unrecognised reconcile verdict {verdict!r}"
        if verdict == FAILED and not allow_failed:
            # "Not applied yet" is not "will never apply": the earlier dispatch
            # (a stalled worker, or a timed-out request still on the wire) may
            # land later. Without a fence or a trusted idempotent connector,
            # keep the effect unresolved and leave any in-flight claim in place.
            verdict, err = STILL_UNKNOWN, NOT_APPLIED_UNFENCED
        if verdict == STILL_UNKNOWN:
            if err:
                with self.db.tx() as c:
                    c.execute("UPDATE effects SET last_error=?, updated_at=? WHERE idempotency_key=?",
                              (err, now, key))
            return self._require(key)
        with self.db.tx() as c:
            cur = c.execute("UPDATE effects SET status=?, updated_at=?, last_error=?,"
                            " in_flight_owner=NULL, in_flight_until=NULL"
                            " WHERE idempotency_key=? AND status=? AND dispatches=?"
                            " AND (in_flight_owner IS NULL OR in_flight_until <= ?)",
                            (verdict, now, "reconciled: not applied" if verdict == FAILED else None,
                             key, UNKNOWN, eff.dispatches, now))
            if cur.rowcount != 1:
                raise EffectInFlight(f"effect {key} changed during reconciliation")
            return load_effect(c, key)

    def _late_landing_excluded(self, eff: Effect) -> bool:
        """Only configuration-time trust counts: the C4 fence, or the contract
        RECORDED on the effect at first dispatch (identity AND digest) being
        present, unchanged, in this journal's trusted registry. A rotated or
        re-mapped contract, or a row with no digest, is not trusted."""
        if self._fenced(eff):
            return True
        contract = self._trusted.get(eff.connector) if eff.connector is not None else None
        if not (contract is not None and eff.connector_digest is not None
                and hmac.compare_digest(contract.contract_digest, eff.connector_digest)):
            return False
        return self._scope_unchanged(contract)

    @staticmethod
    def _scope_unchanged(contract: ConnectorContract) -> bool:
        """The adapter must still report exactly the scope its contract (and so
        the recorded digest) declares; config changed at runtime, or an adapter
        that cannot report, is not trusted."""
        try:
            return current_idempotency_scope(contract.adapter) == contract.idempotency_scope
        except AdapterVerificationError:
            return False

    def _fenced(self, eff: Effect) -> bool:
        """C4 seam: True only if the earlier dispatcher is authoritatively fenced
        off. Default (no fence configured), or any error: refuse."""
        if self._fence is None:
            return False
        try:
            return self._fence(eff) is True
        except Exception:
            return False

    # -- execution ---------------------------------------------------------
    def execute(self, key: str, perform: Callable[[Effect], Any],
                reconcile: Callable[[Effect], str], *, owner: str, connector: str,
                now: float | None = None,
                guard: Callable[[], None] | None = None) -> Effect:
        """Run the journaled effect at most as often as the external state allows:

        * ``succeeded`` -> returned as is, ``perform`` is not called.
        * ``pending`` / ``failed`` -> dispatched once with the same key.
        * ``unknown``   -> ``reconcile`` first. ``succeeded`` resolves it without
          re-execution. ``failed`` permits ONE re-dispatch with the same key, and
          only if a late landing of the earlier dispatch is excluded: the
          configured ``fence`` (C4 seam) confirms the earlier dispatcher is fenced
          off, or the identity the effect was dispatched through is in the
          ``trusted_idempotent`` registry given at construction. Otherwise, and
          for ``still_unknown``, EffectOutcomeUnknown is raised and the effect
          stays in ``unresolved()`` for authoritative reconciliation. An expired
          in-flight claim is never by itself permission to re-dispatch.

        ``connector`` names the identity ``perform`` dispatches through; it is
        recorded on the first dispatch, together with the trusted contract's
        digest if the identity is trusted, and every later dispatch must match
        the identity. If it is a trusted identity, ``perform`` must be that
        contract's registered adapter. Naming an identity grants nothing unless the
        journal was configured to trust it.

        ``perform(effect)`` must use ``effect.action()`` and pass
        ``effect.idempotency_key`` to the external system.

        ``guard()`` (C4), if given, runs first and again inside the claim
        transaction; if it raises, nothing is claimed or dispatched. (Distinct
        from the constructor's ``fence(effect)`` seam, which judges whether an
        EARLIER dispatcher of an ``unknown`` effect is fenced off.)
        """
        if not owner:
            raise ValueError("owner required")
        if not (isinstance(connector, str) and connector):
            raise ValueError("connector identity required")
        if guard is not None:
            guard()
        now = self._now(now)
        eff = self._require(key)
        if eff.status == SUCCEEDED:
            return eff
        if eff.connector is not None and eff.connector != connector:
            raise EffectConflict(f"effect {key} is bound to connector {eff.connector!r},"
                                 f" not {connector!r}")
        if connector in self._trusted and perform != self._trusted[connector].adapter:
            raise EffectConflict(f"perform is not the registered adapter for {connector!r}")
        contract = self._trusted.get(connector)
        digest = contract.contract_digest if contract is not None else None
        if contract is not None and not self._scope_unchanged(contract):
            # Dispatching now would land in a namespace the recorded contract
            # does not describe; refuse before anything is claimed.
            raise EffectConflict(f"adapter for {connector!r} no longer reports its contract's"
                                 " idempotency scope")
        if eff.status == UNKNOWN:
            self._refuse_if_in_flight(eff, now)
            eff = self._reconcile(eff, reconcile, self._late_landing_excluded(eff), now)
            if eff.status == SUCCEEDED:
                return eff
            if eff.status == UNKNOWN:
                raise EffectOutcomeUnknown(
                    f"effect {key} outcome unknown; re-dispatch refused ({eff.last_error or 'unreconciled'})")
        eff.action()  # integrity check BEFORE anything is dispatched
        with self.db.tx() as c:
            if guard is not None:
                guard()  # under the journal write lock; raising rolls the claim back
            cur = c.execute("UPDATE effects SET status=?, dispatches=dispatches+1,"
                            " in_flight_owner=?, in_flight_until=?, updated_at=?,"
                            " connector_digest=CASE WHEN connector IS NULL THEN ?"
                            " ELSE connector_digest END,"
                            " connector=COALESCE(connector, ?)"
                            " WHERE idempotency_key=? AND status IN (?,?) AND dispatches=?"
                            " AND (connector IS NULL OR connector=?)",
                            (UNKNOWN, owner, now + self._dispatch_timeout, now, digest, connector,
                             key, PENDING, FAILED, eff.dispatches, connector))
            if cur.rowcount != 1:
                raise EffectInFlight(f"effect {key} was claimed by another dispatcher")
            eff = load_effect(c, key)
        # From here until an outcome is committed, the journal says "unknown".
        try:
            result = perform(eff)
        except EffectFailed as exc:
            return self._settle(eff, FAILED, None, repr(exc), now)
        except Exception as exc:
            self._settle(eff, UNKNOWN, None, f"ambiguous: {exc!r}", now)
            raise EffectOutcomeUnknown(f"effect {key} outcome unknown after {exc!r}") from exc
        return self._settle(eff, SUCCEEDED, result, None, now)

    def _settle(self, eff: Effect, status: str, result: Any, error: str | None,
                now: float) -> Effect:
        blob = json.dumps(result, sort_keys=True) if result is not None else None
        # An ambiguous (``unknown``) outcome keeps the dispatcher's identity on
        # the row, with its window expired: it is the evidence a ``fence`` needs
        # to decide whether that dispatcher can still land the effect.
        keep = status == UNKNOWN
        with self.db.tx() as c:
            c.execute("UPDATE effects SET status=?, result=?, last_error=?,"
                      " in_flight_owner=?, in_flight_until=?, updated_at=?"
                      " WHERE idempotency_key=? AND dispatches=? AND in_flight_owner=?",
                      (status, blob, error, eff.in_flight_owner if keep else None,
                       now if keep else None, now, eff.idempotency_key,
                       eff.dispatches, eff.in_flight_owner))
            return load_effect(c, eff.idempotency_key)

    def expire_in_flight(self, key: str, owner: str, *, now: float | None = None) -> bool:
        """End the in-flight window of an ``unknown`` effect held by ``owner``,
        keeping ``owner`` on the row.

        Only for C4 takeover: the caller must already have fenced ``owner`` at
        the external boundary (a higher epoch is committed at the authority).
        This releases nothing by itself: the effect stays ``unknown`` and a
        re-dispatch still needs reconciliation plus the ``fence`` seam's
        confirmation that ``owner`` is fenced off. Returns True if a live
        window was ended."""
        if not owner:
            raise ValueError("owner required")
        now = self._now(now)
        with self.db.tx() as c:
            cur = c.execute("UPDATE effects SET in_flight_until=?, last_error=?, updated_at=?"
                            " WHERE idempotency_key=? AND status=? AND in_flight_owner=?"
                            " AND in_flight_until > ?",
                            (now, "in-flight window ended: owner fenced", now, key, UNKNOWN,
                             owner, now))
            return cur.rowcount == 1

    # -- helpers -----------------------------------------------------------
    def _require(self, key: str) -> Effect:
        eff = self.get(key)
        if eff is None:
            raise KeyError(f"no journaled intent {key}")
        return eff

    @staticmethod
    def _refuse_if_in_flight(eff: Effect, now: float) -> None:
        if eff.in_flight_owner is not None and eff.in_flight_until is not None \
                and eff.in_flight_until > now:
            raise EffectInFlight(f"effect {eff.idempotency_key} is being dispatched by "
                                 f"{eff.in_flight_owner}")
