import json
import unittest
from tracing.trace import DecisionTrace

BASE = dict(task_id='12345678-1234-4234-8234-123456789abc', trace_id='abcdefab-cdef-4def-8def-abcdefabcdef', component='policy', outcome='deny', decision_digest='a'*64, environment='synthetic', timestamp='2026-11-06T15:00:00Z')

class TraceTest(unittest.TestCase):
    def test_each_component_linked_and_single_jsonl_record(self):
        for component in ('policy', 'task', 'effect'):
            record = DecisionTrace(**(BASE | {'component': component})).jsonl()
            self.assertEqual(record.count('\n'), 1)
            parsed = json.loads(record)
            self.assertEqual(parsed['task.id'], BASE['task_id'])
            self.assertEqual(parsed['potter.component'], component)
            self.assertEqual(parsed['potter.environment'], 'synthetic')
    def test_rejects_invalid_or_private_inputs(self):
        for changes in ({'task_id':'patient name'}, {'trace_id':'abc'}, {'decision_digest':'secret'}, {'component':'raw prompt'}, {'environment':'live-ish'}, {'timestamp':'2026-11-06T15:00:00'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                DecisionTrace(**(BASE | changes))
    def test_immutable(self):
        record = DecisionTrace(**BASE)
        with self.assertRaises(AttributeError):
            record.task_id = 'changed'

    def test_subject_ids_are_bounded_and_linkable(self):
        for component, subject in [('policy', 'dec-' + 'a' * 32),
                                   ('task', BASE['task_id']),
                                   ('effect', '87654321-4321-4321-8321-cba987654321')]:
            parsed = json.loads(DecisionTrace(**(BASE | {'component': component, 'subject_id': subject})).jsonl())
            self.assertEqual(parsed['potter.subject.id'], subject)
        self.assertNotIn('potter.subject.id', json.loads(DecisionTrace(**BASE).jsonl()))

    def test_rejects_free_text_and_wrong_subject_kinds(self):
        for component, subject in [('policy', BASE['task_id']), ('effect', 'dec-' + 'a' * 32),
                                   ('task', 'private content'), ('policy', 'dec-' + 'A' * 32),
                                   ('effect', True)]:
            with self.subTest(component=component, subject=subject), self.assertRaises(ValueError):
                DecisionTrace(**(BASE | {'component': component, 'subject_id': subject}))

if __name__ == '__main__': unittest.main()
