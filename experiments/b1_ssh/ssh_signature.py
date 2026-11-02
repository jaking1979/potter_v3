"""B1 experimental SSH signature primitive, not a T39 receipt gate.

The caller must independently establish the authorized key set, claims,
reviewer identity, revocation, nonce, CI status and exact candidate binding.
No production consumer may use this experimental helper as authorization.
"""
import subprocess
from pathlib import Path

NAMESPACE = 'potter-review'

def verify_signature(payload: bytes, signature: Path, allowed_signers: Path, principal: str) -> bool:
    """Verify raw bytes under OpenSSH's potter-review namespace; fail closed."""
    if not isinstance(payload, bytes) or not isinstance(principal, str) or not principal.strip() or '\n' in principal or '\r' in principal:
        return False
    try:
        result = subprocess.run(
            ['ssh-keygen', '-Y', 'verify', '-n', NAMESPACE, '-f', str(allowed_signers),
             '-I', principal, '-s', str(signature)], input=payload,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False
