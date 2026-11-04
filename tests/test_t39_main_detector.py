"""T39 acceptance test 1 (detector half): scratch repositories only."""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from t39.main_detector import release_blocked, run_detector
from t39.receipt_registry import Facts, FileAnchor, ReceiptRegistry

NOW = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
N = iter(f"{i:064x}" for i in range(1, 10_000))


class Detector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.repo = d / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@e.invalid")
        self.git("config", "user.name", "T")
        self.commit_file("base")
        self.baseline = self.head()
        self.reg_path, self.anchor = d / "r.db", FileAnchor(d / "a.json")
        ReceiptRegistry.initialize(self.reg_path, self.anchor, "test", NOW).close()
        self.checkpoint, self.block = d / "checkpoint.json", d / "BLOCK"
        self.alerts = []

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def head(self, ref="HEAD"):
        return self.git("rev-parse", ref)

    def commit_file(self, text):
        (self.repo / "f.txt").write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", text)
        return self.head()

    def issue(self, commit):
        reg = ReceiptRegistry(self.reg_path, self.anchor)
        reg.issue(Facts(commit=commit, tree=self.head(f"{commit}^{{tree}}"),
                        receipt_hash=next(N), nonces=(next(N), next(N)),
                        agents={"author": "dolores", "reviewer": "claude"},
                        key_fingerprints={"author": "SHA256:a", "reviewer": "SHA256:b"},
                        verifier_version="v1"), "ci", NOW)
        reg.close()

    def run_it(self, git_dir=None):
        return run_detector(git_dir=git_dir or self.repo / ".git", branch="main",
                            baseline=self.baseline, checkpoint_path=self.checkpoint,
                            block_path=self.block,
                            registry_open=lambda: ReceiptRegistry(self.reg_path, self.anchor),
                            alert=self.alerts.append, now=NOW)

    def test_fast_forward_to_receipted_commit_is_clean(self):
        c = self.commit_file("reviewed")
        self.issue(c)
        r = self.run_it()
        self.assertTrue(r.clean)
        self.assertFalse(release_blocked(self.block))
        self.assertEqual(json.loads(self.checkpoint.read_text())["last_clean"], c)

    def test_direct_push_is_incident_blocks_release_and_plans_revert(self):
        self.issue(self.commit_file("reviewed"))
        bad = self.commit_file("direct push")
        r = self.run_it()
        self.assertEqual(r.unreceipted, [bad])
        self.assertEqual(r.revert_plan, [bad])
        self.assertTrue(release_blocked(self.block))
        self.assertIn("SECURITY INCIDENT", self.alerts[0])
        r2 = self.run_it()                      # keeps reporting until resolved
        self.assertEqual(r2.unreceipted, [bad])

    def test_merge_commit_with_reviewed_tree_is_clean(self):
        self.git("checkout", "-qb", "feature")
        feat = self.commit_file("feature work")
        self.issue(feat)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
        self.assertTrue(self.run_it().clean)

    def test_merge_commit_with_changed_tree_is_incident(self):
        self.git("checkout", "-qb", "feature")
        feat = self.commit_file("feature work")
        self.issue(feat)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "--no-commit", "feature")
        (self.repo / "extra.txt").write_text("sneaked in")
        self.git("add", ".")
        self.git("commit", "-qm", "merge plus extra")
        self.assertEqual(self.run_it().unreceipted, [self.head()])

    def test_unreviewed_intermediate_commits_on_first_parent_are_incidents(self):
        a = self.commit_file("a")
        b = self.commit_file("b")
        self.issue(b)                              # only the tip was reviewed
        self.assertEqual(self.run_it().unreceipted, [a])

    def test_history_rewrite_under_checkpoint_fails_loud(self):
        self.issue(self.commit_file("reviewed"))
        self.assertTrue(self.run_it().clean)
        self.git("reset", "-q", "--hard", self.baseline)
        self.commit_file("rewritten")
        r = self.run_it()
        self.assertIsNotNone(r.error)
        self.assertTrue(release_blocked(self.block))

    def test_registry_unavailable_fails_loud(self):
        self.commit_file("x")
        self.anchor.path.unlink()
        r = self.run_it()
        self.assertIsNotNone(r.error)
        self.assertFalse(r.clean)
        self.assertTrue(release_blocked(self.block))

    def test_wrong_git_dir_is_refused_not_walked_up(self):
        sub = self.repo / "notrepo"
        sub.mkdir()
        r = self.run_it(git_dir=sub)
        self.assertIsNotNone(r.error)

    def test_alert_still_sent_when_block_cannot_be_written(self):
        bad = self.commit_file("direct push")
        self.block = Path(self.tmp.name) / "missing-dir" / "BLOCK"
        r = self.run_it()
        self.assertEqual(r.unreceipted, [bad])
        self.assertIsNotNone(r.block_error)
        self.assertIn("RELEASE BLOCK NOT PERSISTED", self.alerts[0])

    def test_failure_alert_sent_when_block_cannot_be_written(self):
        self.anchor.path.unlink()
        self.block = Path(self.tmp.name) / "missing-dir" / "BLOCK"
        r = self.run_it()
        self.assertIsNotNone(r.error)
        self.assertIsNotNone(r.block_error)
        self.assertIn("FAILED", self.alerts[0])

    def test_inherited_git_env_cannot_redirect(self):
        self.issue(self.commit_file("reviewed"))
        old = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = "/nonexistent"
        try:
            self.assertTrue(self.run_it().clean)
        finally:
            if old is None:
                os.environ.pop("GIT_DIR")
            else:
                os.environ["GIT_DIR"] = old


if __name__ == "__main__":
    unittest.main()
