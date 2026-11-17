import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import add_jobs  # noqa: E402
import apply_models as am  # noqa: E402

POLICY = ROOT / "overlay/jev/policy.json"


class Policy(unittest.TestCase):
    def test_the_shipped_policy_is_complete_and_has_no_open_ended_routing(self):
        classes = am.load(str(POLICY))
        self.assertTrue({"chat", "job_turn", "worker", "escalation", "merrick", "local_small", "decision",
                         "kg_curation"} <= set(classes))
        text = POLICY.read_text()
        self.assertNotIn("REPLACE_", text)
        refs = [r for c in classes.values() for r in [c["model"], *c["fallbacks"]]]
        self.assertFalse([r for r in refs if "/auto" in r], refs)
        for name, c in classes.items():
            self.assertGreater(c["maxCostUsd"], 0, name)                 # every class has a hard cost ceiling
            self.assertGreater(c["timeoutMs"], 0, name)
        self.assertTrue(classes["decision"]["model"].startswith("~typesafe/jev"))

    def test_placeholders_and_auto_routing_are_refused(self):
        base = json.loads(POLICY.read_text())
        for bad in ("REPLACE_WITH_MAIN_MODEL", "openrouter/auto", "openrouter/openrouter/auto"):
            p = json.loads(json.dumps(base))
            p["classes"]["chat"]["model"] = bad
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(p, f)
            with self.assertRaises(ValueError, msg=bad):
                am.load(f.name)
            self.assertEqual(am.main([f.name, "--job-args"]), 2)
            Path(f.name).unlink()

    def test_patch_sets_default_potter_and_merrick_and_lists_only_policy_models(self):
        classes = am.load(str(POLICY))
        p = am.patch(classes, ["potter", "merrick"])["agents"]
        self.assertEqual(p["defaults"]["model"]["primary"], classes["chat"]["model"])
        self.assertEqual(p["entries"]["merrick"]["model"]["primary"], classes["merrick"]["model"])
        self.assertNotIn("~typesafe/jev-latest", p["defaults"]["models"])   # decision endpoint ids are not chat models
        self.assertIn(classes["local_small"]["model"], p["defaults"]["models"])

    def test_every_agent_job_names_its_model(self):
        args = subprocess.run([sys.executable, str(ROOT / "install/apply_models.py"), str(POLICY), "--job-args"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(args.startswith("--model "))
        values = {"POTTER_HOME": "/p", "WS": "/w", "TZ": "UTC", "TO": "1", "OC": "openclaw", "KG_DB": "/k",
                  "SESSIONS": "/s", "JOB_MODEL_ARGS": args}
        jobs = json.loads((ROOT / "install/jobs.json").read_text())["jobs"]
        agent_jobs = [j for j in jobs if j["kind"] == "agent"]
        self.assertTrue(agent_jobs)
        for j in agent_jobs:
            self.assertIn("--model", add_jobs.argv_for(j, values), j["name"])
        boot = (ROOT / "install/bootstrap.sh").read_text()
        self.assertIn("overlay/jev/policy.json", boot)
        self.assertNotIn("policy.example.json", boot)
        self.assertEqual(boot.count('"${JOB_MODEL_ARGS[@]}"'), 2)           # morning brief and mail intake
        self.assertIn("apply_models potter,merrick", boot)
        self.assertIn('"command": "potter-kg-curate', (ROOT / "install/jobs.json").read_text())


if __name__ == "__main__":
    unittest.main()
