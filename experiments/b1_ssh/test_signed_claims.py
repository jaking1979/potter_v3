"""Synthetic cross-component test, not a receipt gate or T39 acceptance."""
import shutil
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
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

    def test_verified_signers_are_snapshot_not_raced_source(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); key=p/'key'; message=p/'message'; allowed=p/'allowed'
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
            allowed.write_text('reviewer '+Path(str(key)+'.pub').read_text())
            message.write_bytes(b'fixture')
            subprocess.run(['ssh-keygen','-Y','sign','-f',str(key),'-n','potter-review',str(message)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            sig=Path(str(message)+'.sig')
            actual_run=subprocess.run
            def replace_source(*args,**kwargs):
                allowed.write_text('* '+Path(str(key)+'.pub').read_text())
                sig.write_text('invalid')
                return actual_run(*args,**kwargs)
            with patch('ssh_signature.subprocess.run',side_effect=replace_source):
                self.assertTrue(verify_signature(b'fixture',sig,allowed,'reviewer'))
            self.assertFalse(verify_signature(b'fixture',sig,allowed,'reviewer'))

    @unittest.skipUnless(hasattr(os, 'mkfifo') and hasattr(os, 'O_NOFOLLOW'), 'requires POSIX safe-open')
    def test_symlink_and_fifo_sources_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); regular=p/'regular'; regular.write_text('not a signature')
            link=p/'link'; link.symlink_to(regular)
            fifo=p/'fifo'; os.mkfifo(fifo)
            self.assertFalse(verify_signature(b'fixture',link,regular,'reviewer'))
            self.assertFalse(verify_signature(b'fixture',regular,link,'reviewer'))
            self.assertFalse(verify_signature(b'fixture',fifo,regular,'reviewer'))
            self.assertFalse(verify_signature(b'fixture',regular,fifo,'reviewer'))

    def test_fdopen_failure_closes_descriptor(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source'; source.write_bytes(b'data')
            real_close=os.close
            closed=[]
            def track_close(fd):
                closed.append(fd)
                real_close(fd)
            with patch('ssh_signature.os.fdopen',side_effect=OSError('synthetic failure')), patch('ssh_signature.os.close',side_effect=track_close):
                self.assertFalse(verify_signature(b'data',source,source,'reviewer'))
            self.assertEqual(len(closed),1)

if __name__ == '__main__': unittest.main()
