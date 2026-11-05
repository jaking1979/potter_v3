import unittest
from archive_pair import verify_synthetic_archive
from test_verify_pair import VerificationTests
from verify_pair import _key


class ArchiveTests(unittest.TestCase):
    setUp = VerificationTests.setUp
    payload = VerificationTests.payload
    sign = VerificationTests.sign
    verify = VerificationTests.verify

    def archive(self, issued, **changes):
        args = dict(issued_facts=issued, issued_at=100, observed_now=500,
                    expected=self.expected, agents=self.kw['agents'],
                    historical_agent_keys=self.keys, historical_revocations=set(),
                    required_checks={'tests'}, historical_check_statuses={'tests': True},
                    historical_open_requests=False, max_lifetime=60)
        args.update(changes)
        return verify_synthetic_archive((self.payloads[0], self.sigs[0], self.payloads[1], self.sigs[1]), **args)

    def test_archived_consistency_after_expiry_and_later_revocation(self):
        issued = self.verify()
        with self.assertRaises(ValueError):
            self.verify(now=500, revoked_fingerprints={_key(self.keys['claude'])})
        self.assertEqual(self.archive(issued), issued)

    def test_refuse_forged_issuance_and_invalid_historical_inputs(self):
        issued = self.verify()
        changes = (dict(issued_at=150), dict(issued_at=501),
                   dict(issued_facts=dict(issued, commit='9' * 40)),
                   dict(historical_revocations={_key(self.keys['claude'])}),
                   dict(historical_open_requests=True))
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.archive(issued, **change)

    def test_verifier_version_is_audit_only_but_binding_is_required(self):
        issued = self.verify()
        old_version = dict(issued, verifier_version='older-verifier')
        self.assertEqual(self.archive(old_version), issued)
        for forged in (dict(issued, verifier_version=''), dict(issued, extra='x'),
                       dict(issued, receipt_hash='0' * 64),
                       dict(issued, agents={'author': 'claude', 'reviewer': 'dolores'}),
                       dict(issued, key_fingerprints={'author': 'x', 'reviewer': 'y'})):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                self.archive(forged)

    def test_signature_or_candidate_change_refused(self):
        issued = self.verify()
        self.sigs[0] = self.sigs[1]
        with self.assertRaises(ValueError): self.archive(issued)


if __name__ == '__main__':
    unittest.main()
