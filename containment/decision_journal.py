"""Decision journal for Alpha Bot orders (T31 PASS 2 row 1 item 3 + addendum).

Each order gets a decision ID (New York date + ticker + side + signal version)
and a deterministic UUIDv5 ref_id derived from it, reused on every attempt so
the broker deduplicates. The entry is written `pending` and fsynced BEFORE the
broker call; only a recognized acknowledgment with a well-formed order ID
sets `placed`; any error, timeout or unrecognized response sets `unknown`. An
unresolved (`pending`/`unknown`) or `placed` decision is never resubmitted; only
a reconciled `failed` decision may retry, with the same ref_id.

Entries also act as reservations: until the broker reports a ref_id, its
order's exposure is counted by the guard, which closes the unfilled-order and
restart gaps (Codex P1 on 2945fa4).
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# Fixed namespace so the same decision ID always yields the same ref_id.
REF_NAMESPACE = uuid.UUID("5f0e7c1e-2b1d-5c8a-9d3e-7a1b0c4d2e6f")
UNRESOLVED = ("pending", "unknown")


def decision_id(ny_date: str, symbol: str, side: str, signal_version: str) -> str:
    return f"{ny_date}:{symbol}:{side}:{signal_version}"


def ref_id_for(decision: str) -> str:
    return str(uuid.uuid5(REF_NAMESPACE, decision))


@dataclass(frozen=True)
class Entry:
    decision_id: str
    ref_id: str
    symbol: str
    side: str
    quantity: Decimal
    reserve_usd: Decimal   # buy: worst-case cost; sell: 0
    status: str            # pending | unknown | placed | failed
    order_id: str | None


class JournalUnavailable(RuntimeError):
    pass


class DecisionJournal:
    def __init__(self, path: Path, *, create: bool = False) -> None:
        path = Path(path)
        self.path = path
        if not create and not path.exists():
            raise JournalUnavailable("decision journal missing")
        try:
            # Shared across threads; every use is serialised by the guard lock.
            self._db = sqlite3.connect(str(path), isolation_level=None, timeout=10,
                                       check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            if create:
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS decisions ("
                    " decision_id TEXT PRIMARY KEY, ref_id TEXT NOT NULL UNIQUE,"
                    " symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,"
                    " reserve_usd TEXT NOT NULL, status TEXT NOT NULL"
                    " CHECK (status IN ('pending','unknown','placed','failed')),"
                    " order_id TEXT, updated_at TEXT NOT NULL)")
            self._db.execute("SELECT 1 FROM decisions LIMIT 1")
        except sqlite3.Error as exc:
            raise JournalUnavailable(f"decision journal unusable: {exc}") from exc

    def close(self) -> None:
        self._db.close()

    def _row(self, r: tuple) -> Entry:
        return Entry(r[0], r[1], r[2], r[3], Decimal(r[4]), Decimal(r[5]), r[6], r[7])

    _COLS = "decision_id, ref_id, symbol, side, quantity, reserve_usd, status, order_id"

    def get(self, decision: str) -> Entry | None:
        r = self._db.execute(f"SELECT {self._COLS} FROM decisions WHERE decision_id = ?",
                             (decision,)).fetchone()
        return None if r is None else self._row(r)

    def outstanding(self) -> list[Entry]:
        """Entries whose exposure may be live: pending, unknown or placed."""
        return [self._row(r) for r in self._db.execute(
            f"SELECT {self._COLS} FROM decisions WHERE status IN ('pending','unknown','placed')")]

    def write_pending(self, entry: Entry, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?) "
            "ON CONFLICT(decision_id) DO UPDATE SET status = 'pending', order_id = NULL,"
            " quantity = excluded.quantity, reserve_usd = excluded.reserve_usd,"
            " updated_at = excluded.updated_at WHERE decisions.status = 'failed'",
            (entry.decision_id, entry.ref_id, entry.symbol, entry.side, str(entry.quantity),
             str(entry.reserve_usd), now.isoformat()))
        if self.get(entry.decision_id).status != "pending":
            raise JournalUnavailable("decision is not retryable")

    def set_status(self, decision: str, status: str, now: datetime,
                   order_id: str | None = None) -> None:
        self._db.execute(
            "UPDATE decisions SET status = ?, order_id = COALESCE(?, order_id), updated_at = ?"
            " WHERE decision_id = ?", (status, order_id, now.isoformat(), decision))

    def reconcile(self, known_ref_ids: frozenset[str], now: datetime) -> None:
        """An unresolved entry the broker reports is `placed` (row 1 item 3)."""
        for e in self.outstanding():
            if e.status in UNRESOLVED and e.ref_id in known_ref_ids:
                self.set_status(e.decision_id, "placed", now)

    def all_ref_ids(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT ref_id FROM decisions")}

    def mark_failed(self, decision: str, now: datetime) -> None:
        """Only after the broker confirms no order exists for this ref_id."""
        self.set_status(decision, "failed", now)
