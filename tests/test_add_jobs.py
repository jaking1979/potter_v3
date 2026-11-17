import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import add_jobs as aj  # noqa: E402

VALUES = {"POTTER_HOME": "/opt/potter", "WS": "/w", "TZ": "America/New_York", "TO": "42", "KG_DB": "/s/kg.sqlite",
          "SESSIONS": "/s/sessions", "OC": "openclaw --profile x"}
JOBS = json.loads((ROOT / "install/jobs.json").read_text())["jobs"]


class Table(unittest.TestCase):
    def test_every_job_builds_with_timeout_tz_exact_and_no_placeholders_left(self):
        for job in JOBS:
            a = aj.argv_for(job, VALUES)
            self.assertIn("--timeout-seconds", a, job["name"])
            self.assertIn("--exact", a, job["name"])
            self.assertNotRegex(" ".join(a), r"\{[A-Z_]+\}", job["name"])

    def test_names_are_unique_and_prefixed(self):
        names = [j["name"] for j in JOBS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(n.startswith("potter-") for n in names))

    def test_jobs_that_message_the_owner_or_touch_shared_accounts_start_in_standby(self):
        by = {j["name"]: j for j in JOBS}
        for n in ("potter-daily-trading", "potter-financial-digest", "potter-weekend-digest", "potter-reply-monitor",
                  "potter-fetch-city-events", "potter-llm-monthly-status"):
            self.assertTrue(by[n]["standby"], n)
            self.assertIn("--disabled", aj.argv_for(by[n], VALUES), n)
        for n in ("potter-qc-recorder", "potter-qc-verifier", "potter-qc-recheck", "potter-kg-curate"):
            self.assertFalse(by[n]["standby"], n)

    def test_command_jobs_never_deliver_and_agent_jobs_deliver_only_when_asked(self):
        for job in JOBS:
            a = aj.argv_for(job, VALUES)
            if job["kind"] == "command":
                self.assertIn("--no-deliver", a)
                self.assertNotIn("--announce", a)
            elif job.get("deliver"):
                self.assertEqual(a[a.index("--to") + 1], "42")
            else:
                self.assertIn("--no-deliver", a)

    def test_the_trading_job_runs_the_trading_skill_whose_precheck_gates_it(self):
        t = next(j for j in JOBS if j["name"] == "potter-daily-trading")
        self.assertIn("trading-run", t["message"])
        skill = (ROOT / "overlay/workspace/skills/trading-run/SKILL.md").read_text()
        self.assertIn("trading_precheck", skill)

    def test_bad_definitions_are_refused(self):
        for bad in ({"name": "x", "cron": "0 * * * *", "kind": "command", "timeout": 1, "command": "c"},
                    {"name": "potter-a", "cron": "every hour", "kind": "command", "timeout": 1, "command": "c"},
                    {"name": "potter-a", "cron": "0 * * * *", "kind": "command", "timeout": 0, "command": "c"},
                    {"name": "potter-a", "cron": "0 * * * *", "kind": "magic", "timeout": 5},
                    {"name": "potter-a", "cron": "0 * * * *", "kind": "command", "timeout": 5, "command": "{NOPE}"}):
            with self.assertRaises((ValueError, KeyError)):
                aj.argv_for(bad, VALUES)

    def test_standby_jobs_are_recorded_for_handover_without_duplicates(self):
        with tempfile.TemporaryDirectory() as t:
            fake = Path(t) / "oc"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
            jobs = Path(t) / "jobs.json"
            jobs.write_text(json.dumps({"jobs": JOBS[:6]}))
            reg = Path(t) / "handover.json"
            reg.write_text(json.dumps({"crons": [{"name": "potter-fetch-city-events", "enabled": False}]}))
            args = ["--jobs", str(jobs), "--openclaw", str(fake), "--handover", str(reg)] + \
                   [x for k, v in VALUES.items() for x in ("--set", f"{k}={v}")]
            self.assertEqual(aj.main(args), 0)
            names = [c["name"] for c in json.loads(reg.read_text())["crons"]]
            self.assertEqual(names.count("potter-fetch-city-events"), 1)
            self.assertIn("potter-fetch-local", names)
            self.assertNotIn("potter-qc-recorder", names)


if __name__ == "__main__":
    unittest.main()
