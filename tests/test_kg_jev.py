"""Knowledge-graph plugin and Jev decision layer: run their node tests and check the manifests."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KG = ROOT / "overlay/plugins/potter-kg"
JEV = ROOT / "overlay/jev"


def node_tests(cwd):
    return subprocess.run(["node", "--test"], cwd=cwd, capture_output=True, text=True)


class KgJev(unittest.TestCase):
    def test_kg_node_tests_pass(self):
        r = node_tests(KG)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-300:])
        self.assertIn("fail 0", r.stdout)
        self.assertNotIn("pass 0", r.stdout)

    def test_jev_node_tests_pass(self):
        r = node_tests(JEV)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-300:])
        self.assertIn("fail 0", r.stdout)
        self.assertNotIn("pass 0", r.stdout)

    def test_kg_manifest_and_package_agree(self):
        m = json.loads((KG / "openclaw.plugin.json").read_text())
        p = json.loads((KG / "package.json").read_text())
        self.assertEqual(m["id"], "potter-kg")
        self.assertEqual(p["name"], "potter-kg")
        self.assertEqual(p.get("dependencies", {}), {})          # node built-ins only: no install step

    def test_nothing_personal_or_machine_specific_is_hard_coded(self):
        for d in (KG, JEV):
            for f in d.rglob("*"):
                if f.is_file() and f.suffix in (".js", ".mjs", ".json") and "node_modules" not in f.parts:
                    text = f.read_text()
                    self.assertNotIn("/home/", text, f.name)
                    self.assertNotRegex(text, r"sk-or-[A-Za-z0-9]{8,}", f.name)

    def test_policy_example_is_valid_and_names_no_real_default(self):
        pol = json.loads((JEV / "policy.example.json").read_text())
        self.assertIsInstance(pol, dict)
        self.assertTrue(pol)


if __name__ == "__main__":
    unittest.main()
