import subprocess
import tempfile
import unittest
from pathlib import Path

from claims import SCHEMA, attestation_bytes
from receipt import verify_experimental_receipt


class ReceiptCompositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.expected = {
            "commit": "a" * 40, "tree": "b" * 40,
            "manifest": "c" * 64, "evidence": "d" * 64,
        }
        self.keys = {}
        for role in ("author", "reviewer"):
            key = self.root / role
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
            )
            self.keys[role] = key
        self.trusted = {
            role: " ".join(self.keys[role].with_suffix(".pub").read_text().split()[:2])
            for role in self.keys
        }
        self.consumed = set()
        self.consumptions = []
        self.sequence = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _attestation(self, role, nonce):
        claims = {
            **self.expected, "schema": SCHEMA, "role": role, "verdict": "approve",
            "nonce": nonce, "expires_at": 101,
        }
        payload = attestation_bytes(claims, self.expected, 100, 60)
        self.sequence += 1
        message = self.root / (role + "-" + nonce[:4] + "-" + str(self.sequence))
        message.write_bytes(payload)
        subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(self.keys[role]),
             "-n", "potter-review", str(message)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return payload, Path(str(message) + ".sig")

    def _consume_pair(self, nonces, binding):
        if any(nonce in self.consumed for nonce in nonces):
            return False
        self.consumed.update(nonces)
        self.consumptions.append(binding)
        return True

    def _call(self, **changes):
        author_payload, author_sig = self._attestation("author", "d" * 64)
        reviewer_payload, reviewer_sig = self._attestation("reviewer", "e" * 64)
        arguments = {
            "author_payload": author_payload, "author_signature": author_sig,
            "reviewer_payload": reviewer_payload, "reviewer_signature": reviewer_sig,
            "trusted_keys": self.trusted,
            "trusted_agents": {"author": "dolores", "reviewer": "claude"},
            "expected": self.expected,
            "required_checks": {"unit", "review"},
            "check_statuses": {"unit": True, "review": True},
            "open_request_changes": False, "now": 100, "max_lifetime": 60,
            "consume_nonces": self._consume_pair,
        }
        arguments.update(changes)
        return verify_experimental_receipt(**arguments)

    def test_two_distinct_keys_and_atomic_pair_consumption(self):
        receipt = self._call()
        self.assertEqual(receipt["commit"], self.expected["commit"])
        self.assertEqual(len(self.consumed), 2)
        self.assertNotEqual(
            receipt["key_fingerprints"]["author"],
            receipt["key_fingerprints"]["reviewer"],
        )
        self.assertEqual(receipt["agents"], {"author": "dolores", "reviewer": "claude"})
        self.assertEqual(self.consumptions[0]["commit"], self.expected["commit"])
        self.assertEqual(self.consumptions[0]["tree"], self.expected["tree"])
        self.assertEqual(self.consumptions[0]["receipt_hash"], receipt["receipt_hash"])

    def test_replay_refuses_without_partial_consumption(self):
        self._call()
        before = set(self.consumed)
        with self.assertRaises(ValueError):
            self._call()
        self.assertEqual(self.consumed, before)

    def test_self_review_and_missing_authority_evidence_fail_closed(self):
        same = dict(self.trusted)
        same["reviewer"] = same["author"]
        with self.assertRaises(ValueError):
            self._call(trusted_keys=same)
        self.assertEqual(self.consumed, set())
        with self.assertRaises(ValueError):
            self._call(trusted_agents={"author": "dolores", "reviewer": "dolores"})
        with self.assertRaises(ValueError):
            self._call(check_statuses={"unit": True})
        with self.assertRaises(ValueError):
            self._call(open_request_changes=True)
        self.assertEqual(self.consumed, set())


if __name__ == "__main__":
    unittest.main()
