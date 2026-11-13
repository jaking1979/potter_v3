import unittest
from fixtures.case20 import documents, validate_proposal


class Case20(unittest.TestCase):
    def test_valid_proposal_is_only_data(self):
        before = documents()
        moves = [
            {"source": "/fixture/travel/inbox/flight-example.pdf", "destination": "/fixture/travel/transport/flight-example.pdf"},
            {"source": "/fixture/travel/inbox/hotel-example.pdf", "destination": "/fixture/travel/lodging/hotel-example.pdf"},
        ]
        self.assertEqual(validate_proposal(moves), 2)
        self.assertEqual(documents(), before)

    def test_ambiguous_source_is_not_classified(self):
        with self.assertRaises(ValueError):
            validate_proposal([{"source": "/fixture/travel/inbox/unknown-example.pdf", "destination": "/fixture/travel/lodging/unknown-example.pdf"}])

    def test_path_escape_and_duplicate_refused(self):
        good = {"source": "/fixture/travel/inbox/flight-example.pdf", "destination": "/fixture/travel/transport/flight-example.pdf"}
        bad = [
            [dict(good, destination="/fixture/travel/transport/../../escape.pdf")],
            [good, good],
            [dict(good, destination="/outside/flight-example.pdf")],
            [dict(good, destination="/fixture/travel/lodging/flight-example.pdf")],
            [dict(good, destination="/fixture/travel/transport/other.pdf")],
        ]
        for scenario in bad:
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                validate_proposal(scenario)

    def test_unlisted_or_extra_tool_fields_are_refused(self):
        with self.assertRaises(ValueError):
            validate_proposal([{"source": "/fixture/travel/inbox/flight-example.pdf", "destination": "/fixture/travel/transport/flight-example.pdf", "execute": True}])


if __name__ == "__main__":
    unittest.main()
