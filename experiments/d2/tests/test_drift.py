import copy
import json
from pathlib import Path
import unittest

from registry.bound import generate_bound
from registry.deferred import RegistryError, serialize
from registry.drift import verify_observation_matches_fixture

ROOT = Path(__file__).resolve().parents[1]


class DriftCheckTests(unittest.TestCase):
    def setUp(self):
        self.manifest = (ROOT / 'fixtures/synthetic-manifest.json').read_bytes()
        self.registry = serialize(generate_bound(self.manifest))
        self.observed = json.loads((ROOT / 'fixtures/synthetic-tools.json').read_bytes())

    def check(self, rows):
        return verify_observation_matches_fixture(
            self.manifest, self.registry, json.dumps(rows).encode())

    def test_matching_observation_has_stable_digest(self):
        digest = self.check(self.observed)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, self.check(copy.deepcopy(self.observed)))

    def test_changed_missing_extra_and_deferred_refuse(self):
        changed = copy.deepcopy(self.observed)
        changed[0]['description'] = 'changed'
        for rows in (changed, [], self.observed + [
                {'name': 'other.tool', 'description': 'x', 'input_schema': {}}],
                self.observed + [{'name': 'voice.call', 'description': 'x', 'input_schema': {}}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(RegistryError, 'synthetic definition drift'):
                self.check(rows)

    def test_invalid_sources_refuse_without_echoing_observation(self):
        with self.assertRaises(RegistryError):
            verify_observation_matches_fixture(self.manifest, b'[]', b'[]')
        raw = b'[{"name":"a","name":"private","description":"secret","input_schema":{}}]'
        with self.assertRaises(RegistryError) as cm:
            verify_observation_matches_fixture(self.manifest, self.registry, raw)
        self.assertNotIn('secret', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
