"""Personal profile builder: what becomes an active rule, what is reference, redaction, index, safety."""
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import import_v2 as iv  # noqa: E402
import profile_from_v2 as pf  # noqa: E402

WS = "tree/home/owner/.openclaw/workspace/"
XID = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
FILES = {
    "rules/INDEX.md": "| Trigger | File |\n|---|---|\n| \"Email,\" inbox | `domains/email.md` |\n"
                      "| tone | `global/communication.md` |\n| event tastes | `../preferences/events.md` |\n"
                      "| past mistakes | `../.learnings/ERRORS.md`, `../.learnings/CORRECTIONS.md` |\n",
    "rules/domains/email.md": "Check /home/owner/.openclaw/workspace/prompts/x.md and ~/.openclaw/workspace/state/y.md",
    "rules/domains/travel.md": "book nothing without asking",
    "rules/people/owner.md": "likes directness",
    "rules/projects/potter-v2-rebuild.md": "how v2 was rebuilt",
    "rules/global/communication.md": "1. Direct.",
    "rules/global/permissions.md": f"EHR is a hard wall. Page ID `{XID}`. Other page {OTHER}.",
    "rules/global/tool-use.md": "old tool rules",
    "preferences/events.md": "jazz yes",
    ".learnings/ERRORS.md": "see .learnings/CORRECTIONS.md",
    "trading/mandate.md": "limits",
    "prompts/morning-brief.md": "old brief",
    "wiki/index.md": "wiki",
    "AGENTS.md": "old agents",
    "main/AGENTS.md": "other agent",
    "scripts/run.py": "print(1)",
    "config/.ha-token": "abc",
    "rules/domains/leaky.md": "key sk-or-abcdefghijklmnop1234",
}


def make(path, files=FILES, schema=pf.SCHEMA2, extra=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({"schema": schema}))
        for k, v in files.items():
            z.writestr(WS + k, v)
        for n, d in extra:
            z.writestr(n, d)


class Profile(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.zip = self.tmp / "e.zip"
        self.ws = self.tmp / "ws"
        (self.ws / "rules").mkdir(parents=True)
        (self.ws / "rules/INDEX.md").write_text("# idx\n\n## Personal rules\n<!-- PERSONAL-INDEX: x -->\n")
        (self.ws / "rules/global").mkdir()
        (self.ws / "rules/global/permissions.md").write_text("V3 PERMISSIONS")
        self.state = self.tmp / "state"
        make(self.zip)
        self.rep = pf.apply(self.zip, self.ws, self.state)

    def test_personal_files_are_active_and_machinery_is_reference_only(self):
        for f in ("rules/domains/email.md", "rules/domains/travel.md", "rules/people/owner.md", "preferences/events.md",
                  "rules/personal/communication-style.md", "learnings/ERRORS.md", "trading/mandate.md"):
            self.assertTrue((self.ws / f).exists(), f)
        self.assertEqual((self.ws / "rules/global/permissions.md").read_text(), "V3 PERMISSIONS")   # v3's wins
        for f in ("rules/global/tool-use.md", "rules/global/permissions.md", "prompts/morning-brief.md", "AGENTS.md",
                  "rules/projects/potter-v2-rebuild.md", "wiki/index.md"):
            ref = self.ws / "reference/v2" / f
            self.assertTrue(ref.read_text().startswith("> Reference only"), f)
        self.assertFalse((self.ws / "rules/projects/potter-v2-rebuild.md").exists())
        self.assertFalse((self.ws / "AGENTS.md").exists())

    def test_other_agents_code_secrets_and_hidden_files_are_left_out(self):
        everything = [str(p.relative_to(self.ws)) for p in self.ws.rglob("*") if p.is_file()]
        for bad in ("main/AGENTS.md", "scripts/run.py", ".ha-token", "leaky.md"):
            self.assertFalse(any(bad in e for e in everything), bad)
        self.assertIn("rules/domains/leaky.md", [s["file"] for s in self.rep["skipped"]])

    def test_old_paths_are_rewritten_and_the_wall_id_is_redacted_everywhere(self):
        email = (self.ws / "rules/domains/email.md").read_text()
        self.assertEqual(email, "Check prompts/x.md and state/y.md")
        self.assertEqual((self.ws / "learnings/ERRORS.md").read_text(), "see learnings/CORRECTIONS.md")
        blob = "".join(p.read_text() for p in self.ws.rglob("*.md"))
        self.assertNotIn(XID, blob)
        self.assertIn("[excluded page]", blob)
        # A second id on the same wall line is treated as excluded too: over-blocking is the safe mistake.
        self.assertNotIn(OTHER, blob)
        self.assertEqual(json.loads((self.state / "excluded-ids.json").read_text()), [XID, OTHER])

    def test_index_carries_triggers_for_active_files_and_lists_the_rest(self):
        idx = (self.ws / "rules/INDEX.md").read_text()
        self.assertIn('| "Email," inbox | `domains/email.md` |', idx)
        self.assertIn("| event tastes | `../preferences/events.md` |", idx)
        self.assertIn("`../learnings/ERRORS.md`", idx)
        self.assertIn("| travel | `domains/travel.md` |", idx)
        self.assertNotIn("global/communication.md", idx.split("## Personal rules")[1])
        self.assertNotIn("PERSONAL-INDEX", idx)

    def test_wrong_schema_missing_workspace_and_unsafe_zips_are_refused(self):
        make(self.zip, schema="potter.v2-export/1")
        with zipfile.ZipFile(self.zip) as z, self.assertRaises(iv.BundleError):
            pf.build_plan(z)
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("manifest.json", json.dumps({"schema": pf.SCHEMA2}))
        with zipfile.ZipFile(self.zip) as z, self.assertRaises(iv.BundleError):
            pf.build_plan(z)
        make(self.zip, extra=[("../evil", "x")])
        with zipfile.ZipFile(self.zip) as z, self.assertRaises(iv.BundleError):
            pf.build_plan(z)

    def test_a_github_download_zip_with_a_wrapping_folder_works(self):
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("potter-v2-export-main/manifest.json", json.dumps({"schema": pf.SCHEMA2}))
            for k, v in FILES.items():
                z.writestr("potter-v2-export-main/" + WS + k, v)
        ws2 = self.tmp / "ws2"
        ws2.mkdir()
        rep = pf.apply(self.zip, ws2, self.tmp / "s2")
        self.assertIn("rules/domains/email.md", rep["active_files"])
        self.assertEqual((ws2 / "rules/domains/email.md").read_text(), "Check prompts/x.md and state/y.md")

    def test_ids_are_only_taken_from_lines_about_the_wall(self):
        self.assertEqual(pf.excluded_ids([f"database {OTHER} holds tasks", f"never touch the EHR page {XID}"]), [XID])
        self.assertEqual(pf.excluded_ids(["EHR 01234567-89ab-cdef-0123-456789abcdef"]), [XID])


if __name__ == "__main__":
    unittest.main()
