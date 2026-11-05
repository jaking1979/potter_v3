import copy
import unittest
from realign import realign_pair


def grade(case='case-1', responsibility=False):
    first = {'done': 'yes', 'reexplaining': 'none', 'speed': 'yes', 'trust': 'yes'}
    second = {'done': 'no', 'reexplaining': 'some', 'speed': 'slow', 'trust': 'no'}
    if responsibility:
        first['follow_through'] = 'yes'
        second['follow_through'] = 'no'
    return {'case_id': case, 'responsibility': responsibility,
            'outputs': {'A': first, 'B': second}}


class RealignmentTests(unittest.TestCase):
    def test_swapped_labels_realign_without_mutating_or_leaking_mapping(self):
        first = grade(responsibility=True)
        second = copy.deepcopy(first)
        second['outputs']['A'], second['outputs']['B'] = second['outputs']['B'], second['outputs']['A']
        original = copy.deepcopy(second)
        a, b = realign_pair(first, second, {'A': '1'*64, 'B': '2'*64},
                            {'A': '2'*64, 'B': '1'*64})
        self.assertEqual(a, b)
        self.assertEqual(second, original)
        b['outputs']['A']['done'] = 'partly'
        self.assertEqual(first['outputs']['A']['done'], 'yes')
        self.assertEqual(second, original)

    def test_refuses_different_or_ambiguous_pairs(self):
        good = {'A': '1'*64, 'B': '2'*64}
        for other in ({'A': '1'*64, 'B': '3'*64}, {'A': '1'*64, 'B': '1'*64},
                      {'A': '1'*64}, {'A': 'x'*64, 'B': '2'*64}):
            with self.subTest(other=other), self.assertRaises(ValueError):
                realign_pair(grade(), grade(), good, other)

    def test_refuses_case_responsibility_and_grade_mismatch(self):
        mapping = {'A': '1'*64, 'B': '2'*64}
        invalid = grade()
        invalid['outputs']['A']['trust'] = 'forged'
        for other in (grade(case='different'), grade(responsibility=True), invalid):
            with self.subTest(other=other), self.assertRaises(ValueError):
                realign_pair(grade(), other, mapping, mapping)


if __name__ == '__main__':
    unittest.main()
