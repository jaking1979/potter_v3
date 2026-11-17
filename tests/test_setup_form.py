"""The one-time setup web page: validation, auth, one-shot behavior, secret handling."""
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import setup_form as sf  # noqa: E402

GOOD = {"OWNER_NAME": "Josh", "TIMEZONE": "America/New_York", "BRIEF_TIME": "08:30",
        "OWNER_TELEGRAM_ID": "8411191583", "TELEGRAM_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAA",
        "OPENROUTER_KEY": "sk-or-v1-abcdefghijklmnop1234", "COMPOSIO_MCP_URL": "", "AGENTMAIL_API_KEY": "",
        "OPENAI_VOICE_KEY": "", "HA_URL": "", "HA_TOKEN": "", "TAILSCALE_AUTHKEY": ""}


class Validate(unittest.TestCase):
    def test_good_input_passes(self):
        self.assertEqual(sf.validate(GOOD), {})

    def test_each_bad_field_is_reported_without_echoing_the_value(self):
        bad = {**GOOD, "OWNER_NAME": "<script>", "TIMEZONE": "Mars/Base", "BRIEF_TIME": "25:00",
               "OWNER_TELEGRAM_ID": "12", "TELEGRAM_TOKEN": "SECRETSECRET", "OPENROUTER_KEY": "nope",
               "COMPOSIO_MCP_URL": "http://x", "AGENTMAIL_API_KEY": "x", "TAILSCALE_AUTHKEY": "abc",
               "OPENAI_VOICE_KEY": "pk-1", "HA_URL": "ftp://x", "HA_TOKEN": "t"}
        err = sf.validate(bad)
        self.assertEqual(set(err), set(bad))
        self.assertNotIn("SECRETSECRET", " ".join(err.values()))

    def test_required_vs_optional(self):
        err = sf.validate({k: "" for k in GOOD})
        self.assertEqual(set(err), {"OWNER_NAME", "TIMEZONE", "BRIEF_TIME", "OWNER_TELEGRAM_ID",
                                    "TELEGRAM_TOKEN", "OPENROUTER_KEY"})

    def test_home_assistant_needs_both_or_neither(self):
        self.assertEqual(sf.validate({**GOOD, "HA_URL": "http://homeassistant.local:8123", "HA_TOKEN": "a" * 30}), {})
        self.assertIn("HA_TOKEN", sf.validate({**GOOD, "HA_URL": "http://homeassistant.local:8123"}))
        self.assertIn("HA_URL", sf.validate({**GOOD, "HA_TOKEN": "a" * 30}))

    def test_voice_key_shape(self):
        self.assertEqual(sf.validate({**GOOD, "OPENAI_VOICE_KEY": "sk-" + "a" * 30}), {})
        self.assertIn("OPENAI_VOICE_KEY", sf.validate({**GOOD, "OPENAI_VOICE_KEY": "nope"}))

    def test_env_mapping_prefix_and_strip(self):
        env = sf.to_env({**GOOD, "OWNER_NAME": " Josh "})
        self.assertEqual(env["POTTER_OWNER_NAME"], "Josh")
        self.assertEqual(set(env), {f"POTTER_{k}" for k in GOOD})

    def test_form_never_prefills_secrets_and_escapes_html(self):
        page = sf.render_form("tok", {}, {**GOOD, "OWNER_NAME": '"><b>x'})
        self.assertNotIn(GOOD["TELEGRAM_TOKEN"], page)
        self.assertNotIn('"><b>x', page)
        self.assertIn('type="password"', page)


class Server(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.out = Path(self._t.name) / "env.txt"
        script = Path(self._t.name) / "fake_bootstrap.sh"
        script.write_text(f'#!/bin/bash\nenv | grep ^POTTER_ | sort > {self.out}\n'
                          'echo "starting"; echo "token is $POTTER_TELEGRAM_TOKEN"; echo "done"\n')
        script.chmod(0o755)
        self.note = Path(self._t.name) / "note.txt"
        self.note.write_text("UI password: hunter2")
        os.environ["POTTER_FINAL_NOTE"] = str(self.note)
        self.addCleanup(os.environ.pop, "POTTER_FINAL_NOTE", None)
        self.token = "tok123"
        self.state = sf.State(self.token, [str(script)])
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), sf.make_handler(self.state))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None):
        """HTTP via curl so the test imports no network client module."""
        cmd = ["curl", "-sS", "-i", "-m", "10", "-X", method, f"http://127.0.0.1:{self.port}{path}"]
        if body is not None:
            cmd += ["-H", "Content-Type: application/x-www-form-urlencoded", "--data-binary", body]
        out = subprocess.run(cmd, capture_output=True, check=True).stdout.decode()
        head, _, text = out.partition("\r\n\r\n")
        lines = head.split("\r\n")
        headers = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}

        class R:
            def getheader(self, k):
                return headers.get(k.lower())
        return int(lines[0].split()[1]), text, R()

    def test_wrong_or_missing_token_is_forbidden(self):
        self.assertEqual(self.req("GET", "/")[0], 403)
        self.assertEqual(self.req("GET", "/?t=wrong")[0], 403)
        self.assertEqual(self.req("POST", "/submit?t=wrong", "a=b")[0], 403)
        self.assertEqual(self.req("GET", f"/status")[0], 403)

    def test_form_is_served_with_the_token_and_security_headers(self):
        s, body, r = self.req("GET", f"/?t={self.token}")
        self.assertEqual(s, 200)
        self.assertIn("Install Potter", body)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")

    def test_bad_submit_returns_errors_and_does_not_start(self):
        body = urllib.parse.urlencode({**GOOD, "BRIEF_TIME": "99:99"})
        s, page, _ = self.req("POST", f"/submit?t={self.token}", body)
        self.assertEqual(s, 400)
        self.assertIn("HH:MM", page)
        self.assertFalse(self.state.submitted)
        self.assertNotIn(GOOD["TELEGRAM_TOKEN"], page)

    def test_good_submit_runs_bootstrap_with_env_scrubs_secrets_and_is_one_shot(self):
        body = urllib.parse.urlencode(GOOD)
        s, _, r = self.req("POST", f"/submit?t={self.token}", body)
        self.assertEqual((s, r.getheader("Location")), (303, f"/?t={self.token}"))
        for _ in range(100):
            if self.state.done:
                break
            time.sleep(0.05)
        self.assertTrue(self.state.ok)
        env = self.out.read_text()
        self.assertIn("POTTER_OWNER_TELEGRAM_ID=8411191583", env)
        import json
        _, status, _ = self.req("GET", f"/status?t={self.token}")
        j = json.loads(status)
        self.assertNotIn(GOOD["TELEGRAM_TOKEN"], j["log"])
        self.assertIn("••••", j["log"])
        self.assertIn("hunter2", j["final"])
        self.assertEqual(self.req("POST", f"/submit?t={self.token}", body)[0], 403)   # second submit refused

    def post_multipart(self, fields, filename=None, filedata=None):
        cmd = ["curl", "-sS", "-i", "-m", "20", "-X", "POST", f"http://127.0.0.1:{self.port}/submit?t={self.token}"]
        for k, v in fields.items():
            cmd += ["-F", f"{k}={v}"]
        if filename:
            fp = Path(self._t.name) / filename
            fp.write_bytes(filedata)
            cmd += ["-F", f"V2_BUNDLE=@{fp}"]
        out = subprocess.run(cmd, capture_output=True, check=True).stdout.decode()
        head, _, text = out.partition("\r\n\r\n")
        return int(head.split("\r\n")[0].split()[1]), text

    def good_bundle(self):
        import io
        import json
        import zipfile
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("manifest.json", json.dumps({"schema": "potter.v2-export/1"}))
        return b.getvalue()

    def test_good_v2_bundle_upload_is_saved_private_and_passed_on(self):
        sf.BUNDLE_PATH = Path(self._t.name) / "saved.zip"
        s, _ = self.post_multipart(GOOD, "bundle.zip", self.good_bundle())
        self.assertEqual(s, 303)
        for _ in range(100):
            if self.state.done:
                break
            time.sleep(0.05)
        self.assertEqual(sf.BUNDLE_PATH.read_bytes(), self.good_bundle())
        self.assertEqual(stat.S_IMODE(sf.BUNDLE_PATH.stat().st_mode), 0o600)
        self.assertIn("POTTER_V2_BUNDLE=" + str(sf.BUNDLE_PATH), self.out.read_text())

    def test_bad_v2_bundle_is_rejected_before_anything_runs(self):
        sf.BUNDLE_PATH = Path(self._t.name) / "saved.zip"
        s, page = self.post_multipart(GOOD, "bundle.zip", b"not a zip")
        self.assertEqual(s, 400)
        self.assertIn("not a usable v2 export", page)
        self.assertFalse(self.state.submitted)
        self.assertFalse(sf.BUNDLE_PATH.exists())

    def test_multipart_without_a_file_still_installs(self):
        sf.BUNDLE_PATH = Path(self._t.name) / "saved.zip"
        s, _ = self.post_multipart(GOOD)
        self.assertEqual(s, 303)
        self.assertNotIn("POTTER_V2_BUNDLE", sf.to_env(GOOD))

    def test_expired_page_is_forbidden(self):
        self.state.started = time.time() - sf.LIFETIME_S - 1
        self.assertEqual(self.req("GET", f"/?t={self.token}")[0], 403)


if __name__ == "__main__":
    unittest.main()
