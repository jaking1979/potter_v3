import copy
import unittest
from blind_grading import validate_grades


def valid():
    answer = {'done': 'yes', 'reexplaining': 'some', 'speed': 'slow', 'trust': 'yes'}
    return {'case_id': 'synthetic-1', 'responsibility': False,
            'outputs': {'A': dict(answer), 'B': dict(answer)}}


class GradingTest(unittest.TestCase):
    def test_valid_pair(self):
        self.assertTrue(validate_grades(valid()))

    def test_missing_answer_or_label_rejected(self):
        for mutation in ('field', 'label'):
            data = valid()
            if mutation == 'field': del data['outputs']['A']['done']
            else: del data['outputs']['B']
            with self.assertRaises(ValueError): validate_grades(data)

    def test_mapping_cannot_leak_into_grade(self):
        data = valid(); data['outputs']['A']['model'] = 'example'
        with self.assertRaises(ValueError): validate_grades(data)

    def test_responsibility_requires_followthrough_for_both(self):
        data = valid(); data['responsibility'] = True
        data['outputs']['A']['follow_through'] = 'yes'
        with self.assertRaises(ValueError): validate_grades(data)
        data['outputs']['B']['follow_through'] = 'partly'
        self.assertTrue(validate_grades(data))

    def test_unexpected_values_rejected(self):
        for value in (None, [], 'unknown'):
            data = valid(); data['outputs']['B']['speed'] = value
            with self.assertRaises(ValueError): validate_grades(data)


if __name__ == '__main__': unittest.main()
