"""Chief-of-staff loop (overlay/cos): node tests pass; installed by bootstrap and update; no personal data."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COS = ROOT / "overlay/cos"


class Cos(unittest.TestCase):
    def test_node_tests_pass(self):
        r = subprocess.run(["node", "--test"], cwd=COS, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-300:])
        self.assertIn("fail 0", r.stdout)
        self.assertNotIn("pass 0", r.stdout)

    def test_installed_with_its_wrapper_and_jobs(self):
        for f in ("install/bootstrap.sh", "install/update.sh"):
            s = (ROOT / f).read_text()
            self.assertIn('cp -r "$REPO/overlay/cos" "$POTTER_HOME/cos"', s, f)
            self.assertIn("/usr/local/bin/potter-cos-score", s, f)
        jobs = (ROOT / "install/jobs.json").read_text()
        self.assertIn('"potter-cos-score"', jobs)
        self.assertIn('"potter-week-review"', jobs)
        self.assertIn('"potter-mail-watch"', jobs)
        self.assertIn('"potter-travel-watch"', jobs)
        for f in ("install/bootstrap.sh", "install/update.sh"):
            t = (ROOT / f).read_text()
            self.assertIn("/usr/local/bin/potter-travel", t, f)
            self.assertIn('--travel-inbox "$WS/state/travel/travel-inbox.jsonl"', t, f)
        self.assertNotIn('"potter-mail-observe"', jobs)

    def test_nothing_personal(self):
        for f in COS.glob("*.js"):
            self.assertNotRegex(f.read_text(), r"(?i)\b(josh|christie|molly|edie)\b", f.name)
        self.assertNotRegex((COS / "score.mjs").read_text(), r"(?i)\b(josh|christie|molly|edie)\b")


if __name__ == "__main__":
    unittest.main()
