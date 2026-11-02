"""C0 merge evidence: replayed approval refused; parameters swapped after approval refused."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts" / "v0"))
from approval import ApprovalRefused, BoundAction, verify  # noqa: E402

NOW = 1_800_000_000
ACTION = BoundAction("task-1", "gmail.send", "send", "alex@example.com",
                     {"subject": "Dinner", "body": "7pm works."})


def record(action=ACTION, **over):
    r = {"schema": "potter.approval/v0", "approval_id": "appr-0001", "task_id": action.task_id,
         "action_hash": action.hash(), "decision": "approve", "decided_by": "josh",
         "channel": "telegram", "issued_at": NOW - 10, "expires_at": NOW + 300,
         "nonce": "a" * 32}
    r.update(over)
    return r


class ApprovalContract(unittest.TestCase):
    def test_valid_approval_passes_once(self):
        consumed = set()
        verify(record(), ACTION, NOW, consumed)
        self.assertIn("a" * 32, consumed)

    def test_replay_refused(self):
        consumed = set()
        verify(record(), ACTION, NOW, consumed)
        with self.assertRaisesRegex(ApprovalRefused, "replay"):
            verify(record(), ACTION, NOW + 1, consumed)

    def test_swapped_parameters_refused(self):
        swapped = BoundAction("task-1", "gmail.send", "send", "alex@example.com",
                              {"subject": "Dinner", "body": "Wire $5,000 today."})
        with self.assertRaisesRegex(ApprovalRefused, "action changed"):
            verify(record(), swapped, NOW, set())

    def test_swapped_target_refused(self):
        other = BoundAction("task-1", "gmail.send", "send", "mallory@example.com", ACTION.parameters)
        with self.assertRaises(ApprovalRefused):
            verify(record(), other, NOW, set())

    def test_refused_check_does_not_consume_nonce(self):
        consumed = set()
        with self.assertRaises(ApprovalRefused):
            verify(record(expires_at=NOW - 1), ACTION, NOW, consumed)
        self.assertEqual(consumed, set())

    def test_expired_and_future_refused(self):
        with self.assertRaises(ApprovalRefused):
            verify(record(expires_at=NOW), ACTION, NOW, set())
        with self.assertRaises(ApprovalRefused):
            verify(record(issued_at=NOW + 5, expires_at=NOW + 50), ACTION, NOW, set())

    def test_deny_and_wrong_issuer_refused(self):
        for over in ({"decision": "deny"}, {"decided_by": "dolores"}, {"channel": "email"},
                     {"task_id": "task-2"}, {"schema": "potter.approval/v1"}):
            with self.subTest(over=over), self.assertRaises(ApprovalRefused):
                verify(record(**over), ACTION, NOW, set())

    def test_malformed_refused(self):
        r = record()
        del r["nonce"]
        with self.assertRaisesRegex(ApprovalRefused, "malformed"):
            verify(r, ACTION, NOW, set())

    def test_hash_is_order_independent_and_exact(self):
        a = BoundAction("t", "c", "o", "x", {"a": 1, "b": 2})
        b = BoundAction("t", "c", "o", "x", {"b": 2, "a": 1})
        c = BoundAction("t", "c", "o", "x", {"a": 1, "b": "2"})
        self.assertEqual(a.hash(), b.hash())
        self.assertNotEqual(a.hash(), c.hash())

    def test_computer_approval_needs_screenshot(self):
        act = BoundAction("task-1", "computer", "click", "app:Finder", {"x": 1, "y": 2})
        with self.assertRaisesRegex(ApprovalRefused, "screenshot"):
            verify(record(act), act, NOW, set())
        verify(record(act, screenshot_ref="evidence://s/1"), act, NOW, set())


class SchemaFiles(unittest.TestCase):
    def test_schemas_parse_and_match_verifier(self):
        appr = json.loads((ROOT / "contracts/v0/approval_record.schema.json").read_text())
        evt = json.loads((ROOT / "contracts/v0/client_event.schema.json").read_text())
        from approval import REQUIRED, CHANNELS
        self.assertEqual(set(appr["required"]), set(REQUIRED))
        self.assertEqual(set(appr["properties"]["channel"]["enum"]), CHANNELS)
        self.assertIn("APPROVAL_REQUESTED", evt["properties"]["type"]["enum"])
        self.assertFalse(appr["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
