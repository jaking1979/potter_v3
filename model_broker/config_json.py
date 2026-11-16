"""Offline experiment for decoding bounded synthetic ModelBroker candidates.

No file I/O, source authentication or provider dispatch. The caller has already
allocated the input bytes; a real reader must enforce a limit before reading.
"""
import json

from .broker import Candidate

MAX_CONFIG_BYTES = 256 * 1024  # experimental, not an approved production limit
MAX_CANDIDATES = 1024


def _pairs_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("invalid_constant")


def decode_candidates(raw):
    """Decode exact bytes into immutable candidates, or fail without partial output."""
    if type(raw) is not bytes or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("malformed_configuration")
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_unique,
                             parse_constant=_invalid_constant)
        if type(decoded) is not list or len(decoded) > MAX_CANDIDATES:
            raise ValueError("invalid_collection")
        return tuple(Candidate.from_json_config(item) for item in decoded)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError("malformed_configuration") from None
