import json
import unittest
from tracing.policy_adapter import emit_policy_event, emit_uncorrelated_denial

TASK = '12345678-1234-4234-8234-123456789abc'
TRACE = 'abcdefab-cdef-4def-8def-abcdefabcdef'
ID = 'dec-' + '1' * 32


class AdapterTest(unittest.TestCase):
    def emit(self, event, sink, **changes):
        ctx = dict(task_id=TASK, trace_id=TRACE, environment='synthetic',
                   timestamp='2026-11-06T15:00:00Z', sink=sink)
        event = {'task_id': TASK, 'correlation': 'request'} | event
        emit_policy_event(event, **(ctx | changes))

    def test_event_is_joined_without_copying_untrusted_fields(self):
        out = []
        self.emit({'decision_id': ID, 'outcome': 'deny', 'reason': 'secret text'}, out.append)
        record = json.loads(out[0])
        self.assertEqual((record['task.id'], record['potter.subject.id']), (TASK, ID))
        self.assertNotIn('secret text', out[0])

    def test_bad_event_or_context_never_reaches_sink(self):
        for event, ctx in [({'decision_id': 'private', 'outcome': 'allow'}, {}),
                           ({'outcome': 'allow'}, {}),
                           ({'decision_id': ID, 'outcome': 'private'}, {}),
                           ({'decision_id': ID, 'outcome': 'allow', 'correlation': 'missing'}, {}),
                           ({'decision_id': ID, 'outcome': 'allow', 'task_id': TRACE}, {}),
                           ({'decision_id': ID, 'outcome': 'allow'}, {'task_id': 'private'})]:
            out = []
            with self.subTest(event=event, ctx=ctx), self.assertRaises(ValueError):
                self.emit(event, out.append, **ctx)
            self.assertEqual(out, [])

    def test_sink_failure_propagates(self):
        def fail(_):
            raise OSError('unavailable')
        with self.assertRaises(OSError):
            self.emit({'decision_id': ID, 'outcome': 'allow'}, fail)

    def test_missing_correlation_audit_never_invents_task(self):
        out = []
        event = {'decision_id': ID, 'outcome': 'deny', 'task_id': None,
                 'correlation': 'missing', 'reason': 'private'}
        emit_uncorrelated_denial(event, environment='synthetic', timestamp='2026-11-06T15:00:00Z', sink=out.append)
        record = json.loads(out[0])
        self.assertEqual(record['decision.id'], ID)
        self.assertNotIn('task.id', record)
        self.assertNotIn('private', out[0])
        for changes in ({'outcome': 'allow'}, {'task_id': TASK}, {'decision_id': 'private'}):
            with self.assertRaises(ValueError):
                emit_uncorrelated_denial(event | changes, environment='synthetic', timestamp='2026-11-06T15:00:00Z', sink=out.append)
        def fail(_):
            raise OSError('unavailable')
        with self.assertRaises(OSError):
            emit_uncorrelated_denial(event, environment='synthetic', timestamp='2026-11-06T15:00:00Z', sink=fail)
        with self.assertRaises(ValueError):
            emit_uncorrelated_denial(event, environment='synthetic', timestamp='2026-11-06X15:00:00Z', sink=out.append)


if __name__ == '__main__':
    unittest.main()
