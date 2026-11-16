"""Installer scripts: syntax, input validation, overlay rendering. No network, no OpenClaw needed."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOOD = dict(POTTER_OWNER_NAME="Josh", POTTER_TIMEZONE="America/New_York", POTTER_OWNER_TELEGRAM_ID="8411191583",
            POTTER_TELEGRAM_TOKEN="1:x", POTTER_OPENROUTER_KEY="k", POTTER_BRIEF_TIME="08:30")


def sh(script, **env):
    e = {**os.environ, **env}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=ROOT, env=e)


class Installer(unittest.TestCase):
    def test_every_script_parses(self):
        for f in sorted((ROOT / "install").glob("*.sh")):
            self.assertEqual(sh(f"bash -n {f}").returncode, 0, f.name)

    def test_form_accepts_good_input(self):
        r = sh(". install/lib.sh; collect_inputs; echo \"$OWNER_NAME $BRIEF_TIME\"", **GOOD)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "Josh 08:30"), r.stderr)

    def test_form_rejects_bad_input(self):
        for key, bad in (("POTTER_OWNER_TELEGRAM_ID", "12"), ("POTTER_BRIEF_TIME", "25:00"),
                         ("POTTER_TIMEZONE", "Mars/Base")):
            r = sh(". install/lib.sh; collect_inputs", **{**GOOD, key: bad})
            self.assertNotEqual(r.returncode, 0, key)

    def test_overlay_renders_and_has_no_leftover_placeholders(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "o.json5"
            r = sh(f". install/lib.sh; render_overlay overlay/config.base.json5 {out}", OWNER_NAME="Josh",
                   TIMEZONE="America/New_York", OWNER_TELEGRAM_ID="8411191583", SECRETS_DIR="/etc/potter/secrets")
            self.assertEqual(r.returncode, 0, r.stderr)
            text = out.read_text()
            self.assertNotIn("@@", text)
            self.assertIn("8411191583", text)

    def test_overlay_refuses_unfilled_placeholders(self):
        r = sh(". install/lib.sh; render_overlay overlay/config.base.json5 /dev/null",
               OWNER_NAME="Josh", TIMEZONE="America/New_York", SECRETS_DIR="/x")
        self.assertNotEqual(r.returncode, 0)

    def test_overlay_locks_the_agent_down(self):
        text = (ROOT / "overlay/config.base.json5").read_text()
        for needle in ('dmPolicy: "allowlist"', 'groupPolicy: "disabled"', '"gateway"', 'mode: "ask"',
                       "enabled: false", "telemetry"):
            self.assertIn(needle, text)

    def test_connect_skill_forbids_secrets_in_chat(self):
        text = (ROOT / "overlay/workspace/skills/connect-apps/SKILL.md").read_text()
        self.assertIn("Never ask for a secret in chat", text)
        self.assertIn("read-only", text)

    def test_delegated_changes_need_approval_in_overlay(self):
        self.assertIn("autoApproveChanges: false", (ROOT / "overlay/config.base.json5").read_text())

    def test_agentmail_starts_without_any_sending_tool(self):
        text = (ROOT / "install/bootstrap.sh").read_text()
        for needle in ('"*send*"', '"*reply*"', '"*forward*"', '"*delete*"', "toolFilter"):
            self.assertIn(needle, text)

    def test_no_secret_shapes_in_overlay(self):
        import re
        for p in (ROOT / "overlay").rglob("*"):
            if p.is_file():
                self.assertIsNone(re.search(r"sk-or-|\d{6,}:[A-Za-z0-9_-]{20,}", p.read_text()), p.name)


if __name__ == "__main__":
    unittest.main()
