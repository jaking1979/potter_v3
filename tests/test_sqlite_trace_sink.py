import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from tracing.policy_adapter import emit_policy_audit
from tracing.sqlite_sink import SqliteTraceSink, TraceStoreRefused, validate_jsonl
from tracing.trace import DecisionTrace

TASK = '12345678-1234-4234-8234-123456789abc'
TRACE = 'abcdefab-cdef-4def-8def-abcdefabcdef'
EVENT = '22345678-1234-4234-8234-123456789abc'
TIME = '2026-11-06T15:00:00Z'
DECISION = 'dec-' + 'a' * 32


def line(environment='synthetic', outcome='allow'):
    return DecisionTrace(TASK, TRACE, 'policy', outcome, 'a' * 64,
                         environment, TIME, DECISION).jsonl()


def concurrent_writer(args):
    path, event_id, record = args
    sink = SqliteTraceSink.open_existing(path, environment='synthetic')
    return sink.put(event_id, record)


def crash_writer(path, commit):
    if commit:
        SqliteTraceSink.open_existing(path, environment='synthetic').put(EVENT, line())
    else:
        c = sqlite3.connect(path, isolation_level=None)
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO events VALUES (?, ?)', (EVENT, line()))
    os._exit(17)


class SqliteTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'trace.sqlite'
        self.sink = SqliteTraceSink.bootstrap(self.path, environment='synthetic')

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_and_identical_retry(self):
        self.assertTrue(self.sink.put(EVENT, line()))
        reopened = SqliteTraceSink.open_existing(self.path, environment='synthetic')
        self.assertFalse(reopened.put(EVENT, line()))
        # Cosmetic JSON spacing does not make a conflicting record.
        self.assertFalse(reopened.put(EVENT, json.dumps(json.loads(line())) + '\n'))
        self.assertEqual(reopened.records(), ((EVENT, line()),))

    def test_process_crash_before_and_after_local_commit(self):
        # SQLite transaction recovery, not host power-loss or effect atomicity.
        for committed in (False, True):
            proc = multiprocessing.get_context('spawn').Process(
                target=crash_writer, args=(str(self.path), committed))
            proc.start()
            proc.join(10)
            if proc.is_alive():
                proc.terminate()
                proc.join()
                self.fail('synthetic crash process timed out')
            self.assertEqual(proc.exitcode, 17)
            reopened = SqliteTraceSink.open_existing(self.path, environment='synthetic')
            self.assertEqual(len(reopened.records()), int(committed))
        self.assertFalse(reopened.put(EVENT, line()))

    def test_conflicting_retry_preserves_first_record(self):
        self.sink.put(EVENT, line())
        with self.assertRaises(TraceStoreRefused):
            self.sink.put(EVENT, line(outcome='deny'))
        self.assertEqual(self.sink.records(), ((EVENT, line()),))
        self.assertTrue(self.sink.put(str(uuid4()), line(outcome='deny')))

    def test_existing_open_does_not_bootstrap(self):
        absent = self.path.parent / 'absent.sqlite'
        with self.assertRaises(TraceStoreRefused):
            SqliteTraceSink.open_existing(absent, environment='synthetic')
        self.assertFalse(absent.exists())
        with self.assertRaises(FileExistsError):
            SqliteTraceSink.bootstrap(self.path, environment='synthetic')
        self.assertEqual(self.sink.records(), ())

    def test_environment_separation(self):
        with self.assertRaises(TraceStoreRefused):
            SqliteTraceSink.open_existing(self.path, environment='replay')
        for env in ('replay', 'staging', 'production'):
            with self.assertRaises(TraceStoreRefused):
                self.sink.put(EVENT, line(environment=env))
        for env in ('staging', 'production'):
            with self.assertRaises(TraceStoreRefused):
                SqliteTraceSink.bootstrap(self.path.parent / env, environment=env)
            self.assertFalse((self.path.parent / env).exists())

    def test_separate_denial_and_emitter_seam(self):
        event = {'correlation': 'missing', 'task_id': None,
                 'decision_id': DECISION, 'outcome': 'deny', 'raw': 'SECRET'}
        emit_policy_audit(event, task_id=None, trace_id=None, environment='synthetic',
                          timestamp=TIME, sink=self.sink.for_event(EVENT))
        row = self.sink.records()[0]
        self.assertEqual(json.loads(row[1])['schema'], 'potter.trace.uncorrelated-denial/v0')
        self.assertNotIn('SECRET', row[1])
        self.assertNotIn('task.id', json.loads(row[1]))
        # Same event ID cannot be reused across the two schemas.
        with self.assertRaises(TraceStoreRefused):
            self.sink.put(EVENT, line())

    def test_malformed_payloads_never_stored(self):
        good = json.loads(line())
        cases = [line() + line(), line().rstrip('\n'), 'SECRET' * 1000 + '\n',
                 'null\n', '[]\n', '{"schema":1,"schema":2}\n']
        for changes in ({'raw': 'SECRET'}, {'time': '2026-11-06X15:00:00Z'},
                        {'potter.subject.id': None}, {'potter.outcome': 'SECRET'},
                        {'schema': 'arbitrary'}, {'task.id': 'SECRET'},
                        {'potter.decision.digest': 'SECRET'}):
            cases.append(json.dumps(good | changes) + '\n')
        denial = {'schema': 'potter.trace.uncorrelated-denial/v0',
                  'decision.id': DECISION, 'potter.environment': 'synthetic', 'time': TIME}
        cases.append(json.dumps(denial | {'raw': 'SECRET'}) + '\n')
        cases.append(json.dumps(denial | {'decision.id': 'SECRET'}) + '\n')
        for bad in cases:
            with self.subTest(bad=bad[:40]), self.assertRaises(TraceStoreRefused):
                self.sink.put(EVENT, bad)
        self.assertEqual(self.sink.records(), ())
        self.assertNotIn(b'SECRET', self.path.read_bytes())

    def test_event_id_is_bounded(self):
        for bad in ('SECRET', 'A' * 10000, 1, None, EVENT.upper()):
            # EVENT contains no alpha; use TRACE for case-sensitive refusal.
            if bad == EVENT:
                continue
            with self.assertRaises(TraceStoreRefused):
                self.sink.put(bad, line())
        with self.assertRaises(TraceStoreRefused):
            self.sink.put(TRACE.upper(), line())

    def test_concurrent_processes_one_insert(self):
        args = [(str(self.path), EVENT, line())] * 16
        with multiprocessing.get_context('spawn').Pool(4) as pool:
            results = pool.map(concurrent_writer, args)
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.sink.records()), 1)

    def test_concurrent_distinct_events_survive(self):
        ids = [str(uuid4()) for _ in range(16)]
        args = [(str(self.path), event, line()) for event in ids]
        with multiprocessing.get_context('spawn').Pool(4) as pool:
            results = pool.map(concurrent_writer, args)
        self.assertTrue(all(results))
        self.assertEqual({row[0] for row in self.sink.records()}, set(ids))

    def test_schema_or_metadata_drift_fails_closed(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE metadata SET version='unsupported'")
        for call in (lambda: self.sink.put(EVENT, line()), self.sink.records,
                     lambda: SqliteTraceSink.open_existing(self.path, environment='synthetic')):
            with self.assertRaises(TraceStoreRefused):
                call()
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE metadata SET version='potter.trace.sqlite-offline/v0'")
            c.execute('CREATE TABLE unexpected (raw TEXT)')
        with self.assertRaises(TraceStoreRefused):
            self.sink.put(EVENT, line())

    def test_invalid_existing_and_tampered_record_refused(self):
        empty = self.path.parent / 'empty.sqlite'
        empty.touch()
        with self.assertRaises(TraceStoreRefused):
            SqliteTraceSink.open_existing(empty, environment='synthetic')
        self.sink.put(EVENT, line())
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE events SET record=?', ('SECRET\n',))
        with self.assertRaises(TraceStoreRefused):
            self.sink.records()

    def test_symlink_refused(self):
        alias = self.path.parent / 'alias.sqlite'
        alias.symlink_to(self.path)
        with self.assertRaises(TraceStoreRefused):
            SqliteTraceSink.open_existing(alias, environment='synthetic')


if __name__ == '__main__':
    unittest.main()
