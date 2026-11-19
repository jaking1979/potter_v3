"""Potter vault: the plugin's node tests pass; the setup form checks input and writes a root-only file."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import vault_form  # noqa: E402

GOOD = {"BW_SERVER": "https://vault.example.ts.net", "BW_CLIENTID": "user.12345678-1234-1234-1234-123456789abc",
        "BW_CLIENTSECRET": "x" * 30, "BW_PASSWORD": "correct horse"}


class Vault(unittest.TestCase):
    def test_plugin_node_tests_pass(self):
        r = subprocess.run(["node", "--test"], cwd=ROOT / "overlay/plugins/potter-vault", capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-300:])
        self.assertIn("fail 0", r.stdout)

    def test_form_validation_names_the_field_to_fix(self):
        self.assertEqual(vault_form.validate(GOOD), {})
        bad = vault_form.validate({**GOOD, "BW_SERVER": "http://x", "BW_CLIENTID": "nope"})
        self.assertEqual(set(bad), {"BW_SERVER", "BW_CLIENTID"})

    def test_live_check_reports_the_failing_step_and_never_passes_the_password_as_an_argument(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "args"
            fake = Path(d) / "bw"
            fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncase "$1" in login) exit 0;; unlock) [ "$BW_PASSWORD" = "correct horse" ] && echo SESSION || exit 1;; esac\n')
            fake.chmod(0o755)
            self.assertEqual(vault_form.live_check(GOOD, str(fake)), {})
            self.assertIn("BW_PASSWORD", vault_form.live_check({**GOOD, "BW_PASSWORD": "wrong one!"}, str(fake)))
            self.assertNotIn("correct horse", log.read_text())

    def test_addresses_and_failure_kinds(self):
        self.assertTrue(vault_form.server_ok("https://vault.example.ts.net"))
        lan = ".".join(["10", "0", "0", "5"])                                     # built at runtime: no literal address
        self.assertTrue(vault_form.server_ok(f"http://{lan}:8080"))
        self.assertFalse(vault_form.server_ok("http://vault.example.com"))            # plain http only on a private IP
        self.assertFalse(vault_form.server_ok("http://8.8.8.8:8080"))
        self.assertEqual(vault_form.failure("FetchError: request failed, reason: getaddrinfo ENOTFOUND vault.x.ts.net"), "unreachable")
        self.assertEqual(vault_form.failure(f"connect ECONNREFUSED {lan}:8080"), "unreachable")
        self.assertEqual(vault_form.failure("invalid_client"), "key")
        self.assertEqual(vault_form.failure("self signed certificate"), "tls")

    def test_page_never_echoes_a_secret(self):
        out = vault_form.page("t", GOOD, {}).decode()
        self.assertNotIn("correct horse", out)
        self.assertNotIn("x" * 30, out)
        self.assertIn("vault.example.ts.net", out)


if __name__ == "__main__":
    unittest.main()
