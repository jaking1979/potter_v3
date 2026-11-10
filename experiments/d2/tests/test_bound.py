import copy
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from registry import bound
from registry.deferred import RegistryError, serialize
from registry.synthetic_authority import EXPECTED_DIGEST

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'synthetic-manifest.json'


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


class BoundRegistryTests(unittest.TestCase):
    def setUp(self):
        self.source = FIXTURE.read_bytes()
        self.manifest = json.loads(self.source)
        self.generated = serialize(bound.generate_bound(self.source))

    def test_fixture_matches_external_pin(self):
        self.assertEqual(sha256(self.source).hexdigest(), EXPECTED_DIGEST)
        self.assertEqual(bound.verify_bound(self.source, self.generated), sha256(self.generated).hexdigest())

    def test_deterministic_generation(self):
        self.assertEqual(self.generated, serialize(bound.generate_bound(self.source)))
        self.assertEqual(self.generated, b'[{"class":"host-write","enabled":false,"name":"host.write"},{"class":"read","enabled":true,"name":"notes.read"},{"class":"voice","enabled":false,"name":"voice.call"}]')

    def test_lookup_only_known_enabled_synthetic_capability(self):
        self.assertTrue(bound.lookup_offline(self.source, self.generated, 'notes.read'))
        for name in ('voice.call', 'host.write', 'unknown', None):
            self.assertFalse(bound.lookup_offline(self.source, self.generated, name))

    def test_omissions_changes_extras_cannot_replace_external_pin(self):
        variants = []
        for key in ('classes', 'capabilities'):
            m = copy.deepcopy(self.manifest)
            m[key].pop()
            variants.append(m)
        for key, value in (('schema', 'other'), ('version', 'fixture-2'), ('digest', EXPECTED_DIGEST)):
            m = copy.deepcopy(self.manifest)
            m[key] = value
            variants.append(m)
        m = copy.deepcopy(self.manifest)
        m['capabilities'][1]['enabled'] = True
        variants.append(m)
        m = copy.deepcopy(self.manifest)
        m['classes'].append({'name': 'evil', 'deferred': False})
        variants.append(m)
        for m in variants:
            with self.subTest(manifest=m), self.assertRaisesRegex(RegistryError, 'authority mismatch'):
                bound.generate_bound(encode(m))
            self.assertFalse(bound.lookup_offline(encode(m), self.generated, 'notes.read'))

    def test_self_supplied_hash_and_promotion_flags_rejected(self):
        m = copy.deepcopy(self.manifest)
        m['classes'].pop()
        m['expected_digest'] = sha256(encode(m)).hexdigest()
        m['approved'] = True
        with self.assertRaises(RegistryError):
            bound.generate_bound(encode(m))
        with self.assertRaises(TypeError):
            bound.generate_bound(self.source, approved=True)

    def test_registry_omission_extra_disabled_change_and_order_drift(self):
        rows = json.loads(self.generated)
        variants = [rows[:-1], rows + [{'name': 'evil', 'class': 'read', 'enabled': True}], list(reversed(rows))]
        changed = copy.deepcopy(rows)
        changed[0]['enabled'] = True
        variants.append(changed)
        for rows in variants:
            bad = json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()
            with self.subTest(rows=rows), self.assertRaises(RegistryError):
                bound.verify_bound(self.source, bad)
            self.assertFalse(bound.lookup_offline(self.source, bad, 'notes.read'))

    def test_invalid_bytes_fail_closed(self):
        for source in (None, bytearray(self.source), '', b'', b'{}', b'x' * 65537):
            with self.subTest(source_type=type(source)), self.assertRaises(RegistryError):
                bound.generate_bound(source)
            self.assertFalse(bound.lookup_offline(source, self.generated, 'notes.read'))
        self.assertFalse(bound.lookup_offline(self.source, None, 'notes.read'))

    def assert_invalid_trusted_source(self, raw):
        # Defense in depth: simulate a reviewed trust-config update pinning bad
        # bytes. This mock is NOT a public API or a runtime source of authority.
        with patch.object(bound, 'EXPECTED_DIGEST', sha256(raw).hexdigest()):
            with self.assertRaises(RegistryError):
                bound.generate_bound(raw)
            self.assertFalse(bound.lookup_offline(raw, self.generated, 'notes.read'))

    def test_repin_does_not_bypass_inventory_and_disabled_invariants(self):
        variants = []
        for key in ('classes', 'capabilities'):
            m = copy.deepcopy(self.manifest)
            m[key].pop()
            variants.append(m)
        for field, value in (('name', 'unknown'), ('deferred', False), ('deferred', 1)):
            m = copy.deepcopy(self.manifest)
            m['classes'][1][field] = value
            variants.append(m)
        m = copy.deepcopy(self.manifest)
        m['classes'].append(copy.deepcopy(m['classes'][0]))
        variants.append(m)
        m = copy.deepcopy(self.manifest)
        m['capabilities'][1]['enabled'] = True
        variants.append(m)
        m = copy.deepcopy(self.manifest)
        m['capabilities'][0]['class'] = 'unknown'
        variants.append(m)
        for m in variants:
            with self.subTest(manifest=m):
                self.assert_invalid_trusted_source(encode(m))

    def test_repin_does_not_bypass_schema_version_and_strict_json(self):
        variants = [b'null', b'[]', b'{}', b'{', b'\xff', b'{"x":1,"x":2}']
        for key, value in (('schema', 'other'), ('version', 1), ('extra', True), ('classes', {})):
            m = copy.deepcopy(self.manifest)
            m[key] = value
            variants.append(encode(m))
        for raw in variants:
            with self.subTest(raw=raw):
                self.assert_invalid_trusted_source(raw)

    def test_repinned_nonfinite_numbers_fail_during_json_parsing(self):
        for token in (b'NaN', b'Infinity', b'-Infinity', b'1e400', b'-1e400'):
            raw = self.source.rstrip()[:-1] + b',"extra":' + token + b'}'
            with self.subTest(token=token), patch.object(bound, 'EXPECTED_DIGEST', sha256(raw).hexdigest()):
                with self.assertRaisesRegex(RegistryError, 'invalid manifest JSON'):
                    bound.generate_bound(raw)


if __name__ == '__main__':
    unittest.main()
