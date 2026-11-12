import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from verify_pair import verify_pair, _key
from t39.consumer_gate import admit_merge, require_issued, Refused
from t39.receipt_registry import FileAnchor, ReceiptRegistry


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.keys = {}
        for name in ('dolores', 'claude'):
            path = self.root / name
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True)
            self.keys[name] = ' '.join(path.with_suffix('.pub').read_text().split()[:2])
        self.expected = {'commit': 'a' * 40, 'tree': 'b' * 40, 'manifest': 'c' * 64, 'evidence': 'd' * 64}
        self.kw = dict(expected=self.expected, agents={'author': 'dolores', 'reviewer': 'claude'},
                       agent_keys=self.keys, revoked_fingerprints=set(), required_checks={'tests'},
                       check_statuses={'tests': True}, open_request_changes=False, now=100, max_lifetime=60)
        self.payloads = [self.payload('author', 'e' * 64), self.payload('reviewer', 'f' * 64)]
        self.sigs = [self.sign(self.payloads[0], 'dolores', 'one'), self.sign(self.payloads[1], 'claude', 'two')]

    def payload(self, role, nonce, **override):
        r = dict(self.expected, schema='potter.attestation/v0', role=role, verdict='approve', nonce=nonce, expires_at=150)
        r.update(override)
        return json.dumps(r, sort_keys=True, separators=(',', ':')).encode()

    def sign(self, data, name, filename, namespace='potter-review'):
        path = self.root / filename
        path.write_bytes(data)
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-n', namespace, '-f', str(self.root / name), str(path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return Path(str(path) + '.sig').read_bytes()

    def verify(self, **changes):
        kw = dict(self.kw, **changes)
        return verify_pair(self.payloads[0], self.sigs[0], self.payloads[1], self.sigs[1], **kw)

    def test_valid_signatures_and_repeatable_verification(self):
        first = self.verify()
        self.assertEqual(first, self.verify())
        self.assertEqual(first['nonces'], ('e' * 64, 'f' * 64))
        self.assertNotEqual(first['key_fingerprints']['author'], first['key_fingerprints']['reviewer'])

    def test_forged_wrong_namespace_and_mutated_payload_refused(self):
        self.sigs[0] = self.sigs[1]
        with self.assertRaises(ValueError): self.verify()
        self.sigs[0] = self.sign(self.payloads[0], 'dolores', 'wrongns', 'wrong-namespace')
        with self.assertRaises(ValueError): self.verify()
        self.sigs[0] = self.sign(self.payloads[0], 'dolores', 'three')
        self.payloads[0] = self.payload('author', 'a' * 64)
        with self.assertRaises(ValueError): self.verify()

    def test_revoked_key_distinct_agents_and_keys(self):
        with self.assertRaises(ValueError): self.verify(revoked_fingerprints={_key(self.keys['claude'])})
        with self.assertRaises(ValueError): self.verify(agents={'author': 'claude', 'reviewer': 'claude'})
        with self.assertRaises(ValueError): self.verify(agent_keys={'claude': self.keys['claude'], 'dolores': self.keys['claude']})

    def test_checks_expiry_candidate_and_review_fail_closed(self):
        for changed in (dict(now=150), dict(expected=dict(self.expected, tree='f' * 40)),
                        dict(check_statuses={'tests': False}), dict(open_request_changes=True),
                        dict(required_checks=set())):
            with self.subTest(changed=changed), self.assertRaises(ValueError): self.verify(**changed)

    def test_duplicate_nonce_swapped_roles_and_wrong_key(self):
        self.payloads[1] = self.payload('reviewer', 'e' * 64)
        with self.assertRaises(ValueError): self.verify()
        self.payloads.reverse()
        with self.assertRaises(ValueError): self.verify()
        self.payloads.reverse()
        self.payloads[1] = self.payload('reviewer', 'f' * 64)
        with self.assertRaises(ValueError): self.verify(agent_keys={'claude': self.keys['dolores'], 'dolores': self.keys['claude']})

    def test_registry_lifecycle_with_real_signed_pair_and_replay(self):
        """Scratch-only composition, not proof of trusted provenance or deployment."""
        anchor = FileAnchor(self.root / 'anchor.jsonl')
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        registry = ReceiptRegistry.initialize(self.root / 'registry.sqlite', anchor, 'test', stamp)
        self.addCleanup(registry.close)
        receipt = tuple(zip(self.payloads, self.sigs))
        def verifier(pair):
            return verify_pair(pair[0][0], pair[0][1], pair[1][0], pair[1][1], **self.kw)
        common = dict(commit=self.expected['commit'], tree=self.expected['tree'],
                      load_receipt=lambda _: receipt, verify=verifier, registry=registry, now=stamp)
        admit_merge(**common, issuer='ci')
        for consumer in ('ci', 'builder', 'host', 'rollback'):
            with self.subTest(consumer=consumer):
                require_issued(**common, consumer=consumer, purpose='deploy', on_main=True)
        self.kw['revoked_fingerprints'] = {_key(self.keys['claude'])}
        with self.assertRaises(Refused):
            require_issued(**common, consumer='host', purpose='deploy', on_main=True)

    def test_signed_distinct_candidate_reusing_nonce_is_rejected(self):
        anchor = FileAnchor(self.root / 'replay-anchor.jsonl')
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        registry = ReceiptRegistry.initialize(self.root / 'replay.sqlite', anchor, 'test', stamp)
        self.addCleanup(registry.close)
        def verifier(pair):
            return verify_pair(pair[0][0], pair[0][1], pair[1][0], pair[1][1], **self.kw)
        receipt = tuple(zip(self.payloads, self.sigs))
        admit_merge(commit=self.expected['commit'], tree=self.expected['tree'], load_receipt=lambda _: receipt,
                    verify=verifier, registry=registry, issuer='ci', now=stamp)
        self.expected = dict(self.expected, commit='9' * 40)
        self.kw['expected'] = self.expected
        payloads = [self.payload('author', 'e' * 64), self.payload('reviewer', '1' * 64)]
        signatures = [self.sign(payloads[0], 'dolores', 'replay-a'), self.sign(payloads[1], 'claude', 'replay-b')]
        replay = tuple(zip(payloads, signatures))
        with self.assertRaises(Refused):
            admit_merge(commit=self.expected['commit'], tree=self.expected['tree'], load_receipt=lambda _: replay,
                        verify=verifier, registry=registry, issuer='ci', now=stamp)

    def test_signed_noncanonical_or_malformed_claims_fail_closed(self):
        valid = json.loads(self.payloads[0])
        cases = [
            json.dumps(valid, sort_keys=True).encode(),  # signed whitespace
            json.dumps(dict(reversed(list(valid.items()))), separators=(',', ':')).encode(),
            self.payloads[0][:-1] + b',"role":"author"}',  # duplicate key
            self.payload('author', 'e' * 64, unexpected='field'),
            json.dumps({k: v for k, v in valid.items() if k != 'evidence'}, sort_keys=True, separators=(',', ':')).encode(),
            self.payload('author', 'e' * 64, expires_at=True),
            self.payloads[0] + b'\xff',
        ]
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                self.payloads[0] = payload
                self.sigs[0] = self.sign(payload, 'dolores', f'bad-{index}')
                with self.assertRaises(ValueError): self.verify()

    def test_invalid_signer_keys_fail_closed(self):
        author = self.keys['dolores']
        for bad in ('ssh-rsa AAAA', author + ' trailing-comment', author + '=',
                    author.replace('ssh-ed25519', 'ssh-ed448'), 'ssh-ed25519 AAAA'):
            with self.subTest(key=bad), self.assertRaises(ValueError):
                self.verify(agent_keys={'dolores': bad, 'claude': self.keys['claude']})


if __name__ == '__main__':
    unittest.main()
