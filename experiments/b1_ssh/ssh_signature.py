"""B1 experimental SSH signature primitive, not a T39 receipt gate.

The caller must independently establish the authorized key set, claims,
reviewer identity, revocation, nonce, CI status and exact candidate binding.
No production consumer may use this experimental helper as authorization.
"""
import base64
import binascii
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

NAMESPACE = 'potter-review'
MAX_INPUT_FILE_BYTES = 1_048_576
ALLOWED_KEY_TYPES = frozenset({'ssh-ed25519'})
_PRINCIPAL = re.compile(r'[A-Za-z0-9._@-]+\Z')


def _read_regular_bounded(path: Path) -> bytes:
    """Open once without following the final symlink or blocking on a FIFO."""
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK'):
        raise OSError('required safe-open flags unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError('input is not a regular file')
        return stream.read(MAX_INPUT_FILE_BYTES + 1)


def _safe_allowed_signers(allowed_signers: Path) -> bool:
    """Allow only literal-principal ed25519 signer entries; reject options/CAs."""
    try:
        if not allowed_signers.is_file() or allowed_signers.stat().st_size > MAX_INPUT_FILE_BYTES:
            return False
        for raw_line in allowed_signers.read_text(encoding='ascii').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            fields = line.split()
            if (
                len(fields) < 3
                or not _PRINCIPAL.fullmatch(fields[0])
                or fields[1] not in ALLOWED_KEY_TYPES
            ):
                return False
            try:
                base64.b64decode(fields[2], validate=True)
            except (binascii.Error, ValueError):
                return False
        return True
    except (OSError, UnicodeError):
        return False


def verify_signature(payload: bytes, signature: Path, allowed_signers: Path, principal: str) -> bool:
    """Verify raw bytes under OpenSSH's potter-review namespace; fail closed."""
    if (
        not isinstance(payload, bytes)
        or not isinstance(principal, str)
        or not _PRINCIPAL.fullmatch(principal)
        or not isinstance(signature, Path)
        or not isinstance(allowed_signers, Path)
    ):
        return False
    try:
        signer_bytes = _read_regular_bounded(allowed_signers)
        signature_bytes = _read_regular_bounded(signature)
        if (
            len(signer_bytes) > MAX_INPUT_FILE_BYTES
            or len(signature_bytes) > MAX_INPUT_FILE_BYTES
        ):
            return False
        with tempfile.TemporaryDirectory(prefix='potter-b1-') as directory:
            signer_snapshot = Path(directory) / 'allowed_signers'
            signature_snapshot = Path(directory) / 'signature'
            signer_snapshot.write_bytes(signer_bytes)
            signature_snapshot.write_bytes(signature_bytes)
            signer_snapshot.chmod(0o600)
            signature_snapshot.chmod(0o600)
            if not _safe_allowed_signers(signer_snapshot):
                return False
            result = subprocess.run(
                [
                    'ssh-keygen', '-Y', 'verify', '-n', NAMESPACE,
                    '-f', str(signer_snapshot), '-I', principal,
                    '-s', str(signature_snapshot),
                ],
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False
