import copy
import json
from pathlib import Path
import unittest

from ancestry import InventoryError, compare, inventory


def sha(n):
    return f"{n:040x}"


def node(n, *parents):
    return {"sha": sha(n), "tree": sha(n + 100), "parents": [sha(p) for p in parents]}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [node(1), node(2, 1), node(3, 1), node(4, 2, 3)]

    def test_merge_side_branch_is_inventoried(self):
        result = inventory(sha(4), self.rows)
        self.assertEqual(result["commit_count"], 4)
        self.assertEqual(result["first_parent"], [sha(4), sha(2), sha(1)])
        self.assertIn(node(3, 1), result["commits"])
        self.assertEqual(result["authority"], "observation-only")

    def test_reordering_records_preserves_digest(self):
        self.assertEqual(inventory(sha(4), self.rows), inventory(sha(4), self.rows[::-1]))

    def test_omitted_parent_and_extra_commit_refused(self):
        for rows in (self.rows[1:], self.rows + [node(5)]):
            with self.subTest(rows=rows), self.assertRaises(InventoryError):
                inventory(sha(4), rows)

    def test_cycles_and_duplicate_parents_refused(self):
        for rows in ([node(1, 2), node(2, 1)], [node(1), node(2, 1, 1)]):
            with self.subTest(rows=rows), self.assertRaises(InventoryError):
                inventory(sha(2), rows)

    def test_abbreviated_sha_duplicate_and_extra_fields_refused(self):
        bad = copy.deepcopy(self.rows)
        bad[0]["sha"] = "abc1234"
        extra = copy.deepcopy(self.rows)
        extra[0]["signature"] = "not authority"
        for rows in (bad, self.rows + [node(1)], extra):
            with self.subTest(rows=rows), self.assertRaises(InventoryError):
                inventory(sha(4), rows)

    def test_added_missing_and_tree_drift_reported(self):
        old = inventory(sha(4), self.rows)
        new = inventory(sha(5), self.rows + [node(5, 4)])
        self.assertEqual(compare(old, new)["added"], [sha(5)])
        self.assertEqual(compare(new, old)["missing"], [sha(5)])
        changed = copy.deepcopy(old)
        changed["commits"][0]["tree"] = sha(999)
        self.assertEqual(compare(old, changed)["changed"], [sha(1)])

    def test_observed_main_is_complete_71_commit_graph(self):
        observed = json.loads((Path(__file__).parents[1] / "observed-main.json").read_text())
        result = inventory(observed["observed_tip"], observed["commits"])
        self.assertEqual(result["commit_count"], 71)
        self.assertEqual(len(result["first_parent"]), 5)
        self.assertEqual(result["first_parent"][-1], "457982753dc38c5fe20cca16830ceffbb828c5a2")

    def test_long_history_without_recursive_stack(self):
        rows = [node(1)] + [node(n, n-1) for n in range(2, 2001)]
        self.assertEqual(inventory(sha(2000), rows)["commit_count"], 2000)


if __name__ == "__main__":
    unittest.main()
