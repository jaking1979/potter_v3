"""Real offline SQLite sink replay behavior; no provider or production access."""
from dataclasses import replace
from pathlib import Path
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
        with patch.object(self.sink, 'put', side_effect=TraceStoreRefused('unrelated refusal')):
            with self.assertRaisesRegex(TraceStoreRefused, 'unrelated refusal'):
                emit_replay_safe(self.sink, self.trace)
