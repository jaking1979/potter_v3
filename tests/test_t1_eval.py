"""T1 evaluation (overlay/eval): node tests pass; installed with its wrapper; evaluation sessions block actions."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class T1(unittest.TestCase):
    def test_node_tests_pass(self):
        r = subprocess.run(["node", "--test"], cwd=ROOT / "overlay/eval", capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-300:])
        self.assertIn("fail 0", r.stdout)

    def test_installed_and_actions_blocked(self):
        for f in ("install/bootstrap.sh", "install/update.sh"):
            self.assertIn("/usr/local/bin/potter-t1", (ROOT / f).read_text(), f)
        idx = (ROOT / "overlay/plugins/potter-core/index.js").read_text()
        self.assertIn("isEvalSession(ctx) && cls.risk !== \"read\"", idx)
        self.assertIn("t1-eval-", (ROOT / "overlay/eval/lib.js").read_text())

    def test_bench_is_installed_sealed_and_fictional(self):
        for f in ("install/bootstrap.sh", "install/update.sh"):
            t = (ROOT / f).read_text()
            self.assertIn("/usr/local/bin/potter-bench", t, f)
            self.assertIn('"bench-v2":{"workspace":b+"/v2","brain":False}', t, f)
        idx = (ROOT / "overlay/plugins/potter-core/index.js").read_text()
        self.assertIn("sealedRefusal(event?.toolName, event?.params, SP.ws)", idx)
        self.assertIn("if (profiles[ctx?.agentId]) return;", idx)               # never the real house
        for f in (ROOT / "overlay/eval/bench").rglob("*"):
            if f.is_file():
                self.assertNotRegex(f.read_text(), r"(?i)\b(josh|christie|molly|edie|catskills|bay ridge)\b", f.name)

    def test_nothing_personal(self):
        for f in (ROOT / "overlay/eval").glob("*.js"):
            self.assertNotRegex(f.read_text(), r"(?i)\b(josh|christie|molly|edie)\b", f.name)


if __name__ == "__main__":
    unittest.main()
