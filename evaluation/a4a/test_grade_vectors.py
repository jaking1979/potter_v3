import json
import unittest
from pathlib import Path
from blind_grading import validate_grades

class GradeVectors(unittest.TestCase):
    def test_shared_vectors(self):
        for case in json.loads((Path(__file__).parent / 'grade_vectors.json').read_text()):
            with self.subTest(case=case['name']):
                if case['accept']:
                    self.assertTrue(validate_grades(case['record']))
                else:
                    with self.assertRaises(ValueError): validate_grades(case['record'])

if __name__ == '__main__': unittest.main()
