import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import probe_result as pr  # noqa: E402


def wrap(results):
    return {"auth": {"probes": {"results": results}}}


class Probe(unittest.TestCase):
    def test_all_answered_is_silent(self):
        self.assertEqual(pr.problems(wrap([{"model": "m", "status": "ok", "error": None}])), "")

    def test_refusals_name_the_model_and_the_providers_reason(self):
        out = pr.problems(wrap([{"model": "openrouter/x", "status": "auth", "error": "401 bad key"},
                                {"model": "openrouter/y", "status": "ok"}]))
        self.assertEqual(out, "openrouter/x: auth (401 bad key)")

    def test_nothing_checked_or_unreadable_is_a_problem(self):
        self.assertIn("no OpenRouter model", pr.problems(wrap([])))
        r = subprocess.run([sys.executable, str(ROOT / "install/probe_result.py")], input="not json",
                           capture_output=True, text=True)
        self.assertIn("nothing readable", r.stdout)

    def test_log_lines_around_the_json_are_ignored(self):
        raw = ('\x1b[33m[state/agent-db]\x1b[39m receipt {written}\n' + json.dumps(wrap([{"model": "m", "error": "402 no credit"}]))
               + "\n(node:1) DeprecationWarning: x\n")
        self.assertEqual(pr.problems(pr.parse(raw)), "m: None (402 no credit)")

    def test_the_policy_uses_v41_flash(self):
        pol = json.loads((ROOT / "overlay/jev/policy.json").read_text())
        self.assertEqual(pol["classes"]["chat"]["model"], "openrouter/deepseek/deepseek-v4.1-flash")
        self.assertIn("probe_result.py", (ROOT / "install/bootstrap.sh").read_text())


if __name__ == "__main__":
    unittest.main()
