"""T14 synthetic definition-drift projection; never execution permission."""
from hashlib import sha256
import json
import re

from .bound import generate_bound, verify_bound, _unique_object, MAX_MANIFEST_BYTES
from .deferred import RegistryError
from .synthetic_authority import EXPECTED_VERSION, TOOL_DEFINITION_PINS

_NAME = re.compile(r'^[a-z][a-z0-9_.-]{0,127}$')


def _reject_constant(value):
    raise ValueError('non-finite JSON constant')


def definition_hash(definition):
    """Canonical synthetic digest over all three T14 definition fields."""
    if (type(definition) is not dict
            or set(definition) != {'name', 'description', 'input_schema'}
            or type(definition['name']) is not str
            or not _NAME.fullmatch(definition['name'])
            or type(definition['description']) is not str
            or type(definition['input_schema']) is not dict):
        raise RegistryError('invalid tool definition')
    try:
        raw = json.dumps(definition, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False).encode('ascii')
    except (ValueError, TypeError, RecursionError) as exc:
        raise RegistryError('invalid tool definition JSON') from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RegistryError('tool definition too large')
    return sha256(raw).hexdigest()


def generate_exposure(manifest_bytes, registry_bytes, observed_bytes):
    """Expose only enabled fixture names with unchanged registered definitions.

    All inputs are synthetic. Observations cannot register/re-admit tools, set
    tiers or expand authority. There is no execution or external notification.
    Unknown/changed/missing tools remain absent; source text is never echoed.
    """
    verify_bound(manifest_bytes, registry_bytes)
    registry = generate_bound(manifest_bytes)
    if type(observed_bytes) is not bytes or not 0 < len(observed_bytes) <= MAX_MANIFEST_BYTES:
        raise RegistryError('invalid observed definitions')
    try:
        observed = json.loads(observed_bytes, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RegistryError('invalid observed JSON') from exc
    if type(observed) is not list:
        raise RegistryError('invalid observed list')
    hashes = {}
    for definition in observed:
        digest = definition_hash(definition)
        name = definition['name']
        if name in hashes:
            raise RegistryError('duplicate observed tool')
        hashes[name] = digest
    pins = dict(TOOL_DEFINITION_PINS)
    if len(pins) != len(TOOL_DEFINITION_PINS):
        raise RegistryError('duplicate definition pin')
    tools = [{'name': row.name, 'definition_hash': pins[row.name]}
             for row in registry if row.enabled and row.name in pins
             and hashes.get(row.name) == pins[row.name]]
    output = {'schema': 'potter.d2.synthetic-exposure.v1',
              'registry_version': EXPECTED_VERSION, 'tools': tools}
    return json.dumps(output, sort_keys=True, separators=(',', ':')).encode('ascii')
