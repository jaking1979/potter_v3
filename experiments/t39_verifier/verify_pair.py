"""Offline T39 pair-verifier prototype. Not an authorization boundary.

The caller must independently authenticate every input, including the expected
candidate, checks, role/key mapping, revocation state and clock. This prototype
does not consume nonces, authenticate that input, verify historical receipts,
or authorize a merge, build, rollback, or deployment.
"""
import base64
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}\Z')
_FIELDS = {'schema', 'commit', 'tree', 'manifest', 'evidence', 'role',
           'verdict', 'nonce', 'expires_at'}
_ROLES = ('author', 'reviewer')
_LIMIT = 65536


def _key(key):
    if not isinstance(key, str) or len(key) > 1024:
        raise ValueError('invalid key')
    parts = key.split(' ')
    if len(parts) != 2 or parts[0] != 'ssh-ed25519':
        raise ValueError('unsupported key')
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid key') from exc
    if base64.b64encode(raw).decode('ascii') != parts[1]:
        raise ValueError('noncanonical key')
    # SSH wire encoding: string "ssh-ed25519" followed by a 32-byte key.
    if len(raw) != 51 or raw[:15] != b'\x00\x00\x00\x0bssh-ed25519' or raw[15:19] != b'\x00\x00\x00\x20':
        raise ValueError('invalid ed25519 key blob')
    return hashlib.sha256(raw).hexdigest()


def verify_pair(author_payload, author_signature, reviewer_payload, reviewer_signature,
                *, expected, agents, agent_keys, revoked_fingerprints,
                required_checks, check_statuses, open_request_changes, now, max_lifetime):
    """Verify synthetic active receipts and return facts; never record or consume.

    Signatures are immutable bytes. No network access is used. Caller supplies
    a trusted snapshot; this function does not establish its authenticity.
    """
    if type(now) is not int or type(max_lifetime) is not int or max_lifetime <= 0:
        raise ValueError('invalid clock or lifetime')
    if type(expected) is not dict or set(expected) != {'commit', 'tree', 'manifest', 'evidence'}:
        raise ValueError('candidate required')
    expected = dict(expected)
    if type(agents) is not dict or set(agents) != set(_ROLES):
        raise ValueError('role mapping required')
    agents = dict(agents)
    if any(not isinstance(agents[r], str) or not _NAME.fullmatch(agents[r]) for r in _ROLES) or agents['author'] == agents['reviewer']:
        raise ValueError('distinct agents required')
    if type(agent_keys) is not dict or any(agents[r] not in agent_keys for r in _ROLES):
        raise ValueError('missing key')
    keys = {r: agent_keys[agents[r]] for r in _ROLES}
    fingerprints = {r: _key(keys[r]) for r in _ROLES}
    if type(revoked_fingerprints) not in (set, frozenset) or any(not isinstance(v, str) or not _HEX64.fullmatch(v) for v in revoked_fingerprints):
        raise ValueError('invalid revocation snapshot')
    if fingerprints['author'] == fingerprints['reviewer'] or any(v in revoked_fingerprints for v in fingerprints.values()):
        raise ValueError('same or revoked key')
    if type(required_checks) not in (set, frozenset) or not required_checks or any(not isinstance(x, str) or not x for x in required_checks):
        raise ValueError('checks required')
    if type(check_statuses) is not dict or any(check_statuses.get(x) is not True for x in required_checks) or open_request_changes is not False:
        raise ValueError('review or checks incomplete')
    payloads = (author_payload, reviewer_payload)
    signatures = (author_signature, reviewer_signature)
    claims = []
    for role, payload, signature in zip(_ROLES, payloads, signatures):
        if type(payload) is not bytes or type(signature) is not bytes or not payload or not signature or len(payload) > _LIMIT or len(signature) > _LIMIT:
            raise ValueError('invalid payload or signature')
        try:
            record = json.loads(payload.decode('ascii'))
        except (ValueError, UnicodeError) as exc:
            raise ValueError('invalid payload') from exc
        if type(record) is not dict or set(record) != _FIELDS:
            raise ValueError('invalid claims')
        if json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii') != payload:
            raise ValueError('noncanonical claims')
        if record['schema'] != 'potter.attestation/v0' or record['role'] != role or record['verdict'] != 'approve':
            raise ValueError('wrong role or verdict')
        if any(not isinstance(record[k], str) or not _HEX40.fullmatch(record[k]) for k in ('commit', 'tree')) or any(not isinstance(record[k], str) or not _HEX64.fullmatch(record[k]) for k in ('manifest', 'evidence', 'nonce')):
            raise ValueError('invalid binding')
        if any(record[k] != expected[k] for k in expected):
            raise ValueError('candidate mismatch')
        expiry = record['expires_at']
        if type(expiry) is not int or expiry <= now or expiry - now > max_lifetime:
            raise ValueError('expired or invalid expiry')
        claims.append(record)
    if claims[0]['nonce'] == claims[1]['nonce']:
        raise ValueError('duplicate nonce')
    with tempfile.TemporaryDirectory(prefix='potter-t39-offline-') as directory:
        root = Path(directory)
        for role, payload, signature in zip(_ROLES, payloads, signatures):
            allowed, sig = root / (role + '.keys'), root / (role + '.sig')
            allowed.write_text(role + ' ' + keys[role] + '\n', encoding='ascii')
            sig.write_bytes(signature)
            try:
                outcome = subprocess.run(['ssh-keygen', '-Y', 'verify', '-n', 'potter-review',
                                          '-f', str(allowed), '-I', role, '-s', str(sig)],
                                         input=payload, capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError('signature verifier unavailable') from exc
            if outcome.returncode != 0:
                raise ValueError('invalid signature')
    digest = hashlib.sha256(b'potter.receipt/v0\0' + payloads[0] + b'\0' + payloads[1]).hexdigest()
    return {'commit': claims[0]['commit'], 'tree': claims[0]['tree'], 'receipt_hash': digest,
            'nonces': (claims[0]['nonce'], claims[1]['nonce']), 'agents': agents,
            'key_fingerprints': fingerprints, 'verifier_version': 'offline-prototype-v1'}
