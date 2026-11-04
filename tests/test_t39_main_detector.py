"""T39 acceptance test 1: detector and fail-closed deploy gate. Scratch repos only."""
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from t39.main_detector import release_allowed, run_detector
from t39.receipt_registry import Facts, FileAnchor, ReceiptRegistry

NOW = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
N = iter(f"{i:064x}" for i in range(1, 10_000))


class FlakyAnchor(FileAnchor):
    def __init__(self, path):
        super().__init__(path)
        self.fail = False

    def append(self, entry):
        if self.fail:
            raise OSError("anchor host unreachable")
        super().append(entry)


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
        self.reg_path, self.anchor = d / "r.db", FlakyAnchor(d / "a.jsonl")
        ReceiptRegistry.initialize(self.reg_path, self.anchor, "test", NOW).close()
        self.alerts = []

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def head(self, ref="HEAD"):
        return self.git("rev-parse", ref)

    def commit_file(self, text, name="f.txt"):
        (self.repo / name).write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", text)
        return self.head()

    def open_reg(self):
        return ReceiptRegistry(self.reg_path, self.anchor)

    def issue(self, commit):
        reg = self.open_reg()
        reg.issue(Facts(commit=commit, tree=self.head(f"{commit}^{{tree}}"),
                        receipt_hash=next(N), nonces=(next(N), next(N)),
                        agents={"author": "dolores", "reviewer": "claude"},
                        key_fingerprints={"author": "SHA256:a", "reviewer": "SHA256:b"},
                        verifier_version="v1"), "ci", NOW)
        reg.close()

    def run_it(self, git_dir=None, now=NOW):
        return run_detector(git_dir=git_dir or self.repo / ".git", branch="main",
                            baseline=self.baseline, registry_open=self.open_reg,
                            alert=self.alerts.append, now=now)

    def allowed(self, commit, now=NOW):
        return release_allowed(git_dir=self.repo / ".git", registry_open=self.open_reg,
                               branch="main", commit=commit, now=now)

    # --- clean paths and the deploy gate ------------------------------------

    def test_fast_forward_clean_then_deployable(self):
        c = self.commit_file("reviewed")
        self.issue(c)
        self.assertFalse(self.allowed(c))               # nothing cleared yet
        self.assertTrue(self.run_it().clean)
        self.assertTrue(self.allowed(c))

    def test_push_after_clearance_not_deployable_until_cleared(self):
        c = self.commit_file("reviewed")
        self.issue(c)
        self.run_it()
        later = self.commit_file("pushed after the run")
        self.assertFalse(self.allowed(later))           # never deployable between runs
        self.assertFalse(self.allowed(c))               # main moved: old clear is void

    def test_prior_clear_then_unrecorded_incident_stays_blocked(self):
        # Dolores review on 54cacc9: clear C, push unreceipted D, incident append fails.
        c = self.commit_file("reviewed")
        self.issue(c)
        self.assertTrue(self.run_it().clean)
        self.assertTrue(self.allowed(c))
        d = self.commit_file("direct push")
        self.anchor.fail = True
        r = self.run_it()
        self.assertEqual(r.unreceipted, [d])
        self.assertIn("NOT anchored", self.alerts[-1])
        self.assertFalse(self.allowed(c))               # without any recorded incident
        self.assertFalse(self.allowed(d))
        self.anchor.fail = False                        # append service recovers
        self.assertFalse(self.allowed(c))
        self.assertFalse(self.allowed(d))
        self.run_it()
        self.assertFalse(self.allowed(d))
        self.assertEqual(self.open_reg().last_detector_event("main")["kind"], "incident")

    def test_stale_or_concurrent_detector_write_is_refused(self):
        from t39.receipt_registry import RegistryUnavailable
        c = self.commit_file("reviewed")
        self.issue(c)
        reg = self.open_reg()
        stale_seq = None                                # a run that started before any result
        self.run_it()                                   # a newer run records 'clear'
        with self.assertRaises(RegistryUnavailable):    # the delayed run cannot append
            reg.record_detector("clear", {"branch": "main", "tip": c,
                                          "at": NOW.isoformat()}, stale_seq)
        reg.close()

    def test_out_of_order_run_cannot_clear_over_incident(self):
        from t39.receipt_registry import RegistryUnavailable
        c = self.commit_file("reviewed")
        self.issue(c)
        self.run_it()
        reg = self.open_reg()
        seq_after_clear = reg.last_detector_event("main")["seq"]
        reg.close()
        d = self.commit_file("direct push")
        self.run_it()                                   # newer run records the incident
        reg = self.open_reg()
        with self.assertRaises(RegistryUnavailable):    # a delayed run that saw only C
            reg.record_detector("clear", {"branch": "main", "tip": c,
                                          "at": NOW.isoformat()}, seq_after_clear)
        reg.close()
        self.assertFalse(self.allowed(c))
        self.assertFalse(self.allowed(d))

    def test_deploy_requires_receipt_for_exact_commit(self):
        c = self.commit_file("unreceipted")
        reg = self.open_reg()                           # even a recorded clear is not enough
        reg.record_detector("clear", {"branch": "main", "tip": c, "at": NOW.isoformat()},
                            None)
        reg.close()
        self.assertFalse(self.allowed(c))

    def test_clearance_expires(self):
        c = self.commit_file("reviewed")
        self.issue(c)
        self.run_it()
        self.assertTrue(self.allowed(c, NOW + timedelta(minutes=60)))
        self.assertFalse(self.allowed(c, NOW + timedelta(minutes=66)))
        self.assertFalse(self.allowed(c, NOW - timedelta(minutes=10)))   # future clearance

    def test_no_ff_merge_commit_is_not_itself_receipted(self):
        # Fast-forward-only policy: the merge commit has no receipt of its own.
        self.git("checkout", "-qb", "feature")
        self.issue(self.commit_file("feature work"))
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
        merge = self.head()
        self.assertEqual(self.run_it().unreceipted, [merge])
        self.assertFalse(self.allowed(merge))

    def test_fast_forward_merge_of_reviewed_branch_is_clean(self):
        self.git("checkout", "-qb", "feature")
        f = self.commit_file("feature work")
        self.issue(f)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--ff-only", "feature")
        self.assertTrue(self.run_it().clean)
        self.assertTrue(self.allowed(f))

    # --- incidents -----------------------------------------------------------

    def test_direct_push_is_incident_and_not_deployable(self):
        self.issue(self.commit_file("reviewed"))
        bad = self.commit_file("direct push")
        r = self.run_it()
        self.assertEqual((r.unreceipted, r.revert_plan), ([bad], [bad]))
        self.assertIn("SECURITY INCIDENT", self.alerts[0])
        self.assertFalse(self.allowed(bad))
        self.assertEqual(self.run_it().unreceipted, [bad])   # keeps reporting

    def test_unreviewed_intermediate_first_parent_commits_are_incidents(self):
        a = self.commit_file("a")
        b = self.commit_file("b")
        self.issue(b)
        self.assertEqual(self.run_it().unreceipted, [a])

    def test_checkpoint_surviving_only_as_second_parent_is_rewrite(self):
        # Dolores review on 68c925b.
        old = self.commit_file("reviewed")
        self.issue(old)
        self.assertTrue(self.run_it().clean)
        self.git("checkout", "-q", "--orphan", "rewritten")
        self.git("rm", "-rqf", ".")
        r1 = self.commit_file("new root", "g.txt")
        self.issue(r1)
        self.git("merge", "-q", "--allow-unrelated-histories", "--no-edit", "-s", "ours", old)
        merge = self.head()
        self.issue(merge)                                 # even if every new commit is receipted
        self.git("branch", "-qf", "main", merge)
        self.git("checkout", "-q", "main")
        r = self.run_it()
        self.assertIn("first-parent", r.error or "")
        self.assertFalse(self.allowed(merge))

    def test_history_reset_fails_loud(self):
        self.issue(self.commit_file("reviewed"))
        self.run_it()
        self.git("reset", "-q", "--hard", self.baseline)
        self.commit_file("rewritten")
        r = self.run_it()
        self.assertIsNotNone(r.error)
        self.assertFalse(self.allowed(self.head()))

    # --- failure modes fail closed -------------------------------------------

    def test_anchor_unreachable_on_clean_run_leaves_deploys_refused(self):
        c = self.commit_file("reviewed")
        self.issue(c)
        self.anchor.fail = True
        r = self.run_it()
        self.assertFalse(r.clean)
        self.assertIsNotNone(r.anchor_error)
        self.anchor.fail = False
        self.assertFalse(self.allowed(c))
        self.assertIn("could not anchor clearance", self.alerts[0])

    def test_anchor_unreachable_on_incident_still_alerts_and_refuses(self):
        bad = self.commit_file("direct push")
        self.anchor.fail = True
        r = self.run_it()
        self.assertEqual(r.unreceipted, [bad])
        self.assertIn("NOT anchored", self.alerts[0])
        self.anchor.fail = False
        self.assertFalse(self.allowed(bad))

    def test_registry_unavailable_fails_loud_and_refuses(self):
        self.commit_file("x")
        self.anchor.path.unlink()
        r = self.run_it()
        self.assertIsNotNone(r.error)
        self.assertFalse(r.clean)
        self.assertTrue(self.alerts)
        self.assertFalse(self.allowed(self.head()))

    def test_db_only_forged_receipt_does_not_hide_direct_push(self):
        from tests.test_t39_store_gate import forge_issue_in_db
        bad = self.commit_file("direct push")
        forge_issue_in_db(self.reg_path, "e" * 64, bad, self.head(f"{bad}^{{tree}}"),
                          ("7" * 64, "8" * 64))
        self.assertFalse(self.run_it().clean)
        self.assertFalse(self.allowed(bad))

    def test_tampered_anchor_log_fails_loud(self):
        self.issue(self.commit_file("reviewed"))
        lines = self.anchor.path.read_text().splitlines()
        lines[-1] = lines[-1].replace('"claude"', '"mallory"')
        self.anchor.path.write_text("\n".join(lines) + "\n")
        r = self.run_it()
        self.assertIsNotNone(r.error)
        self.assertFalse(self.allowed(self.head()))

    def test_wrong_git_dir_is_refused(self):
        sub = self.repo / "notrepo"
        sub.mkdir()
        self.assertIsNotNone(self.run_it(git_dir=sub).error)

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
