"""Behaviour tests for tools/nodeploy_guard.py (synthetic inputs only)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import nodeploy_guard as g  # noqa: E402

POLICY = g.load_policy()
# Built at runtime so this file is not itself a hit for the literal rule.
ALPHA = "Alpha" + " Bot"
HOST = "Opti" + "Plex"
SAFE_WORKFLOW = "name: t\non: [push]\npermissions:\n  contents: read\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python -m unittest\n"


def rules(path, text):
    return {f.rule for f in g.scan_text(path, text, POLICY)}


class SecretShapes(unittest.TestCase):
    def test_each_secret_shape_is_refused(self):
        cases = {
            "secret.private_key": "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
            "secret.aws_key_id": "AKIA" + "ABCDEFGHIJKLMNOP",
            "secret.aws_session_key_id": "ASIA" + "ABCDEFGHIJKLMNOP",
            "secret.github_token": "ghp_" + "a" * 36,
            "secret.slack_token": "xoxb-" + "1234567890-abcdef",
            "secret.telegram_bot_token": "123456789:" + "A" * 35,
            "secret.provider_api_key": "sk-ant-" + "a" * 40,
            "secret.assigned_credential": 'api_key = "' + "a1" * 12 + '"',
        }
        for rule, text in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule.replace("aws_session_key_id", "aws_key_id"), rules("src/x.py", text))

    def test_benign_text_is_clean(self):
        for text in ("token = None", "password = ''", "x = 'short'", "sk-short", "keyword = 'a'"):
            with self.subTest(text=text):
                self.assertEqual(rules("src/x.py", text), set())

    def test_secret_shapes_are_refused_even_in_docs(self):
        self.assertIn("secret.github_token", rules("README.md", "ghp_" + "a" * 36))

    def test_findings_never_echo_the_matched_text(self):
        secret = "ghp_" + "b" * 36
        finding = g.scan_text("src/x.py", f"x = '{secret}'", POLICY)[0]
        self.assertNotIn(secret, str(finding))


class References(unittest.TestCase):
    def test_forbidden_literals_refused_in_code_allowed_in_docs(self):
        for literal in (ALPHA, HOST, ALPHA.upper(), ALPHA.replace(" ", "_")):
            with self.subTest(literal=literal):
                self.assertIn("reference.forbidden_literal", rules("src/x.py", f"# uses {literal}"))
                self.assertNotIn("reference.forbidden_literal", rules("README.md", f"uses {literal}"))

    def test_private_address_refused_in_code(self):
        self.assertIn("reference.private_address", rules("src/x.py", "HOST = '192.168.1.20'"))
        self.assertIn("reference.private_address", rules("src/x.py", "HOST = '10.0.0.5'"))
        self.assertEqual(rules("src/x.py", "v = '1.2.3.4'"), set())


class Workflows(unittest.TestCase):
    wf = ".github/workflows/x.yml"

    def test_safe_workflow_is_clean(self):
        self.assertEqual(rules(self.wf, SAFE_WORKFLOW), set())

    def test_missing_permissions_refused(self):
        self.assertIn("workflow.missing_permissions", rules(self.wf, SAFE_WORKFLOW.replace("permissions:\n  contents: read\n", "")))

    def test_write_permission_refused(self):
        for text in ("permissions:\n  contents: write\n", "permissions: write-all\n"):
            with self.subTest(text=text):
                self.assertIn("workflow.write_permission", rules(self.wf, text + "jobs: {}\n"))

    def test_secret_and_environment_refused_but_github_token_allowed(self):
        self.assertIn("workflow.secret_reference", rules(self.wf, SAFE_WORKFLOW + "      - run: echo ${{ secrets.PROD_KEY }}\n"))
        self.assertNotIn("workflow.secret_reference", rules(self.wf, SAFE_WORKFLOW + "      - run: echo ${{ secrets.GITHUB_TOKEN }}\n"))
        self.assertIn("workflow.deployment_environment", rules(self.wf, SAFE_WORKFLOW + "    environment: production\n"))

    def test_deploy_commands_refused(self):
        for command in ("ssh user@host uptime", "rsync -a . server:/srv", "kubectl apply -f x", "docker push img",
                        "npm publish", "gh release create v1", "terraform apply", "scp a b:c"):
            with self.subTest(command=command):
                self.assertIn("workflow.deploy_command", rules(self.wf, SAFE_WORKFLOW + f"      - run: {command}\n"))

    def test_comment_text_is_not_a_command(self):
        self.assertEqual(rules(self.wf, SAFE_WORKFLOW + "      # we never deploy here\n"), set())

    def test_workflow_rules_only_apply_to_workflow_files(self):
        self.assertNotIn("workflow.missing_permissions", rules("docs/x.yml", "a: b\n"))


class TreeScan(unittest.TestCase):
    def test_tree_scan_flags_env_files_symlink_free_and_reports_location(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x = 1\n# note\nHOST = '192.168.0.9'\n")
            (root / ".env").write_text("A=1\n")
            (root / "bin.dat").write_bytes(b"\0\1\2" + ("ghp_" + "c" * 36).encode())
            found = g.scan_tree(root, POLICY, files=["a.py", ".env", "bin.dat"])
            self.assertIn(g.Finding("a.py", 3, "reference.private_address"), found)
            self.assertIn(g.Finding(".env", 0, "path.forbidden_file"), found)
            self.assertFalse([f for f in found if f.path == "bin.dat"])

    def test_oversized_file_is_a_finding_not_a_skip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "big.txt").write_bytes(b"a" * (g.MAX_FILE_BYTES + 1))
            self.assertEqual([f.rule for f in g.scan_tree(root, POLICY, files=["big.txt"])], ["file.too_large_to_scan"])

    def test_policy_must_be_well_formed(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "p.json"
            bad.write_text(json.dumps({"forbidden_literals": "x", "doc_allow_globs": [], "workflow_allow_deploy_words": []}))
            with self.assertRaises(ValueError):
                g.load_policy(bad)

    def test_this_repository_is_clean(self):
        self.assertEqual([str(f) for f in g.scan_tree(ROOT, POLICY)], [])

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            Path(d, "ok.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=d, check=True)
            ok = subprocess.run([sys.executable, str(ROOT / "tools/nodeploy_guard.py"), d], capture_output=True, text=True)
            self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "nodeploy_guard: clean"))
            Path(d, "bad.py").write_text("HOST = '10.1.2.3'\n")
            subprocess.run(["git", "add", "."], cwd=d, check=True)
            bad = subprocess.run([sys.executable, str(ROOT / "tools/nodeploy_guard.py"), d], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("bad.py:1: reference.private_address", bad.stdout)


if __name__ == "__main__":
    unittest.main()
