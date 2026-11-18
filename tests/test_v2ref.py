"""T1's v2 baseline workspace: v2's own files plus the owner's personal files; none of v3's machinery."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import v2ref_workspace  # noqa: E402


class V2Ref(unittest.TestCase):
    def test_built_from_v2_files_and_personal_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            for rel, text in {"reference/v2/AGENTS.md": "v2 agents", "reference/v2/SOUL.md": "v2 soul", "reference/v2/rules/global/x.md": "v2 rule",
                              "USER.md": "owner", "preferences/food.md": "wine", "rules/people/family.md": "people",
                              "rules/global/memory.md": "v3 machinery", "skills/vault/SKILL.md": "v3 skill", "AGENTS.md": "v3 agents"}.items():
                p = ws / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
            out = Path(d) / "v2ref"
            r = v2ref_workspace.build(ws, out)
            self.assertTrue(r["has_agents_md"] and r["has_soul"])
            self.assertEqual((out / "AGENTS.md").read_text(), "v2 agents")              # v2's, not v3's
            self.assertEqual((out / "rules/global/x.md").read_text(), "v2 rule")
            self.assertEqual((out / "USER.md").read_text(), "owner")
            self.assertTrue((out / "preferences/food.md").exists() and (out / "rules/people/family.md").exists())
            self.assertFalse((out / "skills").exists())
            self.assertFalse((out / "rules/global/memory.md").exists())
            v2ref_workspace.build(ws, out)                                                # rebuild is clean
            self.assertEqual((out / "AGENTS.md").read_text(), "v2 agents")

    def test_installed(self):
        for f in ("install/update.sh", "install/bootstrap.sh"):
            self.assertIn("oc agents add potter-v2ref", (ROOT / f).read_text(), f)


if __name__ == "__main__":
    unittest.main()


class TellsTheBaseline(unittest.TestCase):
    def test_owner_messages_since_install_become_daily_notes_jobs_and_evals_do_not(self):
        import json as j
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"; (ws / "reference/v2").mkdir(parents=True); (ws / "reference/v2/AGENTS.md").write_text("v2")
            ses = Path(d) / "sessions"; ses.mkdir()
            rows = [
                {"type": "message", "timestamp": "2026-11-18T22:12:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "Thanksgiving is Spanish this year: paella mixta."}]}},
                {"type": "message", "timestamp": "2026-11-18T22:13:00Z", "message": {"role": "assistant", "content": "Noted."}},
                {"timestamp": "2026-11-18T22:14:00Z", "role": "user", "content": "Run the morning-brief skill."},
                {"timestamp": "2026-11-10T09:00:00Z", "role": "user", "content": "before the install"},
            ]
            (ses / "main.jsonl").write_text("\n".join(j.dumps(r) for r in rows))
            (ses / "t1-eval-1-brief1.jsonl").write_text(j.dumps({"timestamp": "2026-11-18T23:00:00Z", "role": "user", "content": "eval question"}))
            out = Path(d) / "v2ref"
            r = v2ref_workspace.build(ws, out, ses, since="2026-11-17")
            self.assertEqual(r["owner_messages"], 1)
            note = (out / "memory/2026-11-18.md").read_text()
            self.assertIn("22:12 The owner said: Thanksgiving is Spanish this year: paella mixta.", note)
            self.assertNotIn("morning-brief", note)
            self.assertNotIn("eval question", note)
            self.assertFalse((out / "memory/2026-11-10.md").exists())
