import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import sync_shipped as ss  # noqa: E402


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin:/usr/local/bin"})


class Sync(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        base = Path(self.t.name)
        self.repo, self.ws = base / "repo", base / "ws"
        self.src = self.repo / "overlay/workspace"
        (self.src / "skills/mon").mkdir(parents=True)
        (self.src / "rules/global").mkdir(parents=True)
        git(base, "init", "-q", str(self.repo))
        self.ship({"skills/mon/SKILL.md": "v1\n", "skills/mon/helper.py": "x=1\n", "rules/global/a.md": "rule v1\n"})
        self.ws.mkdir()

    def tearDown(self):
        self.t.cleanup()

    def ship(self, files):
        for rel, text in files.items():
            (self.src / rel).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c")

    def run_sync(self):
        return ss.sync(self.repo, self.ws)

    def test_fresh_install_copies_everything_and_records_it(self):
        r = self.run_sync()
        self.assertEqual(r["counts"]["added"], 3)
        self.assertEqual((self.ws / "skills/mon/SKILL.md").read_text(), "v1\n")
        m = json.loads((self.ws / "state/shipped/manifest.json").read_text())
        self.assertEqual(m["files"]["skills/mon/SKILL.md"]["blob"], ss.blob(b"v1\n"))

    def test_untouched_files_update_and_edited_files_are_kept_with_upstream_beside(self):
        self.run_sync()
        (self.ws / "skills/mon/SKILL.md").write_text("v1 + my scope\n")
        self.ship({"skills/mon/SKILL.md": "v2\n", "rules/global/a.md": "rule v2\n"})
        r = self.run_sync()
        self.assertEqual(r["kept"], ["skills/mon/SKILL.md"])
        self.assertEqual((self.ws / "skills/mon/SKILL.md").read_text(), "v1 + my scope\n")
        self.assertEqual((self.ws / "skills/mon/SKILL.md.upstream").read_text(), "v2\n")
        self.assertEqual((self.ws / "rules/global/a.md").read_text(), "rule v2\n")
        self.run_sync()                                                       # still kept on the next update
        self.assertEqual((self.ws / "skills/mon/SKILL.md").read_text(), "v1 + my scope\n")

    def test_when_the_repo_catches_up_the_file_is_managed_again(self):
        self.run_sync()
        (self.ws / "skills/mon/SKILL.md").write_text("better\n")
        self.ship({"skills/mon/SKILL.md": "better\n"})
        self.run_sync()
        self.assertFalse((self.ws / "skills/mon/SKILL.md.upstream").exists())
        self.ship({"skills/mon/SKILL.md": "better v3\n"})
        self.run_sync()
        self.assertEqual((self.ws / "skills/mon/SKILL.md").read_text(), "better v3\n")

    def test_before_a_manifest_an_old_shipped_copy_is_refreshed_but_a_local_edit_is_kept(self):
        (self.ws / "skills/mon").mkdir(parents=True)
        (self.ws / "skills/mon/SKILL.md").write_text("v1\n")                  # an older shipped copy
        (self.ws / "rules/global").mkdir(parents=True)
        (self.ws / "rules/global/a.md").write_text("rule v1, edited\n")       # never in the repo
        self.ship({"skills/mon/SKILL.md": "v2\n", "rules/global/a.md": "rule v2\n"})
        r = self.run_sync()
        self.assertEqual((self.ws / "skills/mon/SKILL.md").read_text(), "v2\n")
        self.assertEqual(r["kept"], ["rules/global/a.md"])

    def test_report_lists_edits_with_a_diff_new_files_and_notes(self):
        self.run_sync()
        (self.ws / "skills/mon/SKILL.md").write_text("v1\nnear first\n")
        (self.ws / "skills/mine").mkdir()
        (self.ws / "skills/mine/SKILL.md").write_text("my own skill\n")
        (self.ws / "state/shipped/notes.md").write_text("2026-11-20 | skills/mon/SKILL.md | rank near first\n")
        r = ss.report(self.repo, self.ws)
        self.assertEqual([e["path"] for e in r["edited"]], ["skills/mon/SKILL.md"])
        self.assertIn("+near first", r["edited"][0]["diff"])
        self.assertIn("rank near first", r["edited"][0]["notes"][0])
        self.assertEqual([n["path"] for n in r["new"]], ["skills/mine/SKILL.md"])
        md = (self.ws / "state/shipped/DRIFT.md").read_text()
        self.assertIn("## Edited: `skills/mon/SKILL.md`", md)
        self.assertIn("## New: `skills/mine/SKILL.md`", md)

    def test_accept_takes_the_repo_version_and_archives_the_local_one(self):
        self.run_sync()
        (self.ws / "skills/mon/SKILL.md").write_text("mine\n")
        self.ship({"skills/mon/SKILL.md": "v2\n"})
        self.run_sync()
        done = ss.accept(self.repo, self.ws, "skills/mon")
        self.assertEqual(done, ["skills/mon/SKILL.md"])
        self.assertEqual((self.ws / "skills/mon/SKILL.md").read_text(), "v2\n")
        self.assertFalse((self.ws / "skills/mon/SKILL.md.upstream").exists())
        self.assertEqual(next((self.ws / "state/shipped/archive").rglob("SKILL.md")).read_text(), "mine\n")
        self.ship({"skills/mon/SKILL.md": "v3\n"})
        self.run_sync()
        self.assertEqual((self.ws / "skills/mon/SKILL.md").read_text(), "v3\n")
        with self.assertRaises(SystemExit):
            ss.accept(self.repo, self.ws, "skills/nope")


class Wiring(unittest.TestCase):
    def test_update_and_install_use_it_instead_of_copying_over(self):
        u = (ROOT / "install/update.sh").read_text()
        self.assertIn("sync_shipped.py", u)
        self.assertNotIn('cp "$REPO/overlay/workspace/rules/global/"*.md "$WS/rules/global/"', u)
        self.assertIn("sync_shipped.py", (ROOT / "install/bootstrap.sh").read_text())

    def test_weekly_report_then_note_jobs_exist(self):
        jobs = {j["name"]: j for j in json.loads((ROOT / "install/jobs.json").read_text())["jobs"]}
        self.assertEqual(jobs["potter-shipped-report"]["command"], "potter-shipped --report")
        self.assertEqual(jobs["potter-local-changes"]["tools"], ["read"])
        self.assertIn("local-changes", jobs["potter-local-changes"]["message"])


if __name__ == "__main__":
    unittest.main()
