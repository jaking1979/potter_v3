"""Synthetic SIGKILL after the trace insert, before SQLite COMMIT."""
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
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
real_connect = sink._connect
class Proxy:
    def __init__(self, inner): self.inner = inner
    def __getattr__(self, key): return getattr(self.inner, key)
    def execute(self, sql, *args):
        result = self.inner.execute(sql, *args)
        if sql.startswith('INSERT INTO events '):
            os.kill(os.getpid(), signal.SIGKILL)
        return result
sink._connect = lambda: Proxy(real_connect())
Handoff(sys.argv[1], sink).reconcile(sys.argv[3])
raise AssertionError('insert seam not reached')
'''

@unittest.skipUnless(hasattr(signal, 'SIGKILL'), 'requires POSIX SIGKILL')
class SinkTransactionKill(unittest.TestCase):
    def test_kill_after_insert_before_commit_recovers_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sink = SqliteTraceSink.bootstrap(root / 'trace.sqlite', environment='synthetic')
            journal = root / 'journal.sqlite'
            Handoff.bootstrap(journal, sink).begin(EVENT, LINE)
            child = subprocess.run([sys.executable, '-c', CHILD, str(journal),
                                    str(sink.path), EVENT],
                                   cwd=Path(__file__).resolve().parents[1], timeout=15)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            with closing(sqlite3.connect(journal)) as c:
                self.assertEqual(c.execute('SELECT state FROM intent').fetchone(), ('pending',))
            reopened = SqliteTraceSink.open_existing(sink.path, environment='synthetic')
            self.assertEqual(reopened.records(), ())
            recovered = Handoff(journal, reopened)
            self.assertTrue(recovered.reconcile(EVENT))
            self.assertFalse(recovered.reconcile(EVENT))
            self.assertEqual(reopened.records(), ((EVENT, LINE),))

if __name__ == '__main__':
    unittest.main()
