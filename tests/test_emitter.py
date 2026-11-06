import json
import unittest
from tracing.emitter import emit
from tracing.trace import DecisionTrace

class EmitterTest(unittest.TestCase):
    def make(self, component):
        return DecisionTrace(task_id='12345678-1234-4234-8234-123456789abc', trace_id='abcdefab-cdef-4def-8def-abcdefabcdef', component=component, outcome='unknown', decision_digest='a'*64, environment='synthetic', timestamp='2026-11-06T15:00:00Z')

    def test_decisions_are_emitted_with_shared_task_and_distinct_component(self):
        records = []
        for component in ('policy', 'task', 'effect'):
            emit(self.make(component), records.append)
        parsed = [json.loads(x) for x in records]
        self.assertEqual([x['potter.component'] for x in parsed], ['policy', 'task', 'effect'])
        self.assertEqual({x['task.id'] for x in parsed}, {'12345678-1234-4234-8234-123456789abc'})
        self.assertTrue(all(x['potter.environment'] == 'synthetic' for x in parsed))

    def test_sink_failure_is_not_silenced(self):
        def broken(_):
            raise OSError('sink unavailable')
        with self.assertRaises(OSError):
            emit(self.make('policy'), broken)

    def test_unvalidated_record_never_reaches_sink(self):
        records = []
        with self.assertRaises(TypeError):
            emit({'task_id': 'raw text'}, records.append)
        self.assertEqual(records, [])

if __name__ == '__main__': unittest.main()
