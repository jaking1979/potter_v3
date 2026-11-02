"""Experimental T39 attestation encoding. Not a receipt or release gate.

Validation here does not establish signatures, trusted key custody, CI state,
revocation, atomic nonce consumption, cross-review or any production authority.
"""
import json
import re

SCHEMA = 'potter.attestation/v0'
FIELDS = frozenset({
    'schema', 'commit', 'tree', 'manifest', 'evidence', 'role', 'verdict',
    'nonce', 'expires_at',
})
HEX40 = re.compile(r'[0-9a-f]{40}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
VERDICTS = frozenset({'approve', 'request_changes'})


def attestation_bytes(claims, expected, now, max_lifetime):
    """Validate exact candidate bindings and return deterministic bytes to sign.

    `expected`, `now`, and `max_lifetime` must come from the future trusted
    consumer, not from the attestation. Nonce consumption and replay protection
    remain separate.
    """
    if not isinstance(claims, dict) or set(claims) != FIELDS:
        raise ValueError('unexpected claims')
    if not isinstance(expected, dict) or set(expected) != {
        'commit', 'tree', 'manifest', 'evidence',
    }:
        raise ValueError('expected candidate required')
    if claims['schema'] != SCHEMA:
        raise ValueError('unsupported schema')
    if any(
        not isinstance(claims[key], str) or not HEX40.fullmatch(claims[key])
        for key in ('commit', 'tree')
    ):
        raise ValueError('invalid git identity')
    if any(
        not isinstance(claims[key], str) or not HEX64.fullmatch(claims[key])
        for key in ('manifest', 'evidence', 'nonce')
    ):
        raise ValueError('invalid hash or nonce')
    if any(claims[key] != expected[key] for key in expected):
        raise ValueError('candidate mismatch')
    if claims['role'] not in ('author', 'reviewer') or claims['verdict'] not in VERDICTS:
        raise ValueError('role or verdict refused')
    if (
        type(now) is not int
        or type(max_lifetime) is not int
        or max_lifetime <= 0
        or type(claims['expires_at']) is not int
        or claims['expires_at'] <= now
        or claims['expires_at'] - now > max_lifetime
    ):
        raise ValueError('expired or invalid time')
    return json.dumps(
        claims, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
        allow_nan=False,
    ).encode('ascii')
