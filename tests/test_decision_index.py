"""B2 merge evidence: edited canonical wording -> hash mismatch -> check fails."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import decision_index as di  # noqa: E402


class Sandbox:
    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "governance" / "decisions", self.dir / "decisions")
        self.manifest = self.dir / "decisions" / "manifest.json"
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir)


class CanonicalTests(unittest.TestCase):
    def test_presentation_noise_ignored(self):
        a = "Line one.\nLine two.\n"
        b = "Line one.  \r\n\r\n\r\nLine two.\r\n\r\n"
        self.assertEqual(di.digest(a.replace("\n", "\n\n", 1)), di.digest(b))

    def test_word_change_detected(self):
        self.assertNotEqual(di.digest("Josh must approve."), di.digest("Josh may approve."))

    def test_unicode_nfc(self):
        self.assertEqual(di.digest("café"), di.digest("café"))


class CheckTests(unittest.TestCase):
    def test_repo_manifest_is_clean(self):
        self.assertEqual(di.check(), [])

    def test_edited_wording_blocks(self):
        with Sandbox() as s:
            f = s.dir / "decisions" / "wording" / "T39.md"
            f.write_text(f.read_text().replace("two distinct-key", "one or more"), encoding="utf-8")
            problems = di.check(s.manifest)
            self.assertTrue(any("T39: hash mismatch" in p for p in problems))

    def test_missing_and_unlisted_files_block(self):
        with Sandbox() as s:
            (s.dir / "decisions" / "wording" / "T38.md").unlink()
            (s.dir / "decisions" / "wording" / "T99.md").write_text("x\n")
            problems = di.check(s.manifest)
            self.assertTrue(any("T38: missing" in p for p in problems))
            self.assertTrue(any("unlisted wording file T99.md" in p for p in problems))

    def test_duplicate_ids_rejected(self):
        with Sandbox() as s:
            data = json.loads(s.manifest.read_text())
            data["decisions"].append(dict(data["decisions"][0]))
            s.manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                di.check(s.manifest)

    def test_cli_exit_codes(self):
        self.assertEqual(di.main(["check"]), 0)
        self.assertEqual(di.main([]), 1)


class CompareTests(unittest.TestCase):
    def test_notion_export_matches_and_drifts(self):
        snap = (ROOT / "governance/decisions/wording/T39.md").read_text()
        self.assertTrue(di.compare("T39", snap.replace("\n", "\r\n")))
        self.assertFalse(di.compare("T39", snap.replace("1 h", "24 h")))

    def test_unknown_id(self):
        with self.assertRaises(KeyError):
            di.compare("T999", "x")


class RtIdTests(unittest.TestCase):
    def test_ids_stable_and_complete(self):
        text = (ROOT / "governance/decisions/wording/T39.md").read_text()
        ids = [i for i, _ in di.rt_ids("T39", text)]
        self.assertEqual(ids, [f"RT-T39-{n}" for n in range(1, 7)])

    def test_gap_in_numbering_rejected(self):
        text = "## Acceptance tests\n\n1. a\n3. c\n"
        with self.assertRaises(ValueError):
            di.rt_ids("T1", text)

    def test_parenthesized_numbering(self):
        text = "## Wording\n(1) not a test\n## Acceptance tests\n(1) a\n(2) b\n"
        self.assertEqual([i for i, _ in di.rt_ids("T5", text)], ["RT-T5-1", "RT-T5-2"])


if __name__ == "__main__":
    unittest.main()
