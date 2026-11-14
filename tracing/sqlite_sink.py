"""Offline SQLite trace experiment. Synthetic/replay only; no runtime wiring.

One database holds one environment. Explicit bootstrap and existing-store open
are separate: loss of a store must never silently bootstrap a replacement.
Event identity comes from a caller that must keep it stable across retries.
This provides local insert idempotency, not effect/audit atomicity, authenticated
context, restore authority, retention, host fencing, or production durability.
The path and its parents must be trusted and not concurrently replaced.
"""
import json
import hashlib
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from .trace import DecisionTrace, validate_timestamp

VERSION = 'potter.trace.sqlite-offline/v1'
MAX_RECORD_BYTES = 2048
ENVIRONMENTS = frozenset({'synthetic', 'replay'})
DECISION_SCHEMA = 'potter.trace.decision/v0'
DENIAL_SCHEMA = 'potter.trace.uncorrelated-denial/v0'
_DECISION_ID = re.compile(r'dec-[0-9a-f]{32}')
_DECISION_KEYS = frozenset({'schema', 'task.id', 'trace.id', 'potter.component',
    'potter.outcome', 'potter.decision.digest', 'potter.environment', 'time'})
_DENIAL_KEYS = frozenset({'schema', 'decision.id', 'potter.environment', 'time'})
_DDL = {
    'metadata': 'CREATE TABLE metadata (version TEXT NOT NULL, environment TEXT NOT NULL)',
    'events': 'CREATE TABLE events (seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, record TEXT NOT NULL, prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL)'
}
_GENESIS = '0' * 64


def _entry_hash(environment, seq, event_id, record, prev_hash):
    body = json.dumps([VERSION, environment, seq, event_id, record, prev_hash],
                      ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(body).hexdigest()


class TraceStoreRefused(ValueError):
    """Invalid input, conflicting event retry, or invalid existing store."""


class TraceStoreConflict(TraceStoreRefused):
    """Existing event identity has nonidentical content."""


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise TraceStoreRefused('event ID must be a canonical UUID') from None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise TraceStoreRefused('duplicate JSON key')
        result[key] = value
    return result


def validate_jsonl(line, environment):
    """Allow only the two frozen bounded record formats; return canonical JSONL."""
    if environment not in ENVIRONMENTS:
        raise TraceStoreRefused('offline environment required')
    if not isinstance(line, str):
        raise TraceStoreRefused('record must be JSONL text')
    try:
        size = len(line.encode('utf-8'))
    except UnicodeError:
        raise TraceStoreRefused('record encoding invalid') from None
    if size > MAX_RECORD_BYTES or not line.endswith('\n') or line.count('\n') != 1:
        raise TraceStoreRefused('record must be one bounded JSONL line')
    try:
        data = json.loads(line, object_pairs_hook=_pairs)
        if not isinstance(data, dict) or data.get('potter.environment') != environment:
            raise ValueError()
        schema = data.get('schema')
        if schema == DECISION_SCHEMA:
            if set(data) not in (_DECISION_KEYS, _DECISION_KEYS | {'potter.subject.id'}):
                raise ValueError()
            record = DecisionTrace(task_id=data['task.id'], trace_id=data['trace.id'],
                component=data['potter.component'], outcome=data['potter.outcome'],
                decision_digest=data['potter.decision.digest'], environment=environment,
                timestamp=data['time'], subject_id=data.get('potter.subject.id'))
            # An explicitly present null subject is not emitted by the contract.
            if 'potter.subject.id' in data and data['potter.subject.id'] is None:
                raise ValueError()
            return record.jsonl()
        if schema != DENIAL_SCHEMA or set(data) != _DENIAL_KEYS:
            raise ValueError()
        if not isinstance(data['decision.id'], str) or not _DECISION_ID.fullmatch(data['decision.id']):
            raise ValueError()
        validate_timestamp(data['time'])
        return json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n'
    except (ValueError, TypeError, KeyError, RecursionError):
        raise TraceStoreRefused('record does not match bounded trace contract') from None


class SqliteTraceSink:
    """Local synthetic store, opened only through bootstrap/open_existing."""
    def __init__(self, path, environment):
        if environment not in ENVIRONMENTS:
            raise TraceStoreRefused('offline environment required')
        self.path = Path(path).absolute()
        self.environment = environment

    def _connect(self):
        # mode=rw is deliberate: an absent store never becomes an empty store.
        c = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True,
                            timeout=10, isolation_level=None)
        c.execute('PRAGMA trusted_schema=OFF')
        c.execute('PRAGMA synchronous=FULL')
        return c

    def _verify(self, c):
        rows = c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
        expected = [('table', 'events', 'events', _DDL['events']),
                    ('table', 'metadata', 'metadata', _DDL['metadata']),
                    ('index', 'sqlite_autoindex_events_1', 'events', None)]
        if rows != expected:
            raise TraceStoreRefused('existing store schema mismatch')
        meta = c.execute('SELECT version, environment FROM metadata').fetchall()
        if meta != [(VERSION, self.environment)]:
            raise TraceStoreRefused('existing store version or environment mismatch')

    @classmethod
    def bootstrap(cls, path, *, environment):
        result = cls(path, environment)
        # Exclusive creation; an existing store is never overwritten/migrated.
        with result.path.open('xb'):
            pass
        c = result._connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            for sql in _DDL.values():
                c.execute(sql)
            c.execute('INSERT INTO metadata VALUES (?, ?)', (VERSION, environment))
            c.execute('COMMIT')
        finally:
            c.close()
        return cls.open_existing(path, environment=environment)

    @classmethod
    def open_existing(cls, path, *, environment):
        result = cls(path, environment)
        if not result.path.is_file() or result.path.is_symlink():
            raise TraceStoreRefused('existing regular store required')
        c = result._connect()
        try:
            result._verify(c)
            if c.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise TraceStoreRefused('existing store integrity check failed')
        finally:
            c.close()
        return result

    def put(self, event_id, line):
        """True for insertion; False for identical retry; conflict raises.

        A result is returned after the local transaction commits. An uncertain
        caller retries the same ID and exact record. No external effect is run.
        """
        _uuid(event_id)
        canonical = validate_jsonl(line, self.environment)
        c = self._connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            self._verify(c)
            rows = self._checked_rows(c)
            prior = c.execute('SELECT record FROM events WHERE event_id=?', (event_id,)).fetchone()
            if prior:
                if prior[0] != canonical:
                    raise TraceStoreConflict('event retry conflicts with stored record')
                c.execute('COMMIT')
                return False
            seq = len(rows) + 1
            prev_hash = rows[-1][4] if rows else _GENESIS
            digest = _entry_hash(self.environment, seq, event_id, canonical, prev_hash)
            c.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?)',
                      (seq, event_id, canonical, prev_hash, digest))
            c.execute('COMMIT')
            return True
        finally:
            c.close()  # rolls back any unfinished transaction

    def for_event(self, event_id):
        """Bind a stable event ID to the existing emitter's single-argument seam."""
        _uuid(event_id)
        return lambda line: self.put(event_id, line)

    def records(self):
        """Read validated records; no opaque or unvalidated text is returned."""
        c = self._connect()
        try:
            c.execute('BEGIN')
            self._verify(c)
            return tuple((row[1], row[2]) for row in self._checked_rows(c))
        finally:
            c.close()

    def _checked_rows(self, c):
        rows = c.execute('SELECT seq, event_id, record, prev_hash, entry_hash FROM events ORDER BY seq').fetchall()
        prev = _GENESIS
        for expected, (seq, event_id, record, prev_hash, entry_hash) in enumerate(rows, 1):
            _uuid(event_id)
            if (seq != expected or prev_hash != prev or
                validate_jsonl(record, self.environment) != record or
                entry_hash != _entry_hash(self.environment, seq, event_id, record, prev_hash)):
                raise TraceStoreRefused('trace sequence or hash chain invalid')
            prev = entry_hash
        return rows
