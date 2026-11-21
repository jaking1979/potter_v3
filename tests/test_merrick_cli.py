import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import cli, pathlog  # noqa: E402
from merrick.testing import HOUSE, FakeHome, states  # noqa: E402


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        (self.state / "house.json").write_text(json.dumps(HOUSE))
        self.fake = FakeHome(states())
        self.fake.history = lambda entity, since: []
        p = mock.patch.object(cli, "_client", lambda ns: self.fake)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch("merrick.ha.time.sleep", lambda s: None)
        s.start()
        self.addCleanup(s.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--state-dir", str(self.state), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_ask_fast_path_prints_the_result_and_logs_it(self):
        code, out, _ = self.run_cli("ask", "set heat to 68")
        res = json.loads(out)
        self.assertEqual((code, res["path"], res["intent"], res["outcome"]), (0, "fast", "set_temperature", "ok"))
        self.assertNotIn("deadline_epoch", res)
        self.assertEqual(len(pathlog.read(self.state / "path-log.jsonl")), 1)

    def test_ask_fallback_hands_the_agent_a_deadline_and_the_end_is_logged(self):
        code, out, _ = self.run_cli("ask", "why is it cold downstairs", "--budget", "45")
        res = json.loads(out)
        self.assertEqual((res["path"], res["budget_s"]), ("fallback", 45.0))
        self.assertEqual(res["deadline_epoch"] - res["started_epoch"], 45)
        self.assertIn("budget-check", res["instruction"])
        code, out, _ = self.run_cli("budget-check", "--deadline", str(time.time() + 30))
        self.assertEqual((code, json.loads(out)["expired"]), (0, False))
        code, out, _ = self.run_cli("budget-check", "--deadline", str(time.time() - 1))
        self.assertEqual(code, 3)
        self.assertIn("STOP", json.loads(out)["instruction"])
        code, _, _ = self.run_cli("fallback-end", "--id", res["id"], "--started", str(time.time() - 50),
                                  "--outcome", "budget_hit")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("report", "--json")
        rep = json.loads(out)
        self.assertEqual((rep["fallback"], rep["fallback_outcomes"], rep["fallback_unfinished"]), (1, {"budget_hit": 1}, 0))
        self.assertGreaterEqual(rep["fallback_ms"]["median"], 50_000)
        code, out, _ = self.run_cli("report")
        self.assertIn("[needs_reasoning] why is it cold downstairs", out)

    def test_departure_asks_first_then_locks_and_arms_once_on_confirmation(self):
        h = dict(HOUSE, departure={"locks": ["lock.front_door"], "alarm": "alarm_control_panel.house"})
        (self.state / "house.json").write_text(json.dumps(h))
        self.fake.states["lock.front_door"] = {"state": "unlocked", "attributes": {}}
        self.fake.states["alarm_control_panel.house"] = {"state": "disarmed", "attributes": {}}
        code, out, _ = self.run_cli("confirm-departure")                    # nothing was asked yet
        self.assertEqual((code, json.loads(out)["outcome"]), (1, "refused"))
        code, out, _ = self.run_cli("ask", "close up the house")
        res = json.loads(out)
        self.assertEqual(res["outcome"], "needs_confirmation")
        self.assertNotIn("pending_departure", res)
        self.assertTrue((self.state / "departure-pending.json").exists())
        self.assertEqual(self.fake.calls, [])
        code, out, _ = self.run_cli("confirm-departure")
        self.assertEqual((code, json.loads(out)["outcome"]), (0, "done"))
        self.assertEqual(self.fake.states["lock.front_door"]["state"], "locked")
        self.assertFalse((self.state / "departure-pending.json").exists())
        code, out, _ = self.run_cli("confirm-departure")                    # cannot be replayed
        self.assertEqual((code, json.loads(out)["outcome"]), (1, "refused"))
        self.assertEqual(len(self.fake.calls), 2)

    def test_cold_watch_is_silent_when_all_is_well(self):
        self.assertEqual(self.run_cli("cold-watch"), (0, "", ""))
        saved = json.loads((self.state / "cold-watch.json").read_text())
        self.assertEqual((saved["seen"], saved["previous"]["basement_f"]), ({}, 52.0))

    def test_cold_watch_speaks_once_for_a_problem_then_stays_quiet(self):
        self.fake.states["sensor.basement_temperature"]["state"] = "38.0"
        code, out, _ = self.run_cli("cold-watch")
        self.assertEqual(code, 0)
        self.assertIn("basement 38.0 F, below the 40 F pipe-safety line", out)
        self.assertEqual(self.run_cli("cold-watch"), (0, "", ""))

    def test_cold_watch_fails_the_job_quietly_when_the_basement_cannot_be_read(self):
        self.fake.fail_reads = True
        code, out, err = self.run_cli("cold-watch")
        self.assertEqual((code, out), (1, ""))                     # nothing in chat; the watchdog sees the failure
        self.assertIn("could not be read", err)

    def test_cold_watch_keeps_its_state_when_delivery_fails(self):
        self.fake.states["sensor.basement_temperature"]["state"] = "38.0"
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
            code, _, _ = self.run_cli("cold-watch", "--to", "12345")
        self.assertEqual(code, 1)
        self.assertEqual(run.call_args[0][0][:6], ["openclaw", "message", "send", "--channel", "telegram", "--target"])
        self.assertFalse((self.state / "cold-watch.json").exists())   # so the next run tries again

    def test_intend_then_tick_runs_it_once_and_reports(self):
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        code, out, _ = self.run_cli("intend", "--description", "heat to 68", "--due", due, "--zone", "main",
                                    "--temperature", "68")
        self.assertEqual((code, json.loads(out)[0]["status"]), (0, "pending"))
        code, out, _ = self.run_cli("tick")
        self.assertEqual(code, 0)
        self.assertIn("Done as asked: heat to 68", out)
        calls = len(self.fake.calls)
        self.assertEqual(self.run_cli("tick"), (0, "", ""))
        self.assertEqual(len(self.fake.calls), calls)
        code, out, _ = self.run_cli("intentions")
        self.assertEqual(json.loads(out)["intentions"][0]["status"], "completed")

    def test_tick_with_nothing_pending_does_not_contact_home_assistant(self):
        self.assertEqual(self.run_cli("tick"), (0, "", ""))
        self.assertEqual(self.fake.reads, [])

    def test_a_period_needs_something_to_go_back_to_and_can_be_cancelled(self):
        due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        until = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        with self.assertRaises(SystemExit):
            self.run_cli("intend", "--description", "x", "--due", due, "--zone", "main", "--temperature", "68",
                         "--until", until)
        code, out, _ = self.run_cli("intend", "--description", "68 this evening", "--due", due, "--zone", "main",
                                    "--temperature", "68", "--until", until, "--revert-temperature", "62")
        made = json.loads(out)
        self.assertEqual([m["must_run"] for m in made], [False, True])
        self.assertEqual(self.run_cli("cancel", made[0]["id"])[0], 0)
        self.assertEqual(self.run_cli("cancel", "nope")[0], 1)

    def test_collect_snapshots_rooms_learns_once_and_does_not_recount_old_episodes(self):
        t0 = datetime.now(timezone.utc) - timedelta(hours=6)

        def at(h):
            return (t0 + timedelta(hours=h)).isoformat()

        hist = {"climate.family_room": [
                    {"last_updated": at(0), "state": "heat", "attributes": {"hvac_action": "heating"}},
                    {"last_updated": at(2), "state": "heat", "attributes": {"hvac_action": "idle"}},
                    {"last_updated": at(5), "state": "heat", "attributes": {"hvac_action": "heating"}}],
                "sensor.family_room_wall_temperature": [
                    {"last_updated": at(-0.1), "state": "60"}, {"last_updated": at(1.9), "state": "64"},
                    {"last_updated": at(4.9), "state": "61"}]}
        asked = []
        self.fake.history = lambda entity, since: (asked.append((entity, since)), hist[entity])[1]
        self.assertEqual(self.run_cli("collect")[0], 0)
        model = json.loads((self.state / "model.json").read_text())["zones"]["family_room"]
        self.assertEqual((model["heating"]["mean_f_per_hour"], model["heating"]["samples"]), (2.0, 1))
        self.assertEqual((model["cooling"]["mean_f_per_hour"], model["cooling"]["samples"]), (1.0, 1))
        self.assertEqual(model["learned_until"], at(5))            # the stretch still in progress is left for next time
        snap = json.loads((self.state / "room-snapshots.jsonl").read_text().splitlines()[0])
        self.assertEqual(snap["temps"], {"basement": 52.0, "zone:main": 61.0, "zone:family_room": 63.2})
        self.assertEqual({e for e, _ in asked}, set(hist))         # the zone without a room sensor is not learned from
        self.assertEqual(self.run_cli("collect")[0], 0)            # same history again an hour later
        again = json.loads((self.state / "model.json").read_text())["zones"]["family_room"]
        self.assertEqual(again["heating"]["samples"], 1)           # v2 re-read 24 hours every hour and recounted
        self.assertEqual(len((self.state / "thermal-episodes.jsonl").read_text().splitlines()), 2)

    def test_collect_reports_a_history_failure_as_a_failed_job(self):
        def boom(entity, since):
            raise ConnectionError("down")
        self.fake.history = boom
        code, _, err = self.run_cli("collect")
        self.assertEqual(code, 1)
        self.assertIn("history unavailable", err)

    def test_missing_settings_stop_with_a_plain_message(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(SystemExit) as e:
            cli.main(["report"])
        self.assertIn("MERRICK_STATE_DIR", str(e.exception))
        (self.state / "house.json").write_text("{}")
        with self.assertRaises(SystemExit) as e:
            self.run_cli("cold-watch")
        self.assertIn("house file missing or empty", str(e.exception))


class Entry(unittest.TestCase):
    def test_the_package_directory_runs_as_a_command_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, "-B", str(ROOT / "overlay" / "merrick"), "--state-dir", d, "report"],
                               capture_output=True, text=True, cwd=d, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "Merrick path log: no requests recorded."), r.stderr)

    def test_the_real_client_refuses_to_start_without_an_address_and_token(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "house.json").write_text(json.dumps(HOUSE))
            r = subprocess.run([sys.executable, "-B", str(ROOT / "overlay" / "merrick"), "--state-dir", d, "cold-watch"],
                               capture_output=True, text=True, cwd=d, env={"PATH": "/usr/bin:/bin"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Home Assistant is not configured", r.stderr)


class Hygiene(unittest.TestCase):
    """The files this step ships must be clean: stdlib only, no personal paths, ids, addresses or guarded words."""

    def files(self):
        out = sorted((ROOT / "overlay" / "merrick").glob("*")) + sorted((ROOT / "overlay" / "workspace-merrick").glob("*"))
        out += sorted((ROOT / "tests").glob("test_merrick_*.py"))
        return [p for p in out if p.is_file()]

    def test_expected_files_exist(self):
        names = {p.name for p in self.files()}
        for need in ("router.py", "pathlog.py", "budget.py", "ha.py", "coldwatch.py", "README.md", "AGENTS.md",
                     "IDENTITY.md", "SOUL.md"):
            self.assertIn(need, names)
        self.assertTrue((ROOT / "docs" / "wiring" / "merrick.md").is_file())

    def test_no_guarded_words_personal_paths_or_private_addresses(self):
        parts = [("alpha", " bot"), ("alpha", "bot"), ("alpha", "_bot"), ("robin", "hood"), ("opti", "plex"),
                 ("brook", "lyn"), ("/home/", "josh")]
        private = re.compile(r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b")
        for p in self.files() + [ROOT / "docs" / "wiring" / "merrick.md"]:
            text = p.read_text()
            low = text.lower()
            with self.subTest(file=p.name):
                if "docs" not in p.parts:
                    for a, b in parts[:6]:
                        self.assertNotIn(a + b, low)
                self.assertNotIn(parts[6][0] + parts[6][1], low)
                self.assertIsNone(private.search(text))
                self.assertIsNone(re.search(r"\b\d{9,}\b", text), "looks like a personal id")

    def test_only_the_standard_library_is_imported(self):
        allowed = set(sys.stdlib_module_names) | {"merrick"}
        for p in (ROOT / "overlay" / "merrick").glob("*.py"):
            for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", p.read_text(), re.M):
                with self.subTest(file=p.name, module=m.group(1)):
                    self.assertIn(m.group(1), allowed)

    def test_no_file_contains_a_network_client(self):
        banned = {"urllib", "http", "socket", "smtplib", "requests", "httpx", "aiohttp", "ssl", "ftplib"}
        for p in (ROOT / "overlay" / "merrick").glob("*.py"):
            found = set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", p.read_text(), re.M))
            self.assertEqual(found & banned, set(), p.name)

    def test_the_repository_direct_send_lint_passes_on_these_files(self):
        sys.path.insert(0, str(ROOT))
        from notifier import lint
        for p in (ROOT / "overlay" / "merrick").glob("*.py"):
            self.assertEqual(lint.scan_source(p.read_text(), p.name, exempt=False), [], p.name)


if __name__ == "__main__":
    unittest.main()
