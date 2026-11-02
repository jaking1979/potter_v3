"""Experimental T39 attestation encoding. Not a receipt or release gate.

Validation here does not establish signatures, trusted key custody, CI state,
revocation, atomic nonce consumption, cross-review or any production authority.
"""
import json
import re

FIELDS = frozenset({'commit', 'tree', 'manifest', 'evidence', 'role', 'verdict', 'nonce', 'expires_at'})
HEX40 = re.compile(r'[0-9a-f]{40}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')

def attestation_bytes(claims, expected, now):
    """Validate exact candidate bindings and return deterministic bytes to sign.

    `expected` and `now` must come from the future trusted consumer, not from
    the attestation. Nonce consumption and replay protection remain separate.
    """
    if not isinstance(claims, dict) or set(claims) != FIELDS:
        raise ValueError('unexpected claims')
    if not isinstance(expected, dict) or set(expected) != {'commit', 'tree', 'manifest', 'evidence'}:
        raise ValueError('expected candidate required')
    if any(not isinstance(claims[k], str) or not HEX40.fullmatch(claims[k]) for k in ('commit', 'tree')):
        raise ValueError('invalid git identity')
    if any(not isinstance(claims[k], str) or not HEX64.fullmatch(claims[k]) for k in ('manifest', 'evidence', 'nonce')):
        raise ValueError('invalid hash or nonce')
    if any(claims[k] != expected[k] for k in expected):
        raise ValueError('candidate mismatch')
    if claims['role'] not in ('author', 'reviewer') or claims['verdict'] != 'approve':
        raise ValueError('role or verdict refused')
    if type(now) is not int or type(claims['expires_at']) is not int or claims['expires_at'] <= now:
        raise ValueError('expired or invalid time')
    return json.dumps(claims, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
