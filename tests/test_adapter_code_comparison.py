"""C2 regression: imported code is compared structurally, not marshal bytes.

Adopted from Dolores's proposal branch dolores/c2-code-comparison-proposal (a9695ba)."""
import importlib.util
import sys
from pathlib import Path
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runtime.effects import adapter_artifact_digest, AdapterVerificationError


SOURCE = 'def perform(effect):\n    return {"ok": True, "name": "perform"}\n'


class AdapterCodeComparison(unittest.TestCase):
    def load(self, path):
        spec = importlib.util.spec_from_file_location('comparison_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.perform

    def test_source_and_cached_import_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'adapter.py'
            path.write_text(SOURCE)
            original = self.load(path)
            digest = adapter_artifact_digest(original)
            self.assertEqual(adapter_artifact_digest(self.load(path)), digest)

    def test_memory_code_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'adapter.py'
            path.write_text(SOURCE)
            original = self.load(path)
            constants = tuple('changed' if value == 'perform' else value
                              for value in original.__code__.co_consts)
            self.assertNotEqual(constants, original.__code__.co_consts)
            original.__code__ = original.__code__.replace(co_consts=constants)
            with self.assertRaises(AdapterVerificationError):
                adapter_artifact_digest(original)


if __name__ == '__main__':
    unittest.main()
