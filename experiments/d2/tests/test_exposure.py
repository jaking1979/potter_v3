import copy
import json
from pathlib import Path
import unittest

from registry.bound import generate_bound
from registry.deferred import RegistryError, serialize
from registry.exposure import definition_hash, generate_exposure
from registry.synthetic_authority import EXPECTED_VERSION, TOOL_DEFINITION_PINS

ROOT = Path(__file__).resolve().parents[1]


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.manifest = (ROOT / 'fixtures/synthetic-manifest.json').read_bytes()
        self.registry = serialize(generate_bound(self.manifest))
        self.raw = (ROOT / 'fixtures/synthetic-tools.json').read_bytes()
        self.tools = json.loads(self.raw)

    def project(self, tools):
        return json.loads(generate_exposure(self.manifest, self.registry, encode(tools)))

    def test_pinned_definition_exposed_with_version_deterministically(self):
        self.assertEqual(definition_hash(self.tools[0]), dict(TOOL_DEFINITION_PINS)['notes.read'])
        expected = {'schema': 'potter.d2.synthetic-exposure.v1',
                    'registry_version': EXPECTED_VERSION,
                    'tools': [{'name': 'notes.read', 'definition_hash': definition_hash(self.tools[0])}]}
        self.assertEqual(self.project(self.tools), expected)
        pretty = json.dumps(self.tools, indent=2).encode()
        self.assertEqual(generate_exposure(self.manifest, self.registry, self.raw),
                         generate_exposure(self.manifest, self.registry, pretty))

    def test_changed_name_description_or_schema_never_self_readmits(self):
        variants = []
        for key, value in (('name', 'notes.write'), ('description', 'Changed instructions'),
                           ('input_schema', {'type': 'object'})):
            changed = copy.deepcopy(self.tools)
            changed[0][key] = value
            variants.append(changed)
        for tools in variants:
            with self.subTest(tools=tools):
                self.assertEqual(self.project(tools)['tools'], [])

    def test_unknown_and_deferred_tools_are_absent(self):
        observed = copy.deepcopy(self.tools)
        for name in ('unregistered.send', 'voice.call', 'host.write'):
            observed.append({'name': name, 'description': 'Synthetic', 'input_schema': {}})
        self.assertEqual(self.project(observed)['tools'], self.project(self.tools)['tools'])
        self.assertEqual(self.project([])['tools'], [])
        self.assertEqual(self.project(list(reversed(observed))), self.project(observed))

    def test_caller_annotations_cannot_supply_admission_or_tier(self):
        for key, value in (('approved', True), ('tier', 'read'),
                           ('definition_hash', dict(TOOL_DEFINITION_PINS)['notes.read'])):
            changed = copy.deepcopy(self.tools)
            changed[0][key] = value
            with self.assertRaises(RegistryError):
                self.project(changed)

    def test_changed_registry_or_manifest_refused_before_exposure(self):
        for manifest, registry in ((b'{}', self.registry),
                                    (self.manifest, b'[]')):
            with self.assertRaises(RegistryError):
                generate_exposure(manifest, registry, self.raw)

    def test_duplicate_and_malformed_observations_fail_closed(self):
        variants = [b'null', b'{}', b'', b'x' * 65537, b'\xff',
                    b'[{"name":"a","name":"b","description":"x","input_schema":{}}]',
                    b'[{"name":"a","description":"x","input_schema":{"x":NaN}}]',
                    encode(self.tools * 2)]
        for raw in variants:
            with self.subTest(raw=raw[:80]), self.assertRaises(RegistryError):
                generate_exposure(self.manifest, self.registry, raw)

    def test_invalid_field_types_and_hash_input_fail_closed(self):
        for key, value in (('name', None), ('description', []), ('input_schema', [])):
            changed = copy.deepcopy(self.tools)
            changed[0][key] = value
            with self.assertRaises(RegistryError):
                self.project(changed)
        for definition in (None, [], {'name': 'notes.read'},
                           {'name': 'a', 'description': 'x', 'input_schema': {'bad': float('inf')}}):
            with self.assertRaises(RegistryError):
                definition_hash(definition)


if __name__ == '__main__':
    unittest.main()
