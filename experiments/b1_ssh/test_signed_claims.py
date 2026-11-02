"""Synthetic cross-component test, not a receipt gate or T39 acceptance."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from claims import SCHEMA, attestation_bytes
from ssh_signature import verify_signature

@unittest.skipUnless(shutil.which('ssh-keygen'), 'OpenSSH not available')
class SignedClaimsTest(unittest.TestCase):
    def test_exact_claim_bytes_are_signed_and_mutations_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); key=p/'key'; message=p/'claims'; allowed=p/'allowed'
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
            allowed.write_text('reviewer '+Path(str(key)+'.pub').read_text())
            expected={'commit':'a'*40,'tree':'b'*40,'manifest':'c'*64,'evidence':'d'*64}
            claims={**expected,'schema':SCHEMA,'role':'reviewer','verdict':'approve','nonce':'e'*64,'expires_at':101}
            payload=attestation_bytes(claims,expected,100,60)
            message.write_bytes(payload)
            subprocess.run(['ssh-keygen','-Y','sign','-f',str(key),'-n','potter-review',str(message)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            sig=Path(str(message)+'.sig')
            self.assertTrue(verify_signature(payload,sig,allowed,'reviewer'))
            changed=attestation_bytes({**claims,'verdict':'request_changes'},expected,100,60)
            self.assertFalse(verify_signature(changed,sig,allowed,'reviewer'))
            with self.assertRaises(ValueError): attestation_bytes({**claims,'tree':'f'*40},expected,100,60)
            with self.assertRaises(ValueError): attestation_bytes(claims,expected,101,60)

if __name__ == '__main__': unittest.main()
