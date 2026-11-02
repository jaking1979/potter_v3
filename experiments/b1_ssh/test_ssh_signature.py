import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ssh_signature import verify_signature


@unittest.skipUnless(shutil.which('ssh-keygen'), 'OpenSSH ssh-keygen is required')
class SSHSignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.key = self.d / 'ephemeral'
        subprocess.run(
            ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)],
            check=True,
        )
        self.allowed = self.d / 'allowed'
        self.allowed.write_text('reviewer ' + self.key.with_suffix('.pub').read_text())
        self.msg = self.d / 'message'
        self.msg.write_bytes(b'synthetic exact candidate')

    def tearDown(self):
        self.tmp.cleanup()

    def sign(self, key=None, namespace='potter-review'):
        subprocess.run(
            ['ssh-keygen', '-Y', 'sign', '-f', str(key or self.key), '-n', namespace, str(self.msg)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return Path(str(self.msg) + '.sig')

    def test_valid_exact_message(self):
        signature = self.sign()
        self.assertTrue(verify_signature(
            self.msg.read_bytes(), signature, self.allowed, 'reviewer',
        ))

    def test_changed_same_length_message_and_wrong_principal_fail(self):
        signature = self.sign()
        self.assertFalse(verify_signature(b'x' * len(self.msg.read_bytes()), signature, self.allowed, 'reviewer'))
        self.assertFalse(verify_signature(
            self.msg.read_bytes(), signature, self.allowed, 'author',
        ))

    def test_untrusted_key_and_wildcard_principal_fail(self):
        other = self.d / 'other'
        subprocess.run(
            ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)],
            check=True,
        )
        signature = self.sign(key=other)
        self.assertFalse(verify_signature(
            self.msg.read_bytes(), signature, self.allowed, 'reviewer',
        ))
        self.allowed.write_text('* ' + self.key.with_suffix('.pub').read_text())
        signature = self.sign()
        self.assertFalse(verify_signature(
            self.msg.read_bytes(), signature, self.allowed, 'reviewer',
        ))
        self.allowed.write_text('reviewer cert-authority ' + self.key.with_suffix('.pub').read_text())
        self.assertFalse(verify_signature(
            self.msg.read_bytes(), signature, self.allowed, 'reviewer',
        ))

    def test_wrong_namespace_missing_file_and_bad_signature_fail(self):
        signature = self.sign(namespace='other')
        self.assertFalse(verify_signature(
            self.msg.read_bytes(), signature, self.allowed, 'reviewer',
        ))
        self.assertFalse(verify_signature(
            self.msg.read_bytes(), self.d / 'missing', self.allowed, 'reviewer',
        ))
        signature.write_text('forged')
        self.assertFalse(verify_signature(
            self.msg.read_bytes(), signature, self.allowed, 'reviewer',
        ))

    def test_host_public_key_cannot_sign(self):
        isolated = self.d / 'public-only'
        isolated.mkdir()
        public_key = isolated / 'key.pub'
        public_key.write_bytes(self.key.with_suffix('.pub').read_bytes())
        env = dict(os.environ)
        env.pop('SSH_AUTH_SOCK', None)
        process = subprocess.run(
            ['ssh-keygen', '-Y', 'sign', '-f', str(public_key), '-n', 'potter-review', str(self.msg)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self.assertNotEqual(process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
