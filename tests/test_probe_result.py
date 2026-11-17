import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import probe_result as pr  # noqa: E402


def out(results):
    return json.dumps({"auth": {"probes": {"results": results}}})


class Probe(unittest.TestCase):
    def test_all_answered_is_silent(self):
        self.assertEqual(pr.verdict(out([{"model": "m", "status": "ok", "error": None}])), "")

    def test_a_refusal_names_the_model_and_reason_and_masks_keys(self):
        v = pr.verdict(out([{"model": "openrouter/x", "status": "auth", "error": "401 bad key sk-or-v1-abc123"},
                            {"model": "openrouter/y", "status": "ok"}]))
        self.assertEqual(v, "REFUSED: openrouter/x: auth (401 bad key sk-or-…)")

    def test_log_lines_around_the_json_are_ignored(self):
        raw = "\x1b[33m[state/agent-db]\x1b[39m receipt {written}\n" + out([{"model": "m", "error": "402 no credit"}]) + "\n(node:1) x\n"
        self.assertEqual(pr.verdict(raw), "REFUSED: m: None (402 no credit)")

    def test_openclaws_own_error_is_unclear_not_a_refusal(self):
        # seen on the real host: the check refuses to run while a gateway is up
        raw = json.dumps({"ok": False, "error": {"type": "cli_error", "message": "A Gateway is running for this state directory"}})
        self.assertEqual(pr.verdict(raw), "UNCLEAR: A Gateway is running for this state directory")
        self.assertEqual(pr.verdict("Gateway is running, stop it first\n"), "UNCLEAR: Gateway is running, stop it first")
        self.assertEqual(pr.verdict(""), "UNCLEAR: the check printed nothing")
        self.assertTrue(pr.verdict(json.dumps({"auth": {"probes": {"results": []}}})).startswith("UNCLEAR"))
        r = subprocess.run([sys.executable, str(ROOT / "install/probe_result.py")], input="not json",
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "UNCLEAR: not json")

    def test_the_installer_stops_only_on_a_refusal_and_checks_with_the_gateway_stopped(self):
        boot = (ROOT / "install/bootstrap.sh").read_text()
        self.assertIn('REFUSED:*) die', boot)
        self.assertLess(boot.index("Checking that OpenRouter accepts"), boot.index('say "Starting the gateway"'))
        self.assertLess(boot.index("paste-api-key"), boot.index("Checking that OpenRouter accepts"))

    def test_the_policy_uses_v41_flash(self):
        pol = json.loads((ROOT / "overlay/jev/policy.json").read_text())
        self.assertEqual(pol["classes"]["chat"]["model"], "openrouter/deepseek/deepseek-v4.1-flash")


if __name__ == "__main__":
    unittest.main()
