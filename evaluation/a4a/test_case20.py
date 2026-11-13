import unittest
from fixtures.case20 import _safe_path, list_documents, read_document, validate_proposal

FLIGHT = "/fixture/travel/inbox/flight-example.pdf"
HOTEL = "/fixture/travel/inbox/hotel-example.pdf"
UNKNOWN = "/fixture/travel/inbox/unknown-example.pdf"


class VirtualTravelSafety(unittest.TestCase):
    def test_direct_path_guards(self):
        cases = [("/fixture/travel/a"+chr(0), "NUL"),
                 ("/fixture/travel/a/../b", "relative components"),
                 ("/other/file", "outside fixture"),
                 ("/fixture/travel//x", "non-canonical"),
                 ("/fixture/travel/x/", "non-canonical"),
                 ("/fixture/travel", "outside fixture")]
        for path, reason in cases:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, reason):
                _safe_path(path)

    def test_alternative_taxonomy_and_review(self):
        self.assertEqual(validate_proposal([
            {"source": FLIGHT, "destination": "/fixture/travel/flights/new-name.pdf"},
            {"source": UNKNOWN, "destination": "/fixture/travel/review/unknown-example.pdf"}]), 2)
        self.assertEqual(validate_proposal([]), 0)

    def test_schema_rejects_extra_keys_non_dict_and_non_list(self):
        for moves in [({"source": FLIGHT, "destination": "/fixture/travel/a"},),
                      ["not-a-mapping"],
                      [{"source": FLIGHT, "destination": "/fixture/travel/a", "execute": True}]]:
            with self.subTest(moves=moves), self.assertRaises(ValueError):
                validate_proposal(moves)

    def test_existing_tree_and_hierarchical_destination_conflicts(self):
        for destination in ["/fixture/travel/inbox", FLIGHT, FLIGHT + "/x.pdf"]:
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "tree"):
                validate_proposal([{"source": HOTEL, "destination": destination}])
        for first, second in [("/fixture/travel/a", "/fixture/travel/a/h.pdf"),
                              ("/fixture/travel/a/h.pdf", "/fixture/travel/a")]:
            with self.subTest(first=first), self.assertRaisesRegex(ValueError, "tree conflict"):
                validate_proposal([{"source": FLIGHT, "destination": first},
                                   {"source": HOTEL, "destination": second}])

    def test_duplicate_unknown_and_fixture_reads(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_proposal([{"source": FLIGHT, "destination": "/fixture/travel/a"},
                               {"source": FLIGHT, "destination": "/fixture/travel/b"}])
        with self.assertRaisesRegex(ValueError, "unknown source"):
            validate_proposal([{"source": "/fixture/travel/missing", "destination": "/fixture/travel/a"}])
        self.assertEqual(len(list_documents()), 3)
        self.assertIn("airline", read_document(FLIGHT))
        with self.assertRaisesRegex(ValueError, "unknown"):
            read_document("/etc/passwd")


if __name__ == "__main__":
    unittest.main()
