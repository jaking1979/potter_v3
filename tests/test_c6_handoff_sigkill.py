"""Synthetic process-kill recovery at the two-store handoff seam.

Only temporary local SQLite stores are used. These tests do not exercise C2,
an independent anchor, a production process or host failure.
"""
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.c6_handoff import Handoff
from tracing.sqlite_sink import SqliteTraceSink
from tracing.trace import DecisionTrace

EVENT = '22345678-1234-4234-8234-123456789abc'
LINE = DecisionTrace('12345678-1234-4234-8234-123456789abc',
                     'abcdefab-cdef-4def-8def-abcdefabcdef', 'policy',
                     'allow', 'a' * 64, 'synthetic', '2026-11-06T15:00:00Z',
                     'dec-' + 'a' * 32).jsonl()

CHILD = r'''
import os, signal, sys
from experiments.c6_handoff import Handoff
from tracing.sqlite_sink import SqliteTraceSink
sink = SqliteTraceSink.open_existing(sys.argv[2], environment='synthetic')
handoff = Handoff(sys.argv[1], sink)
original = sink.put
def killed_put(event_id, record):
    if sys.argv[4] == 'before':
        os.kill(os.getpid(), signal.SIGKILL)
    original(event_id, record)
    os.kill(os.getpid(), signal.SIGKILL)
sink.put = killed_put
handoff.reconcile(sys.argv[3])
raise AssertionError('SIGKILL seam was not reached')
'''


class ProcessKillRecovery(unittest.TestCase):
    def test_restart_recovers_before_and_after_trace_commit(self):
        for kill_at in ('before', 'after'):
            with self.subTest(kill_at=kill_at), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sink = SqliteTraceSink.bootstrap(root / 'trace.sqlite', environment='synthetic')
                journal = root / 'journal.sqlite'
                Handoff.bootstrap(journal, sink).begin(EVENT, LINE)
                child = subprocess.run([sys.executable, '-c', CHILD, str(journal),
                                        str(sink.path), EVENT, kill_at],
                                       cwd=Path(__file__).resolve().parents[1], timeout=15)
                self.assertEqual(child.returncode, -signal.SIGKILL)
                with sqlite3.connect(journal) as c:
                    self.assertEqual(c.execute('SELECT state FROM intent').fetchone(), ('pending',))
                sink = SqliteTraceSink.open_existing(sink.path, environment='synthetic')
                expected_before = () if kill_at == 'before' else ((EVENT, LINE),)
                self.assertEqual(sink.records(), expected_before)
                recovered = Handoff(journal, sink)
                self.assertTrue(recovered.reconcile(EVENT))
                self.assertFalse(recovered.reconcile(EVENT))
                self.assertEqual(sink.records(), ((EVENT, LINE),))


if __name__ == '__main__':
    unittest.main()
