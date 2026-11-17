import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import settings_page as sp  # noqa: E402


class Apply(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.state = Path(self._t.name)
        self.calls = []

        def runner(cmd):
            if cmd[:3] == ["openclaw", "config", "patch"]:
                self.calls.append(("patch", json.loads(Path(cmd[-1]).read_text())))
            else:
                self.calls.append(("cmd", cmd))
            return 0
        self.runner = runner

    def test_app_env_is_private_replaces_and_restarts_the_app(self):
        self.assertEqual(sp.apply_secret("TOKEN_A", "v1", {"kind": "app_env", "app": "trader-bot"}, self.runner, self.state), "")
        self.assertEqual(sp.apply_secret("TOKEN_A", "v2", {"kind": "app_env", "app": "trader-bot"}, self.runner, self.state), "")
        f = self.state / "env/trader-bot.env"
        self.assertEqual(f.read_text(), "TOKEN_A=v2\n")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(f.parent.stat().st_mode), 0o700)
        self.assertEqual(self.calls[-1], ("cmd", ["systemctl", "try-restart", "potter-app-trader-bot.service"]))
        self.assertTrue(sp.secret_status("TOKEN_A", {"kind": "app_env", "app": "trader-bot"}, self.state))
        self.assertFalse(sp.secret_status("OTHER", {"kind": "app_env", "app": "trader-bot"}, self.state))

    def test_mcp_and_global_targets_patch_openclaw_and_clean_up_the_patch_file(self):
        sp.apply_secret("RH_KEY", "abc", {"kind": "mcp_env", "server": "brokerage"}, self.runner, self.state)
        sp.apply_secret("RH_TOKEN", "tok", {"kind": "mcp_header", "server": "brokerage", "header": "Authorization",
                                            "prefix": "Bearer "}, self.runner, self.state)
        sp.apply_secret("SOME_KEY", "z", {"kind": "openclaw_env"}, self.runner, self.state)
        self.assertEqual(self.calls[0][1], {"mcp": {"servers": {"brokerage": {"env": {"RH_KEY": "abc"}}}}})
        self.assertEqual(self.calls[1][1]["mcp"]["servers"]["brokerage"]["headers"], {"Authorization": "Bearer tok"})
        self.assertEqual(self.calls[2][1], {"env": {"SOME_KEY": "z"}})
        self.assertEqual([p for p in self.state.iterdir() if p.name.startswith(".patch")], [])

    def test_bad_inputs_are_refused_without_echoing_the_value(self):
        t = {"kind": "app_env", "app": "trader-bot"}
        S = "SENTINEL9x"
        for name, val, tgt in (("lower", S, t), ("OK_NAME", "", t), ("OK_NAME", S + "\nmore", t), ("OK_NAME", S * 600, t),
                               ("OK_NAME", S, {"kind": "app_env", "app": "../x"}),
                               ("OK_NAME", S, {"kind": "mcp_header", "server": "s", "header": "bad header"}),
                               ("OK_NAME", S, {"kind": "nope"})):
            err = sp.apply_secret(name, val, tgt, self.runner, self.state)
            self.assertTrue(err, (name, tgt))
            self.assertNotIn("SENTINEL", err)
        self.assertEqual(self.calls, [])

    def test_a_rejected_patch_is_reported(self):
        err = sp.apply_secret("K_X", "v", {"kind": "openclaw_env"}, lambda c: 1, self.state)
        self.assertIn("rejected", err)


class Auth(unittest.TestCase):
    def test_login_lockout_and_session_expiry(self):
        now = [1000.0]
        a = sp.Auth("correct horse", clock=lambda: now[0])
        for _ in range(sp.MAX_FAILS):
            self.assertIsNone(a.login("wrong"))
        self.assertIsNone(a.login("correct horse"))             # locked out even with the right password
        now[0] += sp.LOCK_S + 1
        tok = a.login("correct horse")
        self.assertTrue(a.valid(tok))
        now[0] += sp.SESSION_S + 1
        self.assertFalse(a.valid(tok))
        self.assertFalse(a.valid(""))
        self.assertFalse(a.valid("made-up"))


class Server(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.state = Path(self._t.name)
        (self.state / "handover.json").write_text(json.dumps({"secrets_needed": [
            {"name": "BROKER_TOKEN", "used_by": "trader-bot", "how_to_get": "From your Brokerage developer page."}]}))
        self.calls = []
        self.auth = sp.Auth("pw-123456")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), sp.make_handler(self.auth, self.state, lambda c: self.calls.append(c) or 0))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)

    def curl(self, *args):
        out = subprocess.run(["curl", "-sS", "-i", "-m", "10", *args], capture_output=True, check=True).stdout.decode()
        head, _, body = out.partition("\r\n\r\n")
        return int(head.split()[1]), head, body

    def login(self):
        s, head, _ = self.curl("-X", "POST", f"http://127.0.0.1:{self.port}/login", "-d", "pw=pw-123456")
        self.assertEqual(s, 303)
        return [ln.split(": ", 1)[1].split(";")[0] for ln in head.split("\r\n") if ln.lower().startswith("set-cookie")][0]

    def test_page_requires_login_and_wrong_password_is_refused(self):
        s, _, body = self.curl(f"http://127.0.0.1:{self.port}/")
        self.assertIn("Potter password", body)
        self.assertNotIn("BROKER_TOKEN", body)
        s, _, _ = self.curl("-X", "POST", f"http://127.0.0.1:{self.port}/login", "-d", "pw=nope")
        self.assertEqual(s, 403)
        s, _, _ = self.curl("-X", "POST", f"http://127.0.0.1:{self.port}/save", "-d", "BROKER_TOKEN=x")
        self.assertEqual(s, 403)
        self.assertFalse((self.state / "env").exists())

    def test_signed_in_user_saves_a_secret_that_is_never_shown_back(self):
        cookie = self.login()
        _, _, body = self.curl("-H", f"Cookie: {cookie}", f"http://127.0.0.1:{self.port}/")
        self.assertIn("BROKER_TOKEN", body)
        self.assertIn("not set", body)
        s, _, body = self.curl("-H", f"Cookie: {cookie}", "-X", "POST", f"http://127.0.0.1:{self.port}/save",
                               "-d", "BROKER_TOKEN=supersecretvalue123")
        self.assertIn("saved", body)
        self.assertNotIn("supersecretvalue123", body)
        _, _, body = self.curl("-H", f"Cookie: {cookie}", f"http://127.0.0.1:{self.port}/")
        self.assertIn("(set;", body)
        self.assertNotIn("supersecretvalue123", body)
        self.assertEqual((self.state / "env/trader-bot.env").read_text(), "BROKER_TOKEN=supersecretvalue123\n")

    def test_unknown_names_are_ignored(self):
        cookie = self.login()
        self.curl("-H", f"Cookie: {cookie}", "-X", "POST", f"http://127.0.0.1:{self.port}/save", "-d", "EVIL_NAME=1")
        self.assertFalse((self.state / "env").exists())


if __name__ == "__main__":
    unittest.main()
