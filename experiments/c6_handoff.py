"""Synthetic-only two-store trace handoff experiment, not an authority or anchor.

The SQLite journal here stands in for an independent effect journal. It is not
C2 and it does not protect either file from coordinated restore or tampering.
"""
import sqlite3

from tracing.sqlite_sink import TraceStoreRefused, validate_jsonl


class HandoffRefused(ValueError):
    pass


class Handoff:
    def __init__(self, journal_path, sink):
        self.path = journal_path
        self.sink = sink

    @classmethod
    def bootstrap(cls, path, sink):
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE intent (event_id TEXT PRIMARY KEY, record TEXT NOT NULL, state TEXT NOT NULL)')
        return cls(path, sink)

    def begin(self, event_id, record):
        record = validate_jsonl(record, self.sink.environment)
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT record FROM intent WHERE event_id=?', (event_id,)).fetchone()
            if row is not None and row[0] != record:
                raise HandoffRefused('event identity conflicts')
            if row is None:
                c.execute('INSERT INTO intent VALUES (?, ?, ?)', (event_id, record, 'pending'))

    def reconcile(self, event_id):
        with sqlite3.connect(self.path) as c:
            row = c.execute('SELECT record, state FROM intent WHERE event_id=?', (event_id,)).fetchone()
        if row is None or row[1] not in ('pending', 'done'):
            raise HandoffRefused('unknown or invalid intent')
        record, state = row
        # A completed intent whose trace has vanished is a fault, never replayed.
        if state == 'done':
            observed = dict(self.sink.records()).get(event_id)
            if observed != record:
                raise HandoffRefused('completed trace absent or changed')
            return False
        self.sink.put(event_id, record)  # local idempotency handles crash after append
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            updated = c.execute("UPDATE intent SET state='done' WHERE event_id=? AND record=? AND state='pending'", (event_id, record))
            if updated.rowcount != 1:
                raise HandoffRefused('intent changed during handoff')
        return True
