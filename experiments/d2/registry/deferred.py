"""Offline capability registry prototype. No production authority is implied."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")

class RegistryError(ValueError):
    pass

@dataclass(frozen=True, order=True)
class Capability:
    name: str
    capability_class: str
    enabled: bool

def generate(rows, deferred_classes):
    """Build a deterministic registry from explicitly supplied test authority.

    No normalization of names is allowed: ambiguous spelling fails closed.
    A deferred class cannot be enabled by the source or caller.
    """
    if not isinstance(rows, list) or not isinstance(deferred_classes, list):
        raise RegistryError('invalid source')
    if any(not isinstance(x, str) or not _NAME.fullmatch(x) for x in deferred_classes):
        raise RegistryError('invalid deferred class')
    if len(set(deferred_classes)) != len(deferred_classes):
        raise RegistryError('duplicate deferred class')
    deferred = frozenset(deferred_classes)
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'name', 'class', 'enabled'}:
            raise RegistryError('invalid capability')
        name, cls, enabled = row['name'], row['class'], row['enabled']
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(cls, str) or not _NAME.fullmatch(cls) or type(enabled) is not bool:
            raise RegistryError('invalid fields')
        if name in result:
            raise RegistryError('duplicate capability')
        if cls in deferred and enabled:
            raise RegistryError('deferred class enabled')
        result[name] = Capability(name, cls, enabled)
    return tuple(sorted(result.values()))

def serialize(registry):
    return json.dumps([{'name': c.name, 'class': c.capability_class, 'enabled': c.enabled}
                       for c in registry], sort_keys=True, separators=(',', ':')).encode()

def verify_generated(registry, expected_bytes):
    if not isinstance(expected_bytes, bytes) or serialize(registry) != expected_bytes:
        raise RegistryError('registry drift')
    return sha256(expected_bytes).hexdigest()

def authorize(registry, name):
    """Lookup only; caller must separately apply C1 policy and provenance."""
    for item in registry:
        if item.name == name:
            return item.enabled
    return False
