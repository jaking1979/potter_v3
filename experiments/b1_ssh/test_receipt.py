import hashlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from claims import SCHEMA, attestation_bytes
from receipt import verify_experimental_receipt
from nonce_store import prepare, consume_pair, pair_matches


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
            agent: " ".join(self.keys[role].with_suffix(".pub").read_text().split()[:2])
            for role, agent in (("author", "dolores"), ("reviewer", "claude"))
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
            "agent_keys": self.trusted,
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

    def test_consumer_mutation_cannot_change_returned_agent_binding(self):
        agents = {"author": "dolores", "reviewer": "claude"}

        def mutate_after_checks(nonces, binding):
            agents["reviewer"] = "dolores"
            return self._consume_pair(nonces, binding)

        receipt = self._call(trusted_agents=agents, consume_nonces=mutate_after_checks)
        self.assertEqual(agents["reviewer"], "dolores")
        self.assertEqual(receipt["agents"], {"author": "dolores", "reviewer": "claude"})
        self.assertEqual(len(self.consumed), 2)

    def test_replay_refuses_without_partial_consumption(self):
        self._call()
        before = set(self.consumed)
        with self.assertRaises(ValueError):
            self._call()
        self.assertEqual(self.consumed, before)

    def test_scratch_sqlite_composition_and_reopened_replay(self):
        database = self.root / 'scratch.db'
        nonces = ('d' * 64, 'e' * 64)
        with sqlite3.connect(database) as db:
            prepare(db)
            receipt = self._call(consume_nonces=lambda pair, binding: consume_pair(db, pair, binding))
            binding = {'commit': receipt['commit'], 'tree': receipt['tree'],
                       'receipt_hash': receipt['receipt_hash']}
            self.assertTrue(pair_matches(db, nonces, binding))
        with sqlite3.connect(database) as reopened:
            self.assertTrue(pair_matches(reopened, nonces, binding))
            with self.assertRaises(ValueError):
                self._call(consume_nonces=lambda pair, facts: consume_pair(reopened, pair, facts))
            self.assertTrue(pair_matches(reopened, nonces, binding))

    def test_scratch_commit_then_lost_ack_can_be_identified_not_authorized(self):
        database = self.root / 'scratch-lost-ack.db'
        author_payload, author_sig = self._attestation('author', 'd' * 64)
        reviewer_payload, reviewer_sig = self._attestation('reviewer', 'e' * 64)
        expected_hash = hashlib.sha256(
            b'potter.receipt/v0\0' + author_payload + b'\0' + reviewer_payload
        ).hexdigest()
        expected_binding = {'commit': self.expected['commit'], 'tree': self.expected['tree'],
                            'receipt_hash': expected_hash}
        with sqlite3.connect(database) as db:
            prepare(db)
            def commit_then_fail(pair, binding):
                self.assertTrue(consume_pair(db, pair, binding))
                raise OSError('synthetic lost acknowledgment')
            with self.assertRaises(OSError):
                self._call(author_payload=author_payload, author_signature=author_sig,
                           reviewer_payload=reviewer_payload, reviewer_signature=reviewer_sig,
                           consume_nonces=commit_then_fail)
        nonces = ('d' * 64, 'e' * 64)
        with sqlite3.connect(database) as reopened:
            self.assertTrue(pair_matches(reopened, nonces, expected_binding))
            mismatched = {**expected_binding, 'receipt_hash': 'f' * 64}
            self.assertFalse(pair_matches(reopened, nonces, mismatched))
            with self.assertRaises(ValueError):
                self._call(consume_nonces=lambda pair, facts: consume_pair(reopened, pair, facts))

    def test_self_review_and_missing_authority_evidence_fail_closed(self):
        same = dict(self.trusted)
        same["claude"] = same["dolores"]
        with self.assertRaises(ValueError):
            self._call(agent_keys=same)
        self.assertEqual(self.consumed, set())
        with self.assertRaises(ValueError):
            self._call(trusted_agents={"author": "dolores", "reviewer": "dolores"})
        with self.assertRaises(ValueError):
            self._call(check_statuses={"unit": True})
        with self.assertRaises(ValueError):
            self._call(open_request_changes=True)
        self.assertEqual(self.consumed, set())

    def test_borrowed_agent_name_with_wrong_key_fails(self):
        # Sign the reviewer claim with another key while the pre-candidate
        # registry still pins Claude's original key.
        attacker = self.root / 'second-dolores-key'
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(attacker)], check=True)
        self.keys['reviewer'] = attacker
        with self.assertRaises(ValueError):
            self._call()
        self.assertEqual(self.consumed, set())

    def test_expiry_and_candidate_mismatches_fail_without_consumption(self):
        bad_commit = {**self.expected, 'commit': '1' * 40}
        bad_tree = {**self.expected, 'tree': '2' * 40}
        for changes in ({'now': 101}, {'expected': bad_commit}, {'expected': bad_tree}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self._call(**changes)
                self.assertEqual(self.consumed, set())

    def test_swapped_roles_and_nonapprove_verdict_refused(self):
        reviewer_payload, reviewer_sig = self._attestation('reviewer', 'e' * 64)
        with self.assertRaises(ValueError):
            self._call(author_payload=reviewer_payload, author_signature=reviewer_sig)
        claims = {**self.expected, 'schema': SCHEMA, 'role': 'author',
                  'verdict': 'request_changes', 'nonce': 'd' * 64, 'expires_at': 101}
        payload = attestation_bytes(claims, self.expected, 100, 60)
        with self.assertRaises(ValueError):
            self._call(author_payload=payload)
        self.assertEqual(self.consumed, set())


if __name__ == "__main__":
    unittest.main()
