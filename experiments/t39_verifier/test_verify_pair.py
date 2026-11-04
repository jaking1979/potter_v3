import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from verify_pair import verify_pair, _key


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


if __name__ == '__main__':
    unittest.main()
