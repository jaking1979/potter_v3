import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from ssh_signature import verify_signature

class SSHSignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.key = self.d / 'ephemeral'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)], check=True)
        self.allowed = self.d / 'allowed'
        self.allowed.write_text('reviewer ' + self.key.with_suffix('.pub').read_text())
        self.msg = self.d / 'message'
        self.msg.write_bytes(b'synthetic exact candidate')
    def tearDown(self): self.tmp.cleanup()
    def sign(self, namespace='potter-review'):
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(self.key), '-n', namespace, str(self.msg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return Path(str(self.msg)+'.sig')
    def test_valid_exact_message(self):
        sig=self.sign()
        self.assertTrue(verify_signature(self.msg.read_bytes(),sig,self.allowed,'reviewer'))
    def test_changed_message_and_wrong_principal_fail(self):
        sig=self.sign()
        self.assertFalse(verify_signature(b'changed',sig,self.allowed,'reviewer'))
        self.assertFalse(verify_signature(self.msg.read_bytes(),sig,self.allowed,'author'))
    def test_wrong_namespace_missing_file_and_bad_signature_fail(self):
        sig=self.sign('other')
        self.assertFalse(verify_signature(self.msg.read_bytes(),sig,self.allowed,'reviewer'))
        self.assertFalse(verify_signature(self.msg.read_bytes(),self.d/'missing',self.allowed,'reviewer'))
        sig.write_text('forged')
        self.assertFalse(verify_signature(self.msg.read_bytes(),sig,self.allowed,'reviewer'))
    def test_host_public_key_cannot_sign(self):
        isolated = self.d / 'public-only'
        isolated.mkdir()
        public_key = isolated / 'key.pub'
        public_key.write_bytes(self.key.with_suffix('.pub').read_bytes())
        env = dict(os.environ)
        env.pop('SSH_AUTH_SOCK', None)
        p=subprocess.run(['ssh-keygen','-Y','sign','-f',str(public_key),'-n','potter-review',str(self.msg)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
        self.assertNotEqual(p.returncode,0)

if __name__ == '__main__': unittest.main()
