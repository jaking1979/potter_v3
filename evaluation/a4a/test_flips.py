import copy
import unittest
from flips import two_step_flips


def sample(responsibility=False):
    values = {'done': 'yes', 'trust': 'yes', 'speed': 'yes', 'reexplaining': 'none'}
    if responsibility:
        values['follow_through'] = 'yes'
    return {'case_id': 'case-1', 'responsibility': responsibility,
            'outputs': {'A': values.copy(), 'B': values.copy()}}


class FlipsTest(unittest.TestCase):
    def test_opposite_and_adjacent_categories(self):
        first = sample(True)
        second = copy.deepcopy(first)
        second['outputs']['A']['done'] = 'no'
        second['outputs']['A']['trust'] = 'somewhat'
        second['outputs']['B']['follow_through'] = 'no'
        self.assertEqual(two_step_flips(first, second), [
            {'case_id': 'case-1', 'output': 'A', 'dimension': 'done', 'first': 'yes', 'second': 'no'},
            {'case_id': 'case-1', 'output': 'B', 'dimension': 'follow_through', 'first': 'yes', 'second': 'no'},
        ])

    def test_reverse_and_mismatch_fail_closed(self):
        first = sample()
        first['outputs']['B']['trust'] = 'no'
        second = sample()
        self.assertEqual(two_step_flips(first, second)[0]['first'], 'no')
        second['case_id'] = 'different'
        with self.assertRaises(ValueError):
            two_step_flips(first, second)
        with self.assertRaises(ValueError):
            two_step_flips(first, sample(True))
