"""Offline interprocess regression; not an independent rollback authority."""
import multiprocessing
import queue
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


def reconcile_child(journal, trace, ready, release, results):
    try:
        sink = SqliteTraceSink.open_existing(trace, environment='synthetic')
        handoff = Handoff(journal, sink)
        original = sink.put

        def pause_after_append(event_id, record):
            value = original(event_id, record)
            ready.put('appended')
            if not release.wait(15):
                raise TimeoutError('parent did not release child')
            return value

        sink.put = pause_after_append
        results.put(('ok', handoff.reconcile(EVENT)))
    except Exception as exc:
        results.put(('error', type(exc).__name__))


class CrossProcessRecovery(unittest.TestCase):
    def test_two_processes_reconcile_one_pending_intent(self):
        ctx = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as root:
            journal, trace = Path(root) / 'journal.db', Path(root) / 'trace.db'
            sink = SqliteTraceSink.bootstrap(trace, environment='synthetic')
            handoff = Handoff.bootstrap(journal, sink)
            handoff.begin(EVENT, LINE)
            ready, results, release = ctx.Queue(), ctx.Queue(), ctx.Event()
            children = [ctx.Process(target=reconcile_child,
                        args=(journal, trace, ready, release, results)) for _ in range(2)]
            try:
                for child in children:
                    child.start()
                self.assertEqual([ready.get(timeout=20), ready.get(timeout=20)],
                                 ['appended', 'appended'])
                release.set()
                outcomes = [results.get(timeout=20), results.get(timeout=20)]
                self.assertCountEqual(outcomes, [('ok', True), ('ok', False)])
                for child in children:
                    child.join(timeout=20)
                    self.assertEqual(child.exitcode, 0)
                self.assertEqual(sink.records(), ((EVENT, LINE),))
                self.assertFalse(handoff.reconcile(EVENT))
            finally:
                release.set()
                for child in children:
                    if child.is_alive():
                        child.terminate()
                    child.join(timeout=5)
                ready.close()
                results.close()


if __name__ == '__main__':
    unittest.main()
