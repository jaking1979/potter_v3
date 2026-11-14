"""Real offline SQLite sink replay behavior; no provider or production access."""
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tracing.policy_sink_bridge import emit_replay_safe
from tracing.sqlite_sink import SqliteTraceSink, TraceStoreRefused
from tracing.trace import DecisionTrace

TASK = '12345678-1234-4234-8234-123456789abc'
TRACE = 'abcdefab-cdef-4def-8def-abcdefabcdef'
SUBJECT = 'dec-' + 'a' * 32


class PolicyReplayBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'traces.sqlite'
        self.sink = SqliteTraceSink.bootstrap(self.path, environment='synthetic')
        self.trace = DecisionTrace(TASK, TRACE, 'policy', 'deny', 'a' * 64,
                                   'synthetic', '2026-11-06T15:00:00Z', SUBJECT)

    def test_retry_after_restart_keeps_first_timestamp(self):
        self.assertTrue(emit_replay_safe(self.sink, self.trace))
        reopened = SqliteTraceSink.open_existing(self.path, environment='synthetic')
        retry = replace(self.trace, timestamp='2026-11-06T16:00:00Z')
        self.assertFalse(emit_replay_safe(reopened, retry))
        self.assertEqual(reopened.records(), ((TRACE, self.trace.jsonl()),))

    def test_same_id_different_identity_or_outcome_refused(self):
        emit_replay_safe(self.sink, self.trace)
        variants = [replace(self.trace, task_id='22345678-1234-4234-8234-123456789abc'),
                    replace(self.trace, outcome='allow'),
                    replace(self.trace, decision_digest='b' * 64),
                    replace(self.trace, subject_id='dec-' + 'b' * 32)]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(TraceStoreRefused):
                emit_replay_safe(self.sink, variant)
        self.assertEqual(self.sink.records(), ((TRACE, self.trace.jsonl()),))

    def test_environment_and_nonpolicy_are_refused(self):
        with self.assertRaises(TraceStoreRefused):
            emit_replay_safe(self.sink, replace(self.trace, environment='replay'))
        with self.assertRaises(ValueError):
            emit_replay_safe(self.sink, replace(self.trace, component='task', subject_id=None))
        self.assertEqual(self.sink.records(), ())

    def test_absent_or_corrupt_store_never_becomes_empty_success(self):
        emit_replay_safe(self.sink, self.trace)
        self.path.unlink()
        with self.assertRaises(Exception):
            emit_replay_safe(self.sink, self.trace)
        self.assertFalse(self.path.exists())

    def test_unrelated_store_refusal_is_never_treated_as_retry(self):
        emit_replay_safe(self.sink, self.trace)
        with patch.object(self.sink, 'put_time_retry', side_effect=TraceStoreRefused('unrelated refusal')):
            with self.assertRaisesRegex(TraceStoreRefused, 'unrelated refusal'):
                emit_replay_safe(self.sink, self.trace)

    def test_uncorrelated_denial_retry_uses_same_transaction_rule(self):
        event_id = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
        denial = {'schema':'potter.trace.uncorrelated-denial/v0',
                  'decision.id':SUBJECT, 'potter.environment':'synthetic',
                  'time':'2026-11-06T15:00:00Z'}
        line = json.dumps(denial, sort_keys=True, separators=(',', ':')) + '\n'
        self.assertTrue(self.sink.put_time_retry(event_id, line))
        denial['time'] = '2026-11-06T16:00:00Z'
        retry = json.dumps(denial, sort_keys=True, separators=(',', ':')) + '\n'
        with patch.object(self.sink, 'records', side_effect=AssertionError('extra read')):
            self.assertFalse(self.sink.put_time_retry(event_id, retry))
        self.assertEqual(self.sink.records(), ((event_id, line),))
        with self.assertRaises(TraceStoreRefused):
            self.sink.put(event_id, retry)

    def test_tampered_record_refuses_retry(self):
        self.assertTrue(emit_replay_safe(self.sink, self.trace))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE events SET record = record || ' '")
        with self.assertRaises(TraceStoreRefused):
            emit_replay_safe(self.sink, replace(self.trace, timestamp='2026-11-06T16:00:00Z'))
