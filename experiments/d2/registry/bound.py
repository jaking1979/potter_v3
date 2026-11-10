"""Offline, synthetic authority pin. This is not a C1 authorization boundary."""
from hashlib import sha256
from hmac import compare_digest
import json
import math

from .deferred import RegistryError, generate, serialize, authorize
from .synthetic_authority import (
    EXPECTED_DIGEST, EXPECTED_SCHEMA, EXPECTED_VERSION,
    REQUIRED_CLASSES, DEFERRED_CLASSES,
)

MAX_MANIFEST_BYTES = 65536


def _reject_constant(value):
    raise ValueError('non-finite JSON constant')


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON number')
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError('duplicate JSON key')
        result[key] = value
    return result


def generate_bound(manifest_bytes):
    """Generate only from exact bytes pinned outside the supplied manifest.

    The pin is a checked-in SYNTHETIC fixture, not authenticated Notion/B2/B4
    material. No caller-supplied digest, deferred list or promotion switch exists.
    """
    if type(manifest_bytes) is not bytes or not 0 < len(manifest_bytes) <= MAX_MANIFEST_BYTES:
        raise RegistryError('invalid manifest bytes')
    if not compare_digest(sha256(manifest_bytes).hexdigest(), EXPECTED_DIGEST):
        raise RegistryError('manifest authority mismatch')
    try:
        manifest = json.loads(manifest_bytes, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RegistryError('invalid manifest JSON') from exc
    if type(manifest) is not dict or set(manifest) != {'schema', 'version', 'classes', 'capabilities'}:
        raise RegistryError('invalid manifest shape')
    if manifest['schema'] != EXPECTED_SCHEMA or manifest['version'] != EXPECTED_VERSION:
        raise RegistryError('manifest version mismatch')
    classes = manifest['classes']
    if type(classes) is not list:
        raise RegistryError('invalid class inventory')
    declared = {}
    for row in classes:
        if type(row) is not dict or set(row) != {'name', 'deferred'}:
            raise RegistryError('invalid class declaration')
        name = row['name']
        if type(name) is not str or name not in REQUIRED_CLASSES or type(row['deferred']) is not bool or name in declared:
            raise RegistryError('unknown or duplicate class')
        declared[name] = row['deferred']
    if set(declared) != set(REQUIRED_CLASSES):
        raise RegistryError('incomplete class inventory')
    if {name for name, deferred in declared.items() if deferred} != set(DEFERRED_CLASSES):
        raise RegistryError('deferred inventory mismatch')
    registry = generate(manifest['capabilities'], list(DEFERRED_CLASSES))
    if {row.capability_class for row in registry} != set(REQUIRED_CLASSES):
        raise RegistryError('incomplete capability coverage')
    return registry


def verify_bound(manifest_bytes, registry_bytes):
    """Reject omitted/extra/changed generated entries, including disabled entries."""
    expected = serialize(generate_bound(manifest_bytes))
    if type(registry_bytes) is not bytes or not compare_digest(expected, registry_bytes):
        raise RegistryError('generated registry mismatch')
    return sha256(expected).hexdigest()


def lookup_offline(manifest_bytes, registry_bytes, name):
    """Synthetic lookup, fail closed. This does not grant execution permission.

    Real runtime authorization still requires authenticated authority, C1 policy,
    revocation/update handling, and all package/release gates.
    """
    try:
        verify_bound(manifest_bytes, registry_bytes)
        return authorize(generate_bound(manifest_bytes), name)
    except RegistryError:
        return False
