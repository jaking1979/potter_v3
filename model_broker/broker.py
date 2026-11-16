"""Synthetic-only ModelBroker: no network, secrets, prompts or runtime wiring.

Money is integer microdollars. Store records across process restarts; callers
must provide authoritative task IDs, periods and cost estimates in production.
This prototype does not establish their provenance or enforce provider costs.
"""
from __future__ import annotations

import sqlite3
import os
from datetime import date
from uuid import UUID
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class Refused(Exception):
    pass


class LedgerUnavailable(Refused):
    """Unknown schema or inconsistent local accounting: no budget is released."""


_MAX_I64 = (1 << 63) - 1  # SQLite INTEGER is signed 64-bit
# Experimental offline input bounds, not authenticated configuration policy.
_MAX_CANDIDATE_LABEL_LENGTH = 200
_MAX_CANDIDATE_SET_MEMBERS = 64

_COLUMNS = ('call_id', 'task_id', 'task_class', 'privacy_class', 'day', 'month',
            'model', 'provider', 'expected', 'worst', 'actual', 'state')


def _canonical_buckets(task_id, day, month):
    """Validate syntax only; callers must still supply authenticated identities/time."""
    if not all(type(x) is str for x in (task_id, day, month)):
        return False
    try:
        parsed_date = date.fromisoformat(day)
        return (str(UUID(task_id)) == task_id and parsed_date.isoformat() == day
                and month == day[:7])
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class Candidate:
    model: str
    provider: str
    approved: bool
    validated_tasks: frozenset[str]
    privacy_classes: frozenset[str]
    expected_usd_micros: int
    worst_usd_micros: int
    available: bool = True

    @classmethod
    def from_config(cls, fields):
        """Validate an offline config record; not an authenticated config loader."""
        if type(fields) is not dict:
            raise ValueError("malformed_candidate")
        try:
            candidate = cls(**fields)
        except TypeError:
            raise ValueError("malformed_candidate") from None
        if not _valid_candidate(candidate):
            raise ValueError("malformed_candidate")
        return candidate

    @classmethod
    def from_json_config(cls, fields):
        """Convert bounded JSON-style lists in a synthetic config record.

        This validates already-decoded input; it does not authenticate config,
        limit the JSON decoder's allocations, or freeze upstream mutations.
        """
        if type(fields) is not dict:
            raise ValueError("malformed_candidate")
        snapshot = fields.copy()
        for name in ("validated_tasks", "privacy_classes"):
            values = snapshot.get(name)
            if type(values) is not list or len(values) > _MAX_CANDIDATE_SET_MEMBERS:
                raise ValueError("malformed_candidate")
            values = tuple(values)
            if (len(values) > _MAX_CANDIDATE_SET_MEMBERS
                    or any(type(v) is not str or not 0 < len(v) <= _MAX_CANDIDATE_LABEL_LENGTH
                           for v in values)):
                raise ValueError("malformed_candidate")
            normalized = frozenset(values)
            if len(normalized) != len(values):
                raise ValueError("malformed_candidate")
            snapshot[name] = normalized
        return cls.from_config(snapshot)


def _valid_label(value):
    if type(value) is not str or not 0 < len(value) <= _MAX_CANDIDATE_LABEL_LENGTH:
        return False
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def _valid_candidate(c):
    return (type(c) is Candidate
        and _valid_label(c.model)
        and _valid_label(c.provider)
        and type(c.approved) is bool and type(c.available) is bool
        and type(c.validated_tasks) is frozenset
        and len(c.validated_tasks) <= _MAX_CANDIDATE_SET_MEMBERS
        and all(_valid_label(v) for v in c.validated_tasks)
        and type(c.privacy_classes) is frozenset
        and len(c.privacy_classes) <= _MAX_CANDIDATE_SET_MEMBERS
        and all(_valid_label(v) for v in c.privacy_classes)
        and type(c.expected_usd_micros) is int and type(c.worst_usd_micros) is int
        and 0 <= c.expected_usd_micros <= c.worst_usd_micros <= _MAX_I64)


@dataclass(frozen=True)
class Request:
    call_id: str
    task_id: str
    task_class: str
    privacy_class: str
    day: str
    month: str


@dataclass(frozen=True)
class Caps:
    request: int
    task: int
    day: int
    month: int


class Broker:
    """Offline local accounting. Every operation closes its SQLite connection.

    Ledger triggers prevent ordinary UPDATE/DELETE, not a database owner's
    deliberate tampering or restoration of an older whole database.
    """

    @classmethod
    def bootstrap(cls, path: str | Path, caps: Caps):
        """Create one new synthetic ledger exclusively; never reset a store."""
        cls._validate_caps(caps)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise LedgerUnavailable('bootstrap_refused') from exc
        os.close(fd)
        # Leave failed initialization for explicit inspection, never guess that
        # an existing empty store is safe to initialize on a later open.
        instance = cls.__new__(cls)
        instance._open(path, caps, initialize=True)
        return instance

    @staticmethod
    def _validate_caps(caps):
        if type(caps) is not Caps or any(type(x) is not int or not 0 <= x <= _MAX_I64 for x in (caps.request, caps.task, caps.day, caps.month)):
            raise ValueError("caps must be SQLite-range nonnegative integer microdollars")

    def __init__(self, path: str | Path, caps: Caps):
        """Open existing state; missing or empty files are never bootstrapped."""
        self._open(path, caps, initialize=False)

    def _open(self, path, caps, *, initialize):
        self._validate_caps(caps)
        self.path, self.caps = str(path), caps
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                if not initialize:
                    raise LedgerUnavailable('empty_store')
                db.execute("""CREATE TABLE broker_calls (
                call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                task_class TEXT NOT NULL, privacy_class TEXT NOT NULL,
                day TEXT NOT NULL, month TEXT NOT NULL, model TEXT NOT NULL,
                provider TEXT NOT NULL, expected INTEGER NOT NULL,
                worst INTEGER NOT NULL, actual INTEGER,
                state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','settled','overrun'))
                )""")
            elif tables not in ({'broker_calls'}, {'broker_calls', 'broker_meta', 'broker_entries'}):
                raise LedgerUnavailable('unknown_schema')
            if tuple(r[1] for r in db.execute('PRAGMA table_info(broker_calls)')) != _COLUMNS:
                raise LedgerUnavailable('unknown_calls_schema')
            if 'broker_meta' in tables:
                if db.execute('SELECT version FROM broker_meta').fetchall() != [(1,)]:
                    raise LedgerUnavailable('unknown_schema_version')
            else:
                # The only supported migration is the original unversioned
                # prototype. Snapshot entries are explicitly not history.
                rows = db.execute('SELECT * FROM broker_calls').fetchall()
                for row in rows:
                    self._validate_call(row)
                db.execute('CREATE TABLE broker_meta (version INTEGER NOT NULL)')
                db.execute('INSERT INTO broker_meta VALUES (1)')
                db.execute("""CREATE TABLE broker_entries (
                    sequence INTEGER PRIMARY KEY, call_id TEXT NOT NULL REFERENCES broker_calls(call_id),
                    kind TEXT NOT NULL CHECK(kind IN ('reservation','settlement')),
                    amount INTEGER NOT NULL CHECK(typeof(amount)='integer'),
                    origin TEXT NOT NULL CHECK(origin IN ('operation','legacy_snapshot')),
                    UNIQUE(call_id,kind))""")
                db.execute("""CREATE TRIGGER broker_entries_no_update BEFORE UPDATE ON broker_entries
                    BEGIN SELECT RAISE(ABORT,'append_only'); END""")
                db.execute("""CREATE TRIGGER broker_entries_no_delete BEFORE DELETE ON broker_entries
                    BEGIN SELECT RAISE(ABORT,'append_only'); END""")
                for row in rows:
                    self._entry(db, row[0], 'reservation', row[9], 'legacy_snapshot')
                    if row[11] in ('settled', 'overrun'):
                        self._entry(db, row[0], 'settlement', row[10] - row[9], 'legacy_snapshot')
            self._validate_ledger(db)
            db.commit()

    @contextmanager
    def _db(self):
        # mode=rw is important on every operation, not only at construction:
        # losing the file must not produce a fresh budget on an old instance.
        try:
            db = sqlite3.connect(Path(self.path).absolute().as_uri() + '?mode=rw',
                                 uri=True, timeout=10, isolation_level=None)
        except sqlite3.OperationalError as exc:
            raise LedgerUnavailable('store_unavailable') from exc
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            yield db
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _validate_call(row):
        strings, expected, worst, actual, state = row[:8], *row[8:]
        if (not all(isinstance(x, str) and x for x in strings)
                or not _canonical_buckets(row[1], row[4], row[5])
                or type(expected) is not int or type(worst) is not int
                or not 0 <= expected <= worst):
            raise LedgerUnavailable('invalid_call_record')
        if state in ('reserved', 'dispatched'):
            valid = actual is None
        elif state in ('settled', 'overrun'):
            valid = (type(actual) is int and actual >= 0
                     and (actual > worst) == (state == 'overrun'))
        else:
            valid = False
        if not valid:
            raise LedgerUnavailable('invalid_call_state')

    @staticmethod
    def _entry(db, call_id, kind, amount, origin='operation'):
        db.execute('INSERT INTO broker_entries(call_id,kind,amount,origin) VALUES (?,?,?,?)',
                   (call_id, kind, amount, origin))

    def _validate_ledger(self, db):
        if db.execute('SELECT version FROM broker_meta').fetchall() != [(1,)]:
            raise LedgerUnavailable('unknown_schema_version')
        rows = db.execute('SELECT * FROM broker_calls').fetchall()
        expected = {}
        for row in rows:
            self._validate_call(row)
            expected[(row[0], 'reservation')] = row[9]
            if row[11] in ('settled', 'overrun'):
                expected[(row[0], 'settlement')] = row[10] - row[9]
        actual = {}
        for call_id, kind, amount, origin in db.execute(
                'SELECT call_id,kind,amount,origin FROM broker_entries'):
            key = (call_id, kind)
            if (key in actual or type(amount) is not int
                    or origin not in ('operation', 'legacy_snapshot')):
                raise LedgerUnavailable('invalid_ledger_entry')
            actual[key] = amount
        if actual != expected:
            raise LedgerUnavailable('ledger_inconsistent')

    def entries(self, call_id):
        """Return immutable tuples for offline inspection, never provider evidence."""
        with self._db() as db:
            db.execute('BEGIN')
            self._validate_ledger(db)
            return tuple(db.execute('SELECT kind,amount,origin FROM broker_entries '
                                    'WHERE call_id=? ORDER BY sequence', (call_id,)))

    def reserve(self, req: Request, candidates: list[Candidate]):
        if not all(isinstance(v, str) and v for v in (req.call_id, req.task_id, req.task_class, req.privacy_class, req.day, req.month)):
            raise Refused("invalid_request")
        if not _canonical_buckets(req.task_id, req.day, req.month):
            raise Refused("invalid_budget_bucket")
        # Configuration is a finite concrete sequence. Do not iterate an
        # arbitrary caller object (which may consume state or never finish).
        if type(candidates) not in (list, tuple) or len(candidates) > 1024:
            raise Refused("malformed_candidate")
        candidates = tuple(candidates)
        # A shared list can grow after the precheck and before copying.
        if len(candidates) > 1024:
            raise Refused("malformed_candidate")
        eligible = []
        for c in candidates:
            if not _valid_candidate(c):
                raise Refused("malformed_candidate")
            if (c.approved and c.available and req.task_class in c.validated_tasks
                    and req.privacy_class in c.privacy_classes):
                eligible.append(c)
        eligible.sort(key=lambda x: (x.expected_usd_micros, x.worst_usd_micros, x.model, x.provider))
        if not eligible:
            raise Refused("no_eligible_candidate")
        # No implicit price escalation: if the cheapest eligible candidate
        # exceeds any ceiling, refuse rather than picking a dearer route.
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_ledger(db)
            if db.execute("SELECT 1 FROM broker_calls WHERE state='overrun' LIMIT 1").fetchone():
                raise Refused("unresolved_cost_variance")
            if db.execute("SELECT 1 FROM broker_calls WHERE call_id=?", (req.call_id,)).fetchone():
                raise Refused("duplicate_call_id")
            # Original reservation remains forever; a separate settlement
            # entry adjusts only its unused difference (or records overrun).
            def total(where, value):
                row = db.execute('SELECT COALESCE(SUM(e.amount),0) FROM broker_entries e '
                                 'JOIN broker_calls c USING(call_id) WHERE c.' + where + '=?',
                                 (value,)).fetchone()
                return row[0]
            task_used = total("task_id", req.task_id)
            day_used = total("day", req.day)
            month_used = total("month", req.month)
            c = eligible[0]
            w = c.worst_usd_micros
            if not (w <= self.caps.request and task_used + w <= self.caps.task
                    and day_used + w <= self.caps.day and month_used + w <= self.caps.month):
                raise Refused("budget_ceiling")
            db.execute("INSERT INTO broker_calls VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'reserved')",
                       (req.call_id, req.task_id, req.task_class, req.privacy_class,
                        req.day, req.month, c.model, c.provider, c.expected_usd_micros, w))
            self._entry(db, req.call_id, 'reservation', w)
            db.commit()
            return c

    def dispatch_once(self, call_id: str, *, dispatch_day: str):
        """Offline admission using a caller-supplied date; NOT a trusted clock.

        A stale reservation cannot be dispatched in a new day or month. It
        remains fully reserved, including across restarts. This check does not
        guard a real provider invocation after this transaction commits.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_ledger(db)
            if db.execute("SELECT 1 FROM broker_calls WHERE state='overrun' LIMIT 1").fetchone():
                raise Refused('unresolved_cost_variance')
            row = db.execute('SELECT task_id,day,month,worst,state FROM broker_calls WHERE call_id=?',
                             (call_id,)).fetchone()
            if row is None or row[4] != 'reserved':
                raise Refused('not_reserved')
            # Compare the canonical supplied day while holding the write
            # transaction. No stale reservation is migrated or released.
            if not _canonical_buckets(row[0], dispatch_day,
                                      dispatch_day[:7] if type(dispatch_day) is str else None):
                raise Refused('invalid_dispatch_day')
            if dispatch_day != row[1]:
                raise Refused('reservation_period_changed')
            # A reservation is not irrevocable dispatch permission. Another
            # call can overrun or configured caps can tighten before dispatch.
            for column, value, cap in zip(('task_id', 'day', 'month'), row[:3],
                                          (self.caps.task, self.caps.day, self.caps.month)):
                used = db.execute('SELECT COALESCE(SUM(e.amount),0) FROM broker_entries e '
                                  'JOIN broker_calls c USING(call_id) WHERE c.' + column + '=?',
                                  (value,)).fetchone()[0]
                if used > cap:
                    raise Refused('budget_ceiling')
            if row[3] > self.caps.request:
                raise Refused('budget_ceiling')
            changed = db.execute("UPDATE broker_calls SET state='dispatched' WHERE call_id=? AND state='reserved'", (call_id,)).rowcount
            if changed != 1:
                raise Refused("not_reserved")
            db.commit()

    def settle(self, call_id: str, actual_usd_micros: int):
        if type(actual_usd_micros) is not int or not 0 <= actual_usd_micros <= _MAX_I64:
            raise Refused("unknown_or_invalid_cost")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_ledger(db)
            row = db.execute("SELECT state,worst FROM broker_calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None or row[0] != "dispatched":
                raise Refused("not_dispatched")
            state = "overrun" if actual_usd_micros > row[1] else "settled"
            self._entry(db, call_id, 'settlement', actual_usd_micros - row[1])
            db.execute("UPDATE broker_calls SET state=?,actual=? WHERE call_id=?", (state,actual_usd_micros,call_id))
            db.commit()
            if state == "overrun":
                raise Refused("cost_variance")

    def status(self, call_id: str):
        with self._db() as db:
            db.execute('BEGIN')
            self._validate_ledger(db)
            return db.execute("SELECT model,provider,worst,actual,state FROM broker_calls WHERE call_id=?", (call_id,)).fetchone()




