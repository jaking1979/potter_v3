import sqlite3
import tempfile
import unittest
from pathlib import Path

from tracing.sqlite_sink import SqliteTraceSink, TraceStoreRefused
from tracing.trace import DecisionTrace

EVENT = '22345678-1234-4234-8234-123456789abc'
LINE = DecisionTrace('12345678-1234-4234-8234-123456789abc',
                     'abcdefab-cdef-4def-8def-abcdefabcdef', 'policy', 'deny',
                     'a' * 64, 'synthetic', '2026-11-06T15:00:00Z').jsonl()

class CatalogGuardTests(unittest.TestCase):
    def test_altered_catalog_refuses_read_write_and_reopen(self):
        for ddl in ('CREATE TRIGGER erase AFTER INSERT ON events BEGIN DELETE FROM events WHERE seq=NEW.seq; END',
                    'CREATE VIEW leak AS SELECT * FROM events',
                    'CREATE INDEX extra ON events(record)'):
            with self.subTest(ddl=ddl), tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'trace.db'
                sink = SqliteTraceSink.bootstrap(path, environment='synthetic')
                with sqlite3.connect(path) as c:
                    c.execute(ddl)
                for action in (lambda: sink.put(EVENT, LINE), sink.records,
                               lambda: SqliteTraceSink.open_existing(path, environment='synthetic')):
                    with self.assertRaises(TraceStoreRefused):
                        action()
                with sqlite3.connect(path) as c:
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)

