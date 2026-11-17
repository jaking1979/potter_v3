"""The guardrails plugin: run its node tests and check the manifest."""
import json
import subprocess
import unittest
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "overlay/plugins/potter-guardrails"


class Guardrails(unittest.TestCase):
    def test_node_tests_pass(self):
        r = subprocess.run(["node", "--test", "guards.test.mjs"], cwd=D, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-500:])
        self.assertIn("fail 0", r.stdout)
        self.assertNotIn("pass 0", r.stdout)

    def test_manifest_and_package_agree_and_nothing_personal_is_hard_coded(self):
        m = json.loads((D / "openclaw.plugin.json").read_text())
        p = json.loads((D / "package.json").read_text())
        self.assertEqual((m["id"], p["name"]), ("potter-guardrails", "potter-guardrails"))
        self.assertEqual(m["version"], p["version"])
        self.assertIn("excludedIds", m["configSchema"]["properties"])
        src = (D / "guards.js").read_text() + (D / "index.js").read_text()
        self.assertNotIn("/home/", src)
        self.assertNotRegex(src, r"[0-9a-f]{32}")


if __name__ == "__main__":
    unittest.main()
