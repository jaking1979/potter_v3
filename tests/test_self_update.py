"""potter-update: pull from the repository (fast-forward only), keep the previous code, then run update.sh."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install/self_update.sh"


def sh(*args, cwd=None, env=None):
    return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True)


@unittest.skipUnless(os.geteuid() == 0, "runs as root, like on the container")
class SelfUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        g = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@example.com", "HOME": str(self.tmp)}
        self.genv = {**os.environ, **g}
        sh("git", "init", "-q", "-b", "v3-core", cwd=self.origin, env=self.genv)
        self.commit("one")
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "old-copy.txt").write_text("copied without history")
        conf = self.tmp / "github"
        conf.write_text("REPO=owner/repo\nBRANCH=v3-core\n")
        secrets = self.tmp / "secrets"
        secrets.mkdir()
        (secrets / "github_deploy").write_text("not a real key")
        self.env = {**self.genv, "POTTER_SRC": str(self.src), "POTTER_SECRETS_DIR": str(secrets),
                    "POTTER_GITHUB_CONF": str(conf), "POTTER_GIT_URL": str(self.origin)}

    def commit(self, msg):
        inst = self.origin / "install"
        inst.mkdir(exist_ok=True)
        (inst / "update.sh").write_text(f'echo "update.sh ran: {msg} $*"\n')
        sh("git", "add", "-A", cwd=self.origin, env=self.genv)
        sh("git", "commit", "-q", "-m", msg, cwd=self.origin, env=self.genv)

    def run_update(self, *args):
        return sh("bash", str(SCRIPT), *args, env=self.env)

    def test_first_pull_replaces_the_copy_keeps_it_and_runs_the_new_update(self):
        r = self.run_update("--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Available:", r.stdout)
        self.assertTrue((self.src / "old-copy.txt").exists())              # --check changes nothing
        r = self.run_update("--profile", "x.zip")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("update.sh ran: one --profile x.zip", r.stdout)
        self.assertTrue((self.src / ".git").is_dir())
        self.assertTrue((self.tmp / "src.prev" / "old-copy.txt").exists())

    def test_later_pulls_fast_forward_list_commits_and_refuse_rewrites_and_hand_edits(self):
        self.run_update()
        r = self.run_update()
        self.assertIn("Already up to date", r.stdout)
        self.commit("two")
        r = self.run_update("--check")
        self.assertIn("two", r.stdout)
        self.assertNotIn("update.sh ran", r.stdout)
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("update.sh ran: two", r.stdout)
        # a hand edit in the installed code stops the update
        self.commit("three")
        (self.src / "install/update.sh").write_text("echo edited\n")
        r = self.run_update()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("edited by hand", r.stderr)
        sh("git", "checkout", "-q", "--", ".", cwd=self.src)
        # a rewritten branch is refused
        sh("git", "reset", "-q", "--hard", "HEAD~2", cwd=self.origin, env=self.genv)
        self.commit("rewritten")
        r = self.run_update()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rewritten", r.stderr)

    def test_not_set_up_says_so(self):
        env = {**self.env, "POTTER_GITHUB_CONF": str(self.tmp / "missing")}
        r = sh("bash", str(SCRIPT), env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not set up", r.stderr)

    def test_the_known_host_is_pinned_and_the_key_is_read_only_by_design(self):
        s = SCRIPT.read_text()
        self.assertIn("StrictHostKeyChecking=yes", s)
        self.assertIn("--ff-only", s)
        self.assertIn('Allow write access', s)


if __name__ == "__main__":
    unittest.main()
