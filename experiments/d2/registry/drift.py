"""Synthetic strict drift check; not a live admission or execution boundary."""
from hashlib import sha256
import json

from .bound import generate_bound, _unique_object, _reject_constant, _finite_float
from .deferred import RegistryError
from .exposure import generate_exposure
from .synthetic_authority import TOOL_DEFINITION_PINS


def verify_observation_matches_fixture(manifest, registry, observed):
    """Return exposure digest only if all observed definitions exactly match.

    generate_exposure first validates the bounded input and authority fixture.
    Unknown, extra, disabled, changed and missing entries fail this stricter
    check; no observed names or descriptions are returned in the error. This
    helper neither suspends a real tool nor authenticates the observation.
    """
    projected = generate_exposure(manifest, registry, observed)
    output = json.loads(projected)
    expected = {row.name for row in generate_bound(manifest)
                if row.enabled and row.name in dict(TOOL_DEFINITION_PINS)}
    exposed = {row['name'] for row in output['tools']}
    # Already parsed, bounded and validated by generate_exposure above.
    items = json.loads(observed, object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant, parse_float=_finite_float)
    names = {row['name'] for row in items}
    if exposed != expected or names != expected:
        raise RegistryError('synthetic definition drift')
    return sha256(projected).hexdigest()
