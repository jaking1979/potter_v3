import unittest
from fixtures.case20 import _safe_path, documents, validate_proposal

FLIGHT = '/fixture/travel/inbox/flight-example.pdf'
UNKNOWN = '/fixture/travel/inbox/unknown-example.pdf'


class VirtualTravelSafety(unittest.TestCase):
    def check(self, path, message):
        with self.assertRaisesRegex(ValueError, message):
            _safe_path(path)

    def test_paths_directly_reject_each_unsafe_class(self):
        self.check('/fixture/travel/a' + chr(0) + '.pdf', 'NUL')
        self.check('/fixture/travel/a/../b', 'relative components')
        self.check('/other/file.pdf', 'outside fixture')
        self.check('/fixture/travel//a.pdf', 'non-canonical')
        self.check('/fixture/travel/a.pdf/', 'non-canonical')
        self.check('/fixture/travel', 'outside fixture')

    def test_valid_proposals_allow_alternative_taxonomy_and_review(self):
        moves = [{'source': FLIGHT, 'destination': '/fixture/travel/flights/new-name.pdf'},
                 {'source': UNKNOWN, 'destination': '/fixture/travel/review/unknown-example.pdf'}]
        self.assertEqual(validate_proposal(moves), 2)

    def test_empty_proposal(self):
        self.assertEqual(validate_proposal([]), 0)

    def test_collision_and_unknown_sources(self):
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            validate_proposal([{'source': FLIGHT, 'destination': UNKNOWN}])
        with self.assertRaisesRegex(ValueError, 'unknown source'):
            validate_proposal([{'source': '/fixture/travel/unknown', 'destination': '/fixture/travel/other'}])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_proposal([{'source': FLIGHT, 'destination': '/fixture/travel/a'},
                               {'source': FLIGHT, 'destination': '/fixture/travel/b'}])

    def test_fixture_has_no_live_inputs(self):
        self.assertEqual(len(documents()), 3)


if __name__ == '__main__':
    unittest.main()
