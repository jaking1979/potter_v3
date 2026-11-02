"""B2 merge evidence: edited canonical wording -> hash mismatch -> check fails;
snapshots are reproducible verbatim extracts of the Notion record."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import decision_index as di  # noqa: E402

WORDING = ROOT / "governance" / "decisions" / "wording"


def fake_export(*ids, tamper=None):
    """A Notion-page-shaped export containing the current snapshots verbatim,
    surrounded by other content, optionally with one edit applied."""
    parts = ["<page>\n<content>\n# Josh Review Queue\n\nstuff\n\n## T40 · Other thread\n\n<b>Claude · x</b>\n\n"]
    for i in ids:
        parts.append((WORDING / f"{i}.md").read_text(encoding="utf-8").replace("\n", "\r\n") + "\n")
    parts.append("## Next section\n\nmore\n</content>\n</page>\n")
    text = "".join(parts)
    if tamper:
        old, new = tamper
        assert old in text
        text = text.replace(old, new, 1)
    return text


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
        self.assertEqual(di.digest("A.\n\nB.\n"), di.digest("A.  \r\n\r\n\r\nB.\r\n\r\n"))

    def test_word_change_detected(self):
        self.assertNotEqual(di.digest("Josh must approve."), di.digest("Josh may approve."))

    def test_unicode_nfc(self):
        self.assertEqual(di.digest("café"), di.digest("café"))


class ExtractTests(unittest.TestCase):
    def test_snapshots_are_verbatim_extracts(self):
        export = fake_export("T37", "T38", "T39")
        for i in ("T37", "T38", "T39"):
            with self.subTest(i=i):
                self.assertEqual(di.extract(i, export), di.canonical((WORDING / f"{i}.md").read_text()))

    def test_t38_keeps_test6_exclusion_clause(self):
        self.assertIn("If test 6 fails", (WORDING / "T38.md").read_text())

    def test_extract_stops_at_next_section_and_keeps_subsections(self):
        t37 = di.extract("T37", fake_export("T37"))
        self.assertIn("### T37 acceptance appendix", t37)
        self.assertNotIn("## Next section", t37)

    def test_missing_record(self):
        with self.assertRaises(KeyError):
            di.extract("T99", fake_export("T39"))


class CheckTests(unittest.TestCase):
    def test_repo_manifest_is_clean(self):
        self.assertEqual(di.check(), [])

    def test_edited_wording_blocks(self):
        with Sandbox() as s:
            f = s.dir / "decisions" / "wording" / "T39.md"
            f.write_text(f.read_text().replace("two distinct-key", "one or more"), encoding="utf-8")
            self.assertTrue(any("T39: hash mismatch" in p for p in di.check(s.manifest)))

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
    def test_coordinated_edit_caught_against_notion(self):
        """Editing wording AND hash together passes `check`, but `compare`
        against the Notion export still reports drift."""
        with Sandbox() as s:
            f = s.dir / "decisions" / "wording" / "T39.md"
            f.write_text(f.read_text().replace("within 1 h", "within 24 h"), encoding="utf-8")
            data = json.loads(s.manifest.read_text())
            for e in data["decisions"]:
                if e["id"] == "T39":
                    e["sha256"] = di.digest(f.read_text())
            s.manifest.write_text(json.dumps(data))
            self.assertEqual(di.check(s.manifest), [])
            self.assertFalse(di.compare("T39", fake_export("T39"), s.manifest))

    def test_notion_edit_detected(self):
        self.assertTrue(di.compare("T39", fake_export("T39")))
        self.assertFalse(di.compare("T39", fake_export("T39", tamper=("within 1 h", "within 24 h"))))

    def test_unknown_id(self):
        with self.assertRaises(KeyError):
            di.compare("T999", "x")


class SourceCheckTests(unittest.TestCase):
    """L16: reviewer-produced source-check record; gate-side binding/freshness."""
    T0 = 1_800_000_000

    def rec(self, export=None, **kw):
        export = export or fake_export("T37", "T38", "T39")
        args = dict(page_id="401a667c-6316-813f-908f-e6e563f4f7eb", retrieved_at=self.T0, page_last_edited=self.T0 - 60,
                    candidate="c" * 40)
        args.update(kw)
        return export, di.compare_all(export, **args)

    def test_pass_and_verify(self):
        export, r = self.rec()
        self.assertTrue(r["pass"])
        self.assertEqual(di.verify_source_check(r, export, "c" * 40, self.T0 + 60), [])

    def test_mismatch_fails(self):
        export, r = self.rec(export=fake_export("T37", "T38", "T39", tamper=("within 1 h", "within 2 h")))
        self.assertFalse(r["pass"])
        self.assertIn("comparison failed or results do not reproduce",
                      di.verify_source_check(r, export, "c" * 40, self.T0 + 60))

    def test_missing_decision_in_export_fails(self):
        _, r = self.rec(export=fake_export("T38", "T39"))
        self.assertFalse(r["pass"])

    def test_wrong_candidate_stale_and_edited_after(self):
        export, r = self.rec()
        self.assertIn("bound to a different candidate", di.verify_source_check(r, export, "d" * 40, self.T0 + 60))
        self.assertIn("stale or future-dated retrieval",
                      di.verify_source_check(r, export, "c" * 40, self.T0 + di.FRESHNESS_S))
        export2, r2 = self.rec(page_last_edited=self.T0 + 5)
        self.assertIn("page edited after retrieval", di.verify_source_check(r2, export2, "c" * 40, self.T0 + 60))

    def test_forged_results_or_swapped_export_refused(self):
        export, r = self.rec()
        r_forged = dict(r, results={k: True for k in r["results"]})
        bad_export = fake_export("T37", "T38", "T39", tamper=("within 1 h", "within 9 h"))
        self.assertIn("export does not match the recorded export hash",
                      di.verify_source_check(r_forged, bad_export, "c" * 40, self.T0 + 60))

    def test_wrong_page_refused(self):
        export, r = self.rec(page_id="406a667c-6316-819d-b335-d31cb4b8b69a")
        self.assertIn("not the canonical Council page",
                      di.verify_source_check(r, export, "c" * 40, self.T0 + 60))

    def test_unavailable_source_fails_closed(self):
        _, r = self.rec(export=fake_export("T37", "T38", "T39"))
        problems = di.verify_source_check(r, "", "c" * 40, self.T0 + 60)
        self.assertIn("source unavailable (empty export)", problems)

    def test_helper_is_not_provenance(self):
        """Documents the limit Dolores identified: a synthetic export with
        asserted timestamps is internally consistent and passes this helper.
        Provenance/freshness must come from the signed reviewer fetch (B1/B5)."""
        export, r = self.rec()  # built from snapshots, timestamps asserted
        self.assertEqual(di.verify_source_check(r, export, "c" * 40, self.T0 + 60), [])
        self.assertIn("NON-AUTHORITATIVE", di.verify_source_check.__doc__)

    def test_wrong_manifest_refused(self):
        with Sandbox() as s:
            export, r = self.rec()
            data = json.loads(s.manifest.read_text())
            data["note"] = "changed"
            s.manifest.write_text(json.dumps(data))
            self.assertIn("bound to a different manifest",
                          di.verify_source_check(r, export, "c" * 40, self.T0 + 60, s.manifest))


class CoverageTests(unittest.TestCase):
    def test_coverage_reports_unindexed(self):
        missing = di.coverage()
        self.assertNotIn("T39", missing)
        self.assertIn("T1", missing)  # B2 is not complete until this is empty

    def test_coverage_exit_code(self):
        self.assertEqual(di.main(["coverage"]), 3 if di.coverage() else 0)


class RtIdTests(unittest.TestCase):
    def ids(self, i):
        return [r for r, _ in di.rt_ids(i, (WORDING / f"{i}.md").read_text())]

    def test_numbered_list(self):
        self.assertEqual(self.ids("T38"), [f"RT-T38-{n}" for n in range(1, 7)])

    def test_inline_list(self):
        self.assertEqual(self.ids("T39"), [f"RT-T39-{n}" for n in range(1, 7)])

    def test_s_appendix(self):
        self.assertEqual(self.ids("T37"), [f"RT-T37-S{n}" for n in range(1, 14)])

    def test_gap_in_numbering_rejected(self):
        with self.assertRaises(ValueError):
            di.rt_ids("T1", "<b>Acceptance tests:</b>\n\n1. a\n3. c\n")


if __name__ == "__main__":
    unittest.main()
