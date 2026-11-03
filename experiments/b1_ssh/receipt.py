"""Experimental T39 receipt composition, not a production gate.

It proves only in-process composition against caller-provided trusted public keys and
nonce consumer. The caller must supply pre-candidate trusted configuration and a
durable atomic consumer; this module creates no authority, receipt, or release path.
"""
import base64
import hashlib
import json
import re
import tempfile
from pathlib import Path

from claims import attestation_bytes
from ssh_signature import verify_signature

_KEY = re.compile(r"ssh-ed25519 ([A-Za-z0-9+/]+={0,2})\Z")
_ROLES = ("author", "reviewer")


def _trusted_key(value):
    """Return one strictly formed public-key entry without a principal/options."""
    if not isinstance(value, str):
        raise ValueError("trusted key must be text")
    match = _KEY.fullmatch(value.strip())
    if not match:
        raise ValueError("trusted key must be an ed25519 public key")
    try:
        base64.b64decode(match.group(1), validate=True)
    except ValueError as error:
        raise ValueError("invalid trusted key encoding") from error
    return match.group(0)


def _claims(payload, expected, now, max_lifetime, role):
    if not isinstance(payload, bytes):
        raise ValueError("attestation payload must be bytes")
    try:
        claims = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid attestation encoding") from error
    if claims.get("role") != role:
        raise ValueError("attestation role mismatch")
    canonical = attestation_bytes(claims, expected, now, max_lifetime)
    if payload != canonical:
        raise ValueError("noncanonical attestation bytes")
    return claims


def verify_experimental_receipt(
    author_payload, author_signature, reviewer_payload, reviewer_signature,
    trusted_keys, expected, required_checks, check_statuses,
    open_request_changes, now, max_lifetime, consume_nonces,
):
    """Return immutable receipt facts after all experimental checks pass.

    trusted_keys must be a pre-candidate mapping for exactly author/reviewer.
    consume_nonces must atomically consume the whole two-nonce tuple and return
    true only once. This function does not provide that state or establish origin.
    """
    if not isinstance(trusted_keys, dict) or set(trusted_keys) != set(_ROLES):
        raise ValueError("both trusted roles required")
    keys = {role: _trusted_key(trusted_keys[role]) for role in _ROLES}
    if keys["author"] == keys["reviewer"]:
        raise ValueError("same key cannot self-review")
    if not isinstance(required_checks, (set, frozenset)) or not required_checks:
        raise ValueError("required checks must be nonempty")
    if not isinstance(check_statuses, dict) or any(
        check_statuses.get(name) is not True for name in required_checks
    ):
        raise ValueError("required checks are not green")
    if open_request_changes is not False:
        raise ValueError("request-changes state is not closed")
    if not callable(consume_nonces):
        raise ValueError("atomic nonce consumer required")

    author = _claims(author_payload, expected, now, max_lifetime, "author")
    reviewer = _claims(reviewer_payload, expected, now, max_lifetime, "reviewer")
    if author["verdict"] != "approve" or reviewer["verdict"] != "approve":
        raise ValueError("approval verdict required")
    nonces = (author["nonce"], reviewer["nonce"])
    if nonces[0] == nonces[1]:
        raise ValueError("attestations need distinct nonces")

    with tempfile.TemporaryDirectory(prefix="potter-b1-receipt-") as directory:
        root = Path(directory)
        for role, payload, signature in (
            ("author", author_payload, author_signature),
            ("reviewer", reviewer_payload, reviewer_signature),
        ):
            allowed = root / (role + "-allowed")
            allowed.write_text(role + " " + keys[role] + "\n", encoding="ascii")
            allowed.chmod(0o600)
            if not verify_signature(payload, signature, allowed, role):
                raise ValueError("invalid " + role + " signature")

    if consume_nonces(nonces) is not True:
        raise ValueError("nonce pair unavailable or already consumed")
    return {
        "commit": author["commit"],
        "tree": author["tree"],
        "manifest": author["manifest"],
        "evidence": author["evidence"],
        "key_fingerprints": {
            role: hashlib.sha256(keys[role].encode("ascii")).hexdigest()
            for role in _ROLES
        },
    }
