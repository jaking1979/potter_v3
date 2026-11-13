import sqlite3
import tempfile
import unittest
from pathlib import Path

from experiments.c6_handoff import Handoff, HandoffRefused
from tracing.sqlite_sink import SqliteTraceSink, TraceStoreRefused
from tracing.trace import DecisionTrace

EVENT = '22345678-1234-4234-8234-123456789abc'
LINE = DecisionTrace('12345678-1234-4234-8234-123456789abc',
                     'abcdefab-cdef-4def-8def-abcdefabcdef', 'policy',
                     'allow', 'a' * 64, 'synthetic', '2026-11-06T15:00:00Z',
                     'dec-' + 'a' * 32).jsonl()


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sink = SqliteTraceSink.bootstrap(root / 'trace.sqlite', environment='synthetic')
        self.h = Handoff.bootstrap(root / 'journal.sqlite', self.sink)

    def tearDown(self):
        self.tmp.cleanup()

    def test_crash_before_append_reconciles(self):
        self.h.begin(EVENT, LINE)
        self.assertTrue(self.h.reconcile(EVENT))
        self.assertFalse(self.h.reconcile(EVENT))
        self.assertEqual(self.sink.records(), ((EVENT, LINE),))

    def test_crash_after_append_reconciles_once(self):
        self.h.begin(EVENT, LINE)
        self.sink.put(EVENT, LINE)  # crash before journal marked done
        self.assertTrue(self.h.reconcile(EVENT))
        self.assertEqual(self.sink.records(), ((EVENT, LINE),))

    def test_completed_but_missing_trace_is_fault_not_retry(self):
        self.h.begin(EVENT, LINE)
        self.h.reconcile(EVENT)
        with sqlite3.connect(self.sink.path) as c:
            c.execute('DELETE FROM events')
        with self.assertRaises(HandoffRefused):
            self.h.reconcile(EVENT)
        self.assertEqual(self.sink.records(), ())

    def test_duplicate_identity_conflict_refused(self):
        self.h.begin(EVENT, LINE)
        different = LINE.replace('"allow"', '"deny"')
        with self.assertRaises(HandoffRefused):
            self.h.begin(EVENT, different)
        self.assertEqual(self.sink.records(), ())

    def test_unknown_intent_refused(self):
        with self.assertRaises(HandoffRefused):
            self.h.reconcile(EVENT)


if __name__ == '__main__':
    unittest.main()
