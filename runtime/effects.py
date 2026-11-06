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
``fence`` (C4 seam; default refuses) or a connector that declares
``enforces_idempotency_key = True``. ``"succeeded"`` resolves it without
re-execution; ``"still_unknown"`` (or anything unrecognised, or an unfenced
``"failed"``) refuses the retry and leaves it unresolved.

Executors run only ``effect.action()``: the persisted canonical action,
integrity-checked against its hash (C0 "one snapshot" rule).

Not here: the fencing implementation itself (C4 fills the ``fence`` seam);
the outbox (C3); real connectors.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ._contract import BoundAction, EffectIntent
from .db import Database, as_database

# Fixed namespace: the same (task, step, action) always yields the same key.
EFFECT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:potter:v3:c2:effect-journal")

PENDING, UNKNOWN, SUCCEEDED, FAILED = "pending", "unknown", "succeeded", "failed"
STILL_UNKNOWN = "still_unknown"
RECONCILE_VERDICTS = (SUCCEEDED, FAILED, STILL_UNKNOWN)
DEFAULT_DISPATCH_TIMEOUT_S = 300.0
NOT_APPLIED_UNFENCED = ("reconcile reports not applied, but the earlier dispatch may still land;"
                        " re-dispatch needs a fence (C4) or an idempotency-enforcing connector")


def enforces_idempotency_key(connector: Any) -> bool:
    """True only if the connector (a perform callable, or the object it is
    bound to) explicitly declares ``enforces_idempotency_key = True``: the
    external system applies a given idempotency key at most once."""
    for obj in (connector, getattr(connector, "__self__", None)):
        if obj is not None and getattr(obj, "enforces_idempotency_key", False) is True:
            return True
    return False


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
    """This task step is already bound to a different action."""


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

    def intent(self) -> EffectIntent:
        return EffectIntent(self.effect_id, self.task_id, self.action_hash,
                            self.approval_id or "", self.action_canonical)

    def action(self) -> BoundAction:
        """The exact journaled action; raises ApprovalRefused if altered."""
        return self.intent().action()


_COLS = ("idempotency_key, task_id, effect_id, action_hash, action_canonical, approval_id,"
         " status, dispatches, in_flight_owner, in_flight_until, result, last_error")


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
                 fence: Callable[[Effect], bool] | None = None):
        self.db = as_database(db)
        self._fence = fence   # C4 seam; None = no fencing = refuse unfenced re-dispatch
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
                  enforces_idempotency_key: bool = False, now: float | None = None) -> Effect:
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
        allow_failed = enforces_idempotency_key is True or self._fenced(eff)
        return self._reconcile(eff, reconcile, allow_failed, now)

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
            # land later. Without a fence or an idempotency-enforcing connector,
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
                reconcile: Callable[[Effect], str], *, owner: str,
                now: float | None = None) -> Effect:
        """Run the journaled effect at most as often as the external state allows:

        * ``succeeded`` -> returned as is, ``perform`` is not called.
        * ``pending`` / ``failed`` -> dispatched once with the same key.
        * ``unknown``   -> ``reconcile`` first. ``succeeded`` resolves it without
          re-execution. ``failed`` permits ONE re-dispatch with the same key, and
          only if a late landing of the earlier dispatch is excluded: the
          configured ``fence`` (C4 seam) confirms the earlier dispatcher is fenced
          off, or the connector declares ``enforces_idempotency_key = True`` (the
          external system dedupes on ``effect.idempotency_key``). Otherwise, and
          for ``still_unknown``, EffectOutcomeUnknown is raised and the effect
          stays in ``unresolved()`` for authoritative reconciliation. An expired
          in-flight claim is never by itself permission to re-dispatch.

        ``perform(effect)`` must use ``effect.action()`` and pass
        ``effect.idempotency_key`` to the external system.
        """
        if not owner:
            raise ValueError("owner required")
        now = self._now(now)
        eff = self._require(key)
        if eff.status == SUCCEEDED:
            return eff
        if eff.status == UNKNOWN:
            self._refuse_if_in_flight(eff, now)
            allow_failed = enforces_idempotency_key(perform) or self._fenced(eff)
            eff = self._reconcile(eff, reconcile, allow_failed, now)
            if eff.status == SUCCEEDED:
                return eff
            if eff.status == UNKNOWN:
                raise EffectOutcomeUnknown(
                    f"effect {key} outcome unknown; re-dispatch refused ({eff.last_error or 'unreconciled'})")
        eff.action()  # integrity check BEFORE anything is dispatched
        with self.db.tx() as c:
            cur = c.execute("UPDATE effects SET status=?, dispatches=dispatches+1,"
                            " in_flight_owner=?, in_flight_until=?, updated_at=?"
                            " WHERE idempotency_key=? AND status IN (?,?) AND dispatches=?",
                            (UNKNOWN, owner, now + self._dispatch_timeout, now,
                             key, PENDING, FAILED, eff.dispatches))
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
        with self.db.tx() as c:
            c.execute("UPDATE effects SET status=?, result=?, last_error=?, in_flight_owner=NULL,"
                      " in_flight_until=NULL, updated_at=?"
                      " WHERE idempotency_key=? AND dispatches=? AND in_flight_owner=?",
                      (status, blob, error, now, eff.idempotency_key,
                       eff.dispatches, eff.in_flight_owner))
            return load_effect(c, eff.idempotency_key)

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
