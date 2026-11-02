"""C0 merge evidence: replay refused; parameters swapped after approval refused;
unauthenticated/forged records refused; atomic single use under concurrency."""
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts" / "v0"))
from approval import (ApprovalRefused, BoundAction, HmacAuthenticator,  # noqa: E402
                      MemoryApprovalLedger, signing_payload, verify, MAX_VALIDITY_S)

NOW = 1_800_000_000
AUTH = HmacAuthenticator({"e1-2026": b"test-only-issuer-key"})
ACTION = BoundAction("task-1", "gmail.send", "send", "alex@example.com",
                     {"subject": "Dinner", "body": "7pm works."})


def record(action=ACTION, sign=True, **over):
    r = {"schema": "potter.approval/v0", "approval_id": "appr-0001", "task_id": action.task_id,
         "action_hash": action.hash(), "decision": "approve", "decided_by": "josh",
         "channel": "telegram", "issued_at": NOW - 10, "expires_at": NOW + 300,
         "nonce": "a" * 32, "issuer_key_id": "e1-2026", "issuer_signature": "x"}
    r.update({k: v for k, v in over.items() if k != "issuer_signature"})
    if sign:
        r["issuer_signature"] = AUTH.sign("e1-2026", signing_payload(r))
    if "issuer_signature" in over:
        r["issuer_signature"] = over["issuer_signature"]
    return r


def check(r, action=ACTION, store=None, auth=AUTH, now=NOW, effect_id="eff-1"):
    return verify(r, action, now, store if store is not None else MemoryApprovalLedger(), auth, effect_id)


class ApprovalContract(unittest.TestCase):
    def test_valid_approval_passes_once(self):
        store = MemoryApprovalLedger()
        check(record(), store=store)
        self.assertIn("a" * 32, store)

    def test_replay_refused(self):
        store = MemoryApprovalLedger()
        check(record(), store=store)
        with self.assertRaisesRegex(ApprovalRefused, "replay"):
            check(record(), store=store, now=NOW + 1)

    def test_swapped_parameters_refused(self):
        swapped = BoundAction("task-1", "gmail.send", "send", "alex@example.com",
                              {"subject": "Dinner", "body": "Wire $5,000 today."})
        with self.assertRaisesRegex(ApprovalRefused, "action changed"):
            check(record(), action=swapped)

    def test_swapped_target_refused(self):
        other = BoundAction("task-1", "gmail.send", "send", "mallory@example.com", ACTION.parameters)
        with self.assertRaises(ApprovalRefused):
            check(record(), action=other)

    def test_refused_check_does_not_consume_nonce(self):
        store = MemoryApprovalLedger()
        with self.assertRaises(ApprovalRefused):
            check(record(expires_at=NOW - 1), store=store)
        self.assertNotIn("a" * 32, store)

    def test_expired_and_future_refused(self):
        with self.assertRaises(ApprovalRefused):
            check(record(expires_at=NOW))
        with self.assertRaises(ApprovalRefused):
            check(record(issued_at=NOW + 5, expires_at=NOW + 50))

    def test_deny_and_wrong_claims_refused(self):
        for over in ({"decision": "deny"}, {"decided_by": "dolores"}, {"channel": "email"},
                     {"task_id": "task-2"}, {"schema": "potter.approval/v1"}):
            with self.subTest(over=over), self.assertRaises(ApprovalRefused):
                check(record(**over))

    def test_malformed_refused(self):
        r = record()
        del r["nonce"]
        with self.assertRaisesRegex(ApprovalRefused, "malformed"):
            check(r)

    def test_schema_violations_refused_without_consuming(self):
        bad = [{"nonce": None}, {"nonce": "short"}, {"approval_id": ""}, {"approval_id": None},
               {"task_id": ""}, {"action_hash": "ABC"}, {"issued_at": True},
               {"expires_at": float(NOW + 300)}, {"screenshot_ref": True}, {"screenshot_ref": ""},
               {"extra_field": 1}, {"issuer_key_id": ""}]
        for over in bad:
            store = MemoryApprovalLedger()
            with self.subTest(over=over), self.assertRaisesRegex(ApprovalRefused, "malformed"):
                check(record(**over), store=store)
            self.assertNotIn("a" * 32, store)
        with self.assertRaisesRegex(ApprovalRefused, "malformed"):
            check(["not", "a", "dict"])

    def test_hash_is_order_independent_and_exact(self):
        a = BoundAction("t", "c", "o", "x", {"a": 1, "b": 2})
        b = BoundAction("t", "c", "o", "x", {"b": 2, "a": 1})
        c = BoundAction("t", "c", "o", "x", {"a": 1, "b": "2"})
        self.assertEqual(a.hash(), b.hash())
        self.assertNotEqual(a.hash(), c.hash())

    def test_computer_approval_needs_screenshot(self):
        act = BoundAction("task-1", "computer", "click", "app:Finder", {"x": 1, "y": 2})
        with self.assertRaisesRegex(ApprovalRefused, "screenshot"):
            check(record(act), action=act)
        check(record(act, screenshot_ref="evidence://s/1"), action=act)


class Validity24h(unittest.TestCase):
    """T37 S1: an approval after 24 h is refused, even if signed with a longer expiry."""

    def test_signed_oversized_expiry_capped_at_24h(self):
        r = record(issued_at=NOW, expires_at=NOW + 7 * 24 * 3600)
        check(r, now=NOW + MAX_VALIDITY_S - 1)
        with self.assertRaisesRegex(ApprovalRefused, "24 h"):
            check(r, now=NOW + MAX_VALIDITY_S)

    def test_exact_boundary(self):
        r = record(issued_at=NOW, expires_at=NOW + MAX_VALIDITY_S)
        check(r, now=NOW + MAX_VALIDITY_S - 1)
        with self.assertRaises(ApprovalRefused):
            check(r, now=NOW + MAX_VALIDITY_S)

    def test_narrower_policy_ttl_preserved(self):
        r = record(issued_at=NOW, expires_at=NOW + 300)
        with self.assertRaises(ApprovalRefused):
            check(r, now=NOW + 300)


class StaleState(unittest.TestCase):
    """T37 S1: a stale-state approval is refused."""

    def test_precondition_change_voids_approval(self):
        old = BoundAction("task-1", "calendar.update", "move", "event:42", {"to": "15:00"}, {"etag": "v7"})
        new = BoundAction("task-1", "calendar.update", "move", "event:42", {"to": "15:00"}, {"etag": "v8"})
        with self.assertRaisesRegex(ApprovalRefused, "action changed"):
            check(record(old), action=new)


class CrashSafety(unittest.TestCase):
    """Dolores review: consumption must not strand an approval on crash."""

    def test_consume_records_intent_atomically_for_recovery(self):
        ledger = MemoryApprovalLedger()
        intent = check(record(), store=ledger, effect_id="eff-77")
        # Simulated crash after consume, before the effect: recovery finds the bound intent.
        recovered = ledger.pending("a" * 32)
        self.assertEqual(recovered, intent)
        self.assertEqual(recovered.action_hash, ACTION.hash())
        with self.assertRaisesRegex(ApprovalRefused, "replay"):
            check(record(), store=ledger, effect_id="eff-78")
        self.assertEqual(ledger.pending("a" * 32).effect_id, "eff-77")

    def test_refusal_records_nothing(self):
        ledger = MemoryApprovalLedger()
        with self.assertRaises(ApprovalRefused):
            check(record(), action=BoundAction("task-1", "x", "y", "z"), store=ledger)
        self.assertIsNone(ledger.pending("a" * 32))

    def test_missing_effect_id_refused(self):
        with self.assertRaisesRegex(ApprovalRefused, "effect id"):
            check(record(), effect_id="")


class Provenance(unittest.TestCase):
    """Dolores review (2): self-asserted fields are not authority."""

    def test_constructed_record_without_issuer_signature_refused(self):
        forged = record(sign=False, issuer_signature="0" * 64)
        with self.assertRaisesRegex(ApprovalRefused, "unauthenticated"):
            check(forged)

    def test_field_edited_after_signing_refused(self):
        r = record()
        r["expires_at"] = NOW + 10_000
        with self.assertRaisesRegex(ApprovalRefused, "unauthenticated"):
            check(r)

    def test_unknown_issuer_key_refused(self):
        r = record()
        r["issuer_key_id"] = "attacker"
        with self.assertRaisesRegex(ApprovalRefused, "unauthenticated"):
            check(r)

    def test_signature_from_other_key_refused(self):
        other = HmacAuthenticator({"e1-2026": b"different-key"})
        r = record(sign=False)
        r["issuer_signature"] = other.sign("e1-2026", signing_payload(r))
        with self.assertRaisesRegex(ApprovalRefused, "unauthenticated"):
            check(r)

    def test_no_authenticator_fails_closed(self):
        with self.assertRaisesRegex(ApprovalRefused, "no trusted issuer"):
            check(record(), auth=None)


class Concurrency(unittest.TestCase):
    """Dolores review (3): single use must be atomic across concurrent consumers."""

    def test_exactly_one_of_many_concurrent_consumers_succeeds(self):
        store, r = MemoryApprovalLedger(), record()
        start, results, lock = threading.Barrier(32), [], threading.Lock()

        def worker():
            start.wait()
            try:
                check(dict(r), store=store)
                ok = True
            except ApprovalRefused:
                ok = False
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(results), 32)


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
