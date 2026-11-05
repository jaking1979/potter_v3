import copy
import hashlib
import unittest
from realign import realign_pair, realign_verified_bytes, realign_occurrences, prepare_synthetic_occurrences
from report import synthetic_report


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

    def test_alignment_feeds_report_without_creating_false_disagreement(self):
        first = grade()
        second = copy.deepcopy(first)
        second['outputs']['A'], second['outputs']['B'] = second['outputs']['B'], second['outputs']['A']
        plan = {'case-1': False}
        unaligned = synthetic_report([first], [second], plan)
        self.assertEqual(unaligned['agreement']['done']['agreements'], 0)
        a, b = realign_pair(first, second, {'A': '1'*64, 'B': '2'*64},
                            {'A': '2'*64, 'B': '1'*64})
        aligned = synthetic_report([a], [b], plan)
        self.assertEqual(aligned['agreement']['done']['agreements'], 1)
        self.assertEqual(aligned['observed_flips'], [])

    def test_byte_consistency_and_changed_snapshot(self):
        a, b = b'first output', b'second output'
        ha, hb = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
        first = grade()
        second = copy.deepcopy(first)
        second['outputs']['A'], second['outputs']['B'] = second['outputs']['B'], second['outputs']['A']
        args = (first, second, {'A': ha, 'B': hb}, {'A': hb, 'B': ha})
        aligned = realign_verified_bytes(*args, {ha: a, hb: b})
        self.assertEqual(aligned[0], aligned[1])
        for bad in ({ha: a, hb: b'changed'}, {ha: a}, {ha: a, hb: b'x' * 1_048_577}):
            with self.subTest(bad_count=len(bad)), self.assertRaises(ValueError):
                realign_verified_bytes(*args, bad)

    def test_identical_bytes_with_distinct_occurrences_and_swapped_labels(self):
        data = b'identical answer'
        digest = hashlib.sha256(data).hexdigest()
        first_map = {'A': {'id': '1'*64, 'sha256': digest}, 'B': {'id': '2'*64, 'sha256': digest}}
        second_map = {'A': first_map['B'].copy(), 'B': first_map['A'].copy()}
        first = grade()
        second = copy.deepcopy(first)
        second['outputs']['A'], second['outputs']['B'] = second['outputs']['B'], second['outputs']['A']
        a, b = realign_occurrences(first, second, first_map, second_map,
                                   {'1'*64: data, '2'*64: data})
        self.assertEqual(a, b)
        for bad in ({'A': first_map['A'], 'B': first_map['A']},
                    {'A': {'id': '1'*64, 'sha256': '3'*64}, 'B': first_map['B']}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                realign_occurrences(first, second, first_map, bad, {'1'*64: data, '2'*64: data})
        with self.assertRaises(ValueError):
            realign_occurrences(first, second, first_map, second_map, {'1'*64: data, '2'*64: b'tampered'})

    def test_prepared_identical_occurrences_remain_distinct(self):
        snapshots, first, second = prepare_synthetic_occurrences(b'same', b'same')
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(first[0], {'A': b'same', 'B': b'same'})
        for visible, hidden in (first, second):
            self.assertEqual(set(hidden), {'A', 'B'})
            self.assertNotEqual(hidden['A']['id'], hidden['B']['id'])
            for label in ('A', 'B'):
                self.assertEqual(visible[label], snapshots[hidden[label]['id']])
        aligned = realign_occurrences(grade(), grade(), first[1], second[1], snapshots)
        self.assertEqual(aligned[0]['case_id'], aligned[1]['case_id'])
        with self.assertRaises(ValueError): prepare_synthetic_occurrences('not bytes', b'same')


if __name__ == '__main__':
    unittest.main()
