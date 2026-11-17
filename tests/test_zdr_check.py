import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import zdr_check as z  # noqa: E402


class Zdr(unittest.TestCase):
    def test_models_come_from_the_policy_and_the_embedding(self):
        pol = {"classes": {"a": {"model": "openrouter/x/one", "fallbacks": ["openrouter/x/two", "ollama/local"]},
                           "j": {"model": "~typesafe/jev-latest", "fallbacks": []}}}
        self.assertEqual(z.models_in(pol, "e/embed"), ["x/one", "x/two", "e/embed"])

    def test_only_models_without_a_zdr_endpoint_are_named(self):
        rows = [{"model_id": "x/one"}, {"model_id": "e/embed"}]
        self.assertEqual(z.missing(["x/one", "x/two", "e/embed"], rows), ["x/two"])

    def test_cli_warns_and_never_fails(self):
        with tempfile.TemporaryDirectory() as d:
            pol, lst = Path(d) / "p.json", Path(d) / "l.json"
            pol.write_text(json.dumps({"classes": {"a": {"model": "openrouter/x/two", "fallbacks": []}}}))
            lst.write_text(json.dumps({"data": [{"model_id": "x/one"}]}))
            r = subprocess.run([sys.executable, str(ROOT / "install/zdr_check.py"), str(pol), "--list", str(lst)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            self.assertIn("x/two: no zero-data-retention provider", r.stdout)
            r = subprocess.run([sys.executable, str(ROOT / "install/zdr_check.py"), str(pol), "--list", "/nonexistent"],
                               capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_the_embedding_model_is_the_zdr_one(self):
        self.assertIn('"openai/text-embedding-3-small"', (ROOT / "install/memory_search.sh").read_text())
        self.assertIn("--embedding openai/text-embedding-3-small", (ROOT / "install/update.sh").read_text())


if __name__ == "__main__":
    unittest.main()
