"""Every skill v2 had exists in v3 (rewritten for v3, or replaced by a named v3 part), and the board meeting works."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "overlay/workspace/skills"

# The skills in the v2 export (schema 2). Each maps to the v3 skill or part that does its job.
V2_SKILLS = {
    "agentmail": "the official AgentMail plugin (install/bootstrap.sh)",
    "agentmail-processing-pipeline": "skills/mail-intake",
    "board-meeting": "skills/board-meeting",
    "home-assistant": "skills/home-status + Merrick (overlay/merrick)",
    "meal-plan": "skills/meal-plan",
    "notion-architecture-reconciler": "skills/notion-architecture-reconciler",
    "resumable-execution": "skills/resumable-execution",
    "self-driving-phase-gates": "skills/self-driving-phase-gates",
}


class Parity(unittest.TestCase):
    def test_every_v2_skill_has_a_v3_counterpart(self):
        for v2, v3 in V2_SKILLS.items():
            for part in v3.split(" + "):
                if part.startswith("skills/"):
                    self.assertTrue((SKILLS / part.split("/", 1)[1] / "SKILL.md").exists(), f"{v2} -> {part}")
        boot = (ROOT / "install/bootstrap.sh").read_text()
        self.assertIn("@agentmail/agentmail", boot)

    def test_ported_skills_hold_nothing_personal(self):
        import re
        for name in ("meal-plan", "board-meeting", "notion-architecture-reconciler", "resumable-execution", "self-driving-phase-gates"):
            for f in (SKILLS / name).rglob("*"):
                if f.is_file():
                    t = f.read_text(errors="ignore")
                    self.assertNotRegex(t, r"(?i)\bjosh\b|@gmail\.com|agentmail\.to|\b\d{9,10}\b|[0-9a-f]{8}-[0-9a-f]{4}-", f"{f}")

    def test_board_meeting_node_tests_pass(self):
        r = subprocess.run(["node", "--test", "board.test.mjs"], cwd=SKILLS / "board-meeting", capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2500:] + r.stderr[-500:])
        self.assertIn("fail 0", r.stdout)


if __name__ == "__main__":
    unittest.main()
