"""Offline A4a pair identifier, interoperable with pair_identity.mjs.

An ID alone cannot authenticate fixtures, prove their presentation, blinding,
independent grading, or authorize a baseline run.
"""
import hashlib
import re


def pair_identity(case_id, version, output_a, output_b):
    if type(case_id) is not str or not case_id.strip() or type(version) is not str or not version.strip():
        raise ValueError('case ID and version required')
    if type(output_a) is not bytes or type(output_b) is not bytes:
        raise TypeError('fixture bytes required')
    digest = hashlib.sha256(b'potter-a4a-pair-v1\0')
    for value in (case_id.encode('utf-8'), version.encode('utf-8'), output_a, output_b):
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    return digest.hexdigest()


def require_same_pair(first, second):
    if any(type(x) is not str or re.fullmatch(r'[0-9a-f]{64}', x) is None for x in (first, second)) or first != second:
        raise ValueError('pair identity mismatch')
