"""Trace join fields (schema v1): a decision row must be tie-able to the effect it authorizes."""
import hashlib
import itertools
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracing.policy_adapter import emit_policy_event
from tracing.trace import DecisionTrace, SCHEMA_V0, SCHEMA_V1

import test_policy_engine as pe

TASK = '12345678-1234-4234-8234-123456789abc'
TRACE = 'abcdefab-cdef-4def-8def-abcdefabcdef'
DEC = 'dec-' + '1' * 32
H = 'b' * 64
BASE = dict(task_id=TASK, trace_id=TRACE, component='policy', outcome='allow', decision_digest='a' * 64,
            environment='synthetic', timestamp='2026-11-06T15:00:00Z', subject_id=DEC)
JOIN = dict(action_hash=H, effect_id='eff-1', capability='gmail.send', reason_code='ok_approved')


def emit(event, **changes):
    out = []
    ctx = dict(task_id=TASK, trace_id=TRACE, environment='synthetic', timestamp='2026-11-06T15:00:00Z', sink=out.append)
    emit_policy_event({'task_id': TASK, 'correlation': 'request', 'decision_id': DEC, 'outcome': 'allow'} | event, **(ctx | changes))
    return out


class Record(unittest.TestCase):
    def test_without_join_fields_the_v0_row_is_unchanged(self):
        row = json.loads(DecisionTrace(**BASE).jsonl())
        self.assertEqual(row['schema'], SCHEMA_V0)
        self.assertFalse([k for k in row if k.startswith(('potter.action', 'potter.effect', 'potter.capab', 'potter.reason'))])

    def test_with_join_fields_the_v1_row_carries_them(self):
        row = json.loads(DecisionTrace(**BASE, **JOIN).jsonl())
        self.assertEqual(row['schema'], SCHEMA_V1)
        self.assertEqual((row['potter.action.hash'], row['potter.effect.id'], row['potter.capability'], row['potter.reason']),
                         (H, 'eff-1', 'gmail.send', 'ok_approved'))

    def test_a_single_join_field_is_enough_for_v1(self):
        for key, value in JOIN.items():
            with self.subTest(key=key):
                self.assertEqual(json.loads(DecisionTrace(**BASE, **{key: value}).jsonl())['schema'], SCHEMA_V1)

    def test_invalid_join_values_are_refused(self):
        bad = {'action_hash': ['xyz', 'B' * 64, 'b' * 63, 5], 'effect_id': ['', 'has space', 'x' * 129, 'a/b', 7],
               'capability': ['Gmail.send', 'gmail send', '', '.x', 'x' * 129, 'a..b'],
               'reason_code': ['Ok', 'ok approved', '', 'x' * 65, '1ok']}
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    DecisionTrace(**BASE, **{key: value})

    def test_boundary_lengths_are_accepted(self):
        DecisionTrace(**BASE, effect_id='x' * 128, capability='a' * 128, reason_code='a' + '_' * 63)


class Adapter(unittest.TestCase):
    def digest(self, **join):
        return json.loads(emit(join)[0])['potter.decision.digest']

    def test_join_fields_flow_through_the_adapter(self):
        row = json.loads(emit({'action_hash': H, 'effect_id': 'eff-1', 'capability': 'gmail.send', 'reason_code': 'ok_approved'})[0])
        self.assertEqual((row['potter.action.hash'], row['potter.effect.id'], row['potter.capability'], row['potter.reason']),
                         (H, 'eff-1', 'gmail.send', 'ok_approved'))

    def test_digest_commits_to_every_join_field(self):
        base = self.digest(**JOIN)
        changes = {'action_hash': 'c' * 64, 'effect_id': 'eff-2', 'capability': 'gmail.draft', 'reason_code': 'ok_other'}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertNotEqual(self.digest(**(JOIN | {key: value})), base)
        self.assertNotEqual(self.digest(**{k: v for k, v in JOIN.items() if k != 'effect_id'}), base)

    def test_v1_digest_known_answer(self):
        material = ['potter.trace.digest/v1', DEC, 'allow', H, 'eff-1', 'gmail.send', 'ok_approved']
        expected = hashlib.sha256(json.dumps(material, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(self.digest(**JOIN), expected)

    def test_legacy_digest_is_kept_when_no_join_fields_are_present(self):
        expected = hashlib.sha256(json.dumps([DEC, 'allow'], separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(self.digest(), expected)

    def test_unrelated_event_fields_are_never_copied(self):
        out = emit(JOIN | {'action_canonical': 'SECRET-BODY', 'parameters': {'body': 'SECRET-BODY'}, 'reason': 'SECRET-BODY'})
        self.assertNotIn('SECRET-BODY', out[0])

    def test_invalid_join_in_an_event_never_reaches_the_sink(self):
        for key, value in {'action_hash': 'zz', 'effect_id': 'has space', 'capability': 'Bad', 'reason_code': 'Bad'}.items():
            out = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                emit(JOIN | {key: value}, sink=out.append)
            self.assertEqual(out, [])


class RealEngine(unittest.TestCase):
    def test_an_approved_send_row_joins_to_its_effect(self):
        ids = itertools.count(1)
        eng = pe.engine(new_id=lambda prefix: f'{prefix}-{next(ids):032x}')
        decision = eng.decide(pe.req(pe.SEND, approval=pe.record(pe.SEND)))
        self.assertEqual(decision.reason_code, 'ok_approved')
        rec = decision.trace_record()
        out = []
        emit_policy_event(rec, task_id=decision.task_id, trace_id=TRACE, environment='synthetic',
                          timestamp='2026-11-06T15:00:00Z', sink=out.append)
        row = json.loads(out[0])
        self.assertEqual(row['potter.action.hash'], pe.SEND.hash())
        self.assertEqual(row['potter.effect.id'], decision.intent.effect_id)
        self.assertEqual((row['potter.capability'], row['potter.reason']), ('gmail.send', 'ok_approved'))
        self.assertNotIn('Dinner', out[0])
        self.assertNotIn('7pm', out[0])


if __name__ == '__main__':
    unittest.main()
