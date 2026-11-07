"""Bounded synthetic bytes adapter, not a server/authentication boundary.

Only the upstream reviewed C7 adapter may eventually supply real snapshots.
Malformed bytes become unavailable state without reflecting input/error text.
"""
import json
import math

from .home import project, respond

MAX_BYTES = 131072
MAX_DEPTH = 8


def _depth(text):
    # Preflight before Python's recursive JSON parser. Ignore quoted braces
    # and escaped quotes. JSON syntax itself is checked by json.loads below.
    quoted = escaped = False
    depth = 0
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError('depth')
        elif char in ']}':
            depth -= 1


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate key')
        obj[key] = value
    return obj


def _number(text):
    if len(text) > 64:
        raise ValueError('numeric token')
    number = float(text)
    if not math.isfinite(number):
        raise ValueError('nonfinite')
    return number


def _integer(text):
    if len(text) > 64:
        raise ValueError('numeric token')
    return int(text)


def _constant(text):
    raise ValueError('nonfinite')


def _strings(value):
    # Recursive work is already depth/byte bounded by the preflight.
    if type(value) is str:
        if len(value) > 4096:
            raise ValueError('string bound')
        value.encode('utf-8', errors='strict')
    elif type(value) is dict:
        for key, child in value.items():
            _strings(key)
            _strings(child)
    elif type(value) is list:
        for child in value:
            _strings(child)


def load_snapshot(payload):
    """New detached plain-JSON value, or None for an invalid envelope.

    Limits are explicit local prototype bounds, not a shared C7 contract.
    No input contents or decoder errors escape through failure output.
    """
    if type(payload) is not bytes or len(payload) > MAX_BYTES:
        return None
    try:
        text = payload.decode('utf-8', errors='strict')
        _depth(text)
        value = json.loads(text, object_pairs_hook=_object,
                           parse_constant=_constant, parse_float=_number,
                           parse_int=_integer)
        _strings(value)
        return value if type(value) is dict else None
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return None


def project_json(payload, *, now):
    return project(load_snapshot(payload), now=now)


def respond_json(method, path, payload, *, now):
    """Pure formatter. Refuse actions/paths before loading any input bytes."""
    if method != 'GET' or path not in ('/', '/api/status'):
        return respond(method, path, None, now=None)
    return respond(method, path, load_snapshot(payload), now=now)
