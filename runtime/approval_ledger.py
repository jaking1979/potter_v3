"""Production ApprovalLedger for the C0 contract (mandatory C1/C2 gate).

``consume(nonce, intent)`` is ONE effect-journal transaction: it inserts the
pending effect intent and marks the nonce used, or changes nothing. After a
crash, ``pending(nonce)`` (or ``EffectJournal.unresolved()``) finds the bound
intent and recovery reconciles it per T10, so the approval can neither replay
nor strand.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

from ._contract import EffectIntent
from .db import Database, as_database
from .effects import SUCCEEDED, Effect, idempotency_key, insert_intent, load_effect


class _Refuse(Exception):
    pass


class JournalApprovalLedger:
    def __init__(self, db: Database | str | Path, *, clock: Callable[[], float] = time.time):
        self.db = as_database(db)
        self._clock = clock

    def consume(self, nonce: str, intent: EffectIntent) -> bool:
        if not isinstance(nonce, str) or not nonce:
            raise TypeError("nonce must be a non-empty string")
        if not isinstance(intent, EffectIntent):
            raise TypeError("intent must be a C0 EffectIntent")
        intent.action()  # refuse (ApprovalRefused) an intent whose bytes don't match its hash
        key = idempotency_key(intent.task_id, intent.effect_id, intent.action_hash)
        now = self._clock()
        try:
            with self.db.tx() as c:
                if c.execute("SELECT 1 FROM approval_nonces WHERE nonce=?", (nonce,)).fetchone():
                    raise _Refuse()
                try:
                    # A step that already has an effect is never re-bound to a new approval.
                    insert_intent(c, intent, key, now)
                    c.execute("INSERT INTO approval_nonces (nonce, idempotency_key, consumed_at)"
                              " VALUES (?,?,?)", (nonce, key, now))
                except sqlite3.IntegrityError:
                    raise _Refuse() from None
        except _Refuse:
            return False
        return True

    def effect(self, nonce: str) -> Effect | None:
        with self.db.read() as c:
            row = c.execute("SELECT idempotency_key FROM approval_nonces WHERE nonce=?",
                            (nonce,)).fetchone()
            return load_effect(c, row[0]) if row else None

    def pending(self, nonce: str) -> EffectIntent | None:
        """The intent bound at consumption while its effect is unresolved
        (pending, unknown, or failed and still retryable)."""
        eff = self.effect(nonce)
        return eff.intent() if eff is not None and eff.status != SUCCEEDED else None

    def __contains__(self, nonce: str) -> bool:
        with self.db.read() as c:
            return c.execute("SELECT 1 FROM approval_nonces WHERE nonce=?",
                             (nonce,)).fetchone() is not None
