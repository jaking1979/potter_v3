"""Tests for overlay/tools/qc (nightly quality control). No network, no model, no real scheduler."""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay/tools"))
import schedule_watch as sw  # noqa: E402
from qc import common, recorder, verifier  # noqa: E402

NOW = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)           # 01:00 in New York
NOW_MS = int(NOW.timestamp() * 1000)
DAY = "2026-11-16"


def msg(role, text, ts=NOW_MS - 3_600_000):
    return json.dumps({"type": "message", "timestamp": ts, "message": {"role": role, "content": [
        {"type": "text", "text": text}, {"type": "tool_use", "name": "x"}]}})


def job(name, **state):
    return {"id": "id-" + name, "name": name, "enabled": True, "state": state}


OK_JOBS = [job("potter-morning-brief", lastStatus="ok", nextRunAtMs=NOW_MS + 60_000)]


class FakeRun:
    """Stands in for subprocess.run: answers the scheduler, git and message commands, records every call."""

    def __init__(self, jobs=OK_JOBS, git="", send_rc=0, cron_rc=0):
        self.jobs, self.git, self.send_rc, self.cron_rc, self.calls = jobs, git, send_rc, cron_rc, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:1] == ["git"]:
            return SimpleNamespace(returncode=0, stdout=self.git, stderr="")
        if "cron" in argv:
            return SimpleNamespace(returncode=self.cron_rc, stdout=json.dumps({"jobs": self.jobs}), stderr="")
        if "message" in argv:
            return SimpleNamespace(returncode=self.send_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def sent(self):
        return [c[c.index("--message") + 1] for c in self.calls if "message" in c and "send" in c]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.sessions, self.state, self.ws = self.tmp / "sessions", self.tmp / "qc", self.tmp / "ws"
        for d in (self.sessions, self.ws / "notes"):
            d.mkdir(parents=True)

    def session(self, *lines, name="s1.jsonl"):
        p = self.sessions / name
        p.write_text("\n".join(lines) + "\n")
        os.utime(p, (NOW.timestamp() - 600, NOW.timestamp() - 600))
        return p

    def record(self, *extra, model=None):
        out = io.StringIO()
        rc = recorder.main(["--sessions", str(self.sessions), "--state", str(self.state), *extra],
                           now=NOW, model=model, out=out)
        return rc, out.getvalue()

    def verify(self, *extra, run=None):
        out, run = io.StringIO(), run or FakeRun()
        rc = verifier.main(["--state", str(self.state), "--workspace", str(self.ws), *extra],
                           now=NOW, run=run, out=out)
        return rc, out.getvalue(), run

    def claims(self, day=DAY):
        return common.load_jsonl(self.state / "claims" / f"{day}.jsonl")


class Common(Sandbox):
    def test_recorder_and_verifier_audit_the_same_day(self):
        # 00:30 and 01:00 local on the 17th both audit the 16th; a late run on the 16th UTC evening audits the 15th.
        self.assertEqual(common.audit_date(datetime(2026, 11, 17, 5, 30, tzinfo=timezone.utc), "America/New_York"), DAY)
        self.assertEqual(common.audit_date(NOW, "America/New_York"), DAY)
        self.assertEqual(common.audit_date(datetime(2026, 11, 17, 4, 59, tzinfo=timezone.utc), "America/New_York"),
                         "2026-11-15")

    def test_timestamps_in_ms_seconds_and_iso_are_understood(self):
        self.assertEqual(common.to_ms(1_800_000_000_000), 1_800_000_000_000)
        self.assertEqual(common.to_ms(1_800_000_000), 1_800_000_000_000)
        self.assertEqual(common.to_ms("2026-11-17T06:00:00Z"), NOW_MS)
        self.assertIsNone(common.to_ms("yesterday"))
        self.assertIsNone(common.to_ms(None))

    def test_messages_are_filtered_by_window_and_only_text_is_kept(self):
        p = self.session(msg("assistant", "inside"), msg("assistant", "too old", ts=NOW_MS - 10**9),
                         msg("user", "no stamp", ts=None), "not json", json.dumps({"type": "tool", "x": 1}),
                         json.dumps({"type": "message", "message": {"role": "assistant", "content": "plain"}}))
        got = common.extract_messages(p, NOW_MS - 7_200_000, NOW_MS)
        self.assertEqual([(r, t) for _, r, t in got], [("assistant", "inside"), ("user", "no stamp"),
                                                       ("assistant", "plain")])
        self.assertEqual(common.extract_messages(self.tmp / "missing.jsonl"), [])

    def test_only_recent_session_files_are_read_and_trajectories_are_not(self):
        new = self.session(msg("user", "x"))
        old = self.session(msg("user", "x"), name="old.jsonl")
        os.utime(old, (1, 1))
        self.session("{}", name="s1.trajectory.jsonl")
        (self.sessions / "notes.txt").write_text("x")
        got = common.find_session_files([self.sessions, self.tmp / "nope"], NOW.timestamp() - 3600)
        self.assertEqual(got, [new])

    def test_chunks_are_bounded_and_split_between_messages(self):
        chunks = common.chunk_transcript([(0, "user", "a" * 60), (0, "assistant", "b" * 60), (0, "user", "c" * 500)], 120)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 120 for c in chunks))

    def test_model_is_optional_and_injected(self):
        self.assertIsNone(common.make_model(None))
        self.assertIsNone(common.make_model("  "))
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"payloads": [{"text": " hi "}]}))
        ask = common.make_model("fake-agent --model 'm one' -m", run=run)
        self.assertEqual(ask("PROMPT"), "hi")
        self.assertEqual(seen[0], ["fake-agent", "--model", "m one", "-m", "PROMPT"])
        self.assertIsNone(common.make_model("x", run=lambda a, **k: SimpleNamespace(returncode=2, stdout="boom"))("p"))

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        self.assertIsNone(common.make_model("x", run=missing)("p"))
        self.assertEqual(common.model_output_text("plain words"), "plain words")
        self.assertIsNone(common.model_output_text(json.dumps({"payloads": [{"text": ""}]})))


class Recorder(Sandbox):
    def test_without_a_model_concrete_claims_are_still_recorded_silently(self):
        self.session(msg("assistant", "Done. I saved the summary to `notes/trip-plan.md`. I think it reads well."),
                     msg("assistant", 'I\'ve also set a reminder called "boiler service" for Monday.'),
                     msg("assistant", "I created a job \"weekly-digest\" to run on Fridays."),
                     msg("user", "I saved my own file to `notes/mine.md`."),
                     msg("assistant", "I will look into it. I updated my thinking."))
        rc, out = self.record()
        self.assertEqual((rc, out), (0, ""))
        got = {c["category"]: c for c in self.claims()}
        self.assertEqual(sorted(got), ["reminder_set", "saved", "scheduled"])
        self.assertEqual(got["saved"]["hint"], "notes/trip-plan.md")
        self.assertEqual(got["saved"]["extractor"], "rules")
        self.assertEqual(got["saved"]["status"], "pending")
        self.assertEqual(got["saved"]["source"], "s1.jsonl")              # a file name, never a full path
        self.assertIn("recorder: day=2026-11-16", (self.state / "logs/qc.log").read_text())

    def test_running_twice_adds_nothing_new(self):
        self.session(msg("assistant", "I saved it to `notes/a.md`."))
        self.record()
        self.record()
        self.assertEqual(len(self.claims()), 1)

    def test_dry_run_writes_nothing(self):
        self.session(msg("assistant", "I saved it to `notes/a.md`."))
        rc, out = self.record("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("1 new claim(s)", out)
        self.assertFalse(self.state.exists())

    def test_an_injected_model_is_used_and_its_output_is_sanitised(self):
        self.session(msg("assistant", "Filed the article."))
        prompts = []

        def model(prompt):
            prompts.append(prompt)
            return 'Here you go:\n[{"claim": "filed the heat pump article", "category": "filed", ' \
                   '"verification_hint": "library has heat pump entry", "confidence": "high"},' \
                   ' {"claim": "made up kind", "category": "DROP TABLE", "confidence": 7}, {"claim": "x"}, "junk"]'
        rc, out = self.record(model=model)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("Filed the article.", prompts[0])
        got = self.claims()
        self.assertEqual([(c["category"], c["confidence"], c["extractor"]) for c in got],
                         [("filed", "high", "model"), ("other", "low", "model")])

    def test_a_failing_model_falls_back_to_the_built_in_extractor(self):
        self.session(msg("assistant", "I wrote the plan to `notes/plan.md`."))
        for i, reply in enumerate((None, "sorry, no json here", "[not json]", '{"claim": "not a list"}')):
            with self.subTest(reply=reply):
                self.state = self.tmp / f"qc{i}"
                rc, out = self.record(model=lambda prompt, r=reply: r)
                self.assertEqual((rc, out), (0, ""))
                self.assertEqual([(c["hint"], c["extractor"]) for c in self.claims()], [("notes/plan.md", "rules")])
        self.assertEqual(recorder.parse_model_claims("[]"), [])

    def test_the_number_of_claims_per_night_is_bounded(self):
        self.session(*[msg("assistant", f"I saved file {i} to `notes/f{i}.md`.") for i in range(30)])
        self.record("--max-claims", "7")
        self.assertEqual(len(self.claims()), 7)

    def test_no_sessions_is_not_an_error(self):
        rc, out = self.record()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.claims(), [])


class Evidence(Sandbox):
    def test_keywords_drop_filler_and_short_words(self):
        self.assertEqual(verifier.keywords("Set a reminder for the boiler service on Mon; boiler!"), ["boiler", "service"])

    def test_only_safe_relative_paths_are_taken_from_a_claim(self):
        text = "saved `notes/a.md` and docs/x-*.md, not /etc/passwd, `../../secret.txt`, `~/x.md`, https://a.b/c.md or `two words`"
        self.assertEqual(verifier.explicit_paths(text), ["notes/a.md", "docs/x-*.md"])

    def test_file_evidence(self):
        (self.ws / "notes/trip-plan.md").write_text("x")
        (self.ws / "notes/2026-boiler-quote.md").write_text("x")
        (self.ws / ".git").mkdir()
        (self.ws / ".git/heatpump.md").write_text("x")
        f = lambda t: verifier.check_file(t, self.ws)[0]                    # noqa: E731
        self.assertEqual(f("saved the summary to `notes/trip-plan.md`"), "FOUND")
        self.assertEqual(f("saved to notes/trip-*.md"), "FOUND")
        self.assertEqual(f("wrote up the boiler quote"), "FOUND")           # by a distinctive word in a file name
        self.assertEqual(f("saved `notes/missing.md`"), "NOT_FOUND")
        self.assertEqual(f("saved the heatpump notes"), "NOT_FOUND")        # .git is not evidence
        self.assertEqual(f("did it"), "UNKNOWN")
        self.assertEqual(verifier.check_file("saved `notes/trip-plan.md`", None)[0], "UNKNOWN")
        self.assertEqual(verifier.check_file("saved `notes/trip-plan.md`", self.tmp / "gone")[0], "UNKNOWN")

    def test_a_path_cannot_escape_the_workspace(self):
        (self.tmp / "outside.md").write_text("x")
        os.symlink(self.tmp / "outside.md", self.ws / "notes/link.md")
        self.assertEqual(verifier.check_file("saved `notes/link.md`", self.ws)[0], "NOT_FOUND")

    def test_cron_evidence_is_matched_per_job(self):
        jobs = [{"name": "potter-boiler-reminder", "payload": {"message": "Remind the owner: boiler service"}},
                {"name": "potter-morning-brief", "message": "Run the morning-brief skill"}, "junk"]
        self.assertEqual(verifier.check_cron("set a reminder for the boiler service", jobs)[0], "FOUND")
        self.assertEqual(verifier.check_cron("scheduled the dentist follow-up", jobs)[0], "NOT_FOUND")
        self.assertEqual(verifier.check_cron("scheduled it", jobs)[0], "UNKNOWN")
        self.assertEqual(verifier.check_cron("boiler", None)[0], "UNKNOWN")
        # stricter matching is available: two distinctive words must land in the same job
        self.assertEqual(verifier.check_cron("boiler dentist reminder", jobs, min_hits=2)[0], "NOT_FOUND")

    def test_each_kind_of_claim_gets_the_right_verdict(self):
        (self.ws / "notes/trip-plan.md").write_text("x")
        ev = verifier.Evidence(workspace=self.ws, jobs=[{"name": "potter-boiler-reminder"}],
                               git_log="abc123 rework the pantry inventory script", library=None)
        v = lambda cat, claim, hint="": verifier.verify_claim({"category": cat, "claim": claim, "hint": hint}, ev)[0]  # noqa: E731
        self.assertEqual(v("saved", "saved the trip plan", "notes/trip-plan.md"), "verified")
        self.assertEqual(v("saved", "saved the garden layout", "notes/garden.md"), "not_done")
        self.assertEqual(v("built", "built the pantry inventory script"), "verified")            # via commits
        self.assertEqual(v("reminder_set", "set a reminder for the boiler"), "verified")
        self.assertEqual(v("scheduled", "scheduled the dentist follow-up"), "not_done")
        self.assertEqual(v("filed", "filed the heat pump article"), "not_done")                 # no library: files say no
        self.assertEqual(v("will_do", "will call the plumber"), "pending")
        self.assertEqual(v("sent", "sent the email to the plumber"), "unverified")              # nothing to check against
        self.assertEqual(v("other", "something"), "unverified")
        self.assertEqual(v("no-such-kind", "something"), "unverified")
        none = verifier.Evidence()
        self.assertEqual(verifier.verify_claim({"category": "saved", "claim": "saved the trip plan"}, none)[0], "unverified")
        lib = verifier.Evidence(library="Heat Pump Buying Guide | article")
        self.assertEqual(verifier.verify_claim({"category": "filed", "claim": "filed the heat pump article"}, lib)[0], "verified")


class Schedules(unittest.TestCase):
    def test_the_watchdogs_own_evaluate_is_reused_not_copied(self):
        self.assertIs(verifier.schedule_watch.evaluate, sw.evaluate)
        self.assertNotIn("def evaluate", (ROOT / "overlay/tools/qc/verifier.py").read_text())

    def test_failed_and_missed_jobs_are_found_and_healthy_or_disabled_ones_are_not(self):
        off = job("off", lastStatus="error")
        off["enabled"] = False
        jobs = OK_JOBS + [job("boom", lastStatus="error", lastError="exit 3", lastRunAtMs=1),
                          job("late", lastStatus="ok", nextRunAtMs=NOW_MS - 3_600_000), off]
        got = {f["name"]: f["kind"] for f in verifier.schedule_findings(jobs, NOW_MS)}
        self.assertEqual(got, {"boom": "failed", "late": "missed"})
        self.assertEqual(verifier.schedule_findings(OK_JOBS, NOW_MS), [])

    def test_the_watchdog_job_itself_is_checked_here(self):
        dead = job(sw.SELF, lastStatus="ok", nextRunAtMs=NOW_MS - 7_200_000)
        self.assertEqual(sw.evaluate([dead], NOW_MS), [])                    # it cannot see itself
        got = verifier.schedule_findings([dead], NOW_MS)
        self.assertEqual([(f["name"], f["kind"]) for f in got], [("schedule watchdog", "missed")])

    def test_an_unreadable_job_list_is_itself_a_finding(self):
        got = verifier.schedule_findings(None, NOW_MS)
        self.assertEqual(len(got), 1)
        self.assertIn("could not read the job list", got[0]["detail"])


class Report(unittest.TestCase):
    def test_nothing_wrong_renders_nothing(self):
        self.assertEqual(verifier.render(DAY, [], []), "")

    def test_report_is_plain_text_and_bounded(self):
        claims = [{"category": "saved", "claim": "c" * 900, "detail": "d" * 900} for _ in range(40)]
        sched = [{"name": f"job{i}", "kind": "failed", "detail": "x" * 900} for i in range(40)]
        text = verifier.render(DAY, claims, sched, "/state/followup.jsonl")
        self.assertLessEqual(len(text), verifier.REPORT_MAX_CHARS)
        self.assertIn("80 item(s) need attention", text)
        self.assertEqual(text.count("- [saved]"), 10)
        self.assertIn("...and 30 more", text)
        self.assertTrue(text.isascii())
        self.assertNotIn("NO_REPLY", text)
        small = verifier.render(DAY, claims[:1], sched[:1], "/state/followup.jsonl")
        self.assertIn("Schedules (1):", small)
        self.assertIn("- FAILED: job0 - ", small)
        self.assertIn("Follow-ups: /state/followup.jsonl", small)


class Nightly(Sandbox):
    def seed(self, *texts):
        self.session(*[msg("assistant", t) for t in texts])
        self.record()

    def test_everything_verified_means_silence_and_exit_0(self):
        (self.ws / "notes/trip-plan.md").write_text("x")
        self.seed("I saved the summary to `notes/trip-plan.md`.")
        rc, out, run = self.verify("--to", "111")
        self.assertEqual((rc, out, run.sent()), (0, "", []))
        row = common.load_jsonl(self.state / "verify" / f"{DAY}.jsonl")[0]
        self.assertEqual((row["status"], row["recheck_count"]), ("verified", 1))
        self.assertFalse((self.state / "followup").exists())

    def test_no_claims_and_healthy_schedules_is_silent_too(self):
        rc, out, run = self.verify("--to", "111")
        self.assertEqual((rc, out, run.sent()), (0, "", []))

    def test_one_report_covers_unsupported_claims_and_schedules(self):
        self.seed("I saved the summary to `notes/trip-plan.md`.", "I wrote the budget to `notes/budget.md`.")
        (self.ws / "notes/trip-plan.md").write_text("x")
        run = FakeRun(jobs=OK_JOBS + [job("potter-evening-nudge", lastStatus="error", lastError="exit 2", lastRunAtMs=5)])
        rc, out, run = self.verify("--to", "111", "--openclaw", "openclaw --profile p", run=run)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(len(run.sent()), 1)
        text = run.sent()[0]
        self.assertIn("2 item(s) need attention", text)
        self.assertIn("notes/budget.md", text)
        self.assertNotIn("trip-plan", text)
        self.assertIn("- FAILED: potter-evening-nudge - exit 2", text)
        send = [c for c in run.calls if "message" in c][0]
        self.assertEqual(send[:5], ["openclaw", "--profile", "p", "message", "send"])
        self.assertEqual(send[send.index("--target") + 1], "111")
        fu = common.load_jsonl(self.state / "followup" / f"{DAY}.jsonl")
        self.assertEqual([(f["status"], f["category"]) for f in fu], [("open", "saved")])

    def test_schedule_problem_alone_is_reported(self):
        run = FakeRun(jobs=[job("potter-morning-brief", lastStatus="ok", nextRunAtMs=NOW_MS - 86_400_000)])
        rc, out, run = self.verify("--to", "111", run=run)
        self.assertEqual(rc, 0)
        self.assertIn("DID NOT RUN: potter-morning-brief", run.sent()[0])
        self.assertNotIn("Follow-ups", run.sent()[0])

    def test_scheduler_unreachable_is_reported_not_swallowed(self):
        rc, out, run = self.verify("--to", "111", run=FakeRun(cron_rc=1))
        self.assertIn("could not read the job list", run.sent()[0])

    def test_without_a_recipient_or_in_dry_run_the_report_is_printed(self):
        self.seed("I wrote the budget to `notes/budget.md`.")
        rc, out, run = self.verify()
        self.assertEqual(rc, 0)
        self.assertIn("notes/budget.md", out)
        self.assertEqual(run.sent(), [])
        rc, out, run = self.verify("--to", "111", "--dry-run", "--date", "2026-11-16")
        self.assertIn("notes/budget.md", out)
        self.assertEqual(run.sent(), [])

    def test_failed_delivery_fails_the_job(self):
        self.seed("I wrote the budget to `notes/budget.md`.")
        rc, out, run = self.verify("--to", "111", run=FakeRun(send_rc=1))
        self.assertEqual(rc, 1)

    def test_job_list_can_come_from_a_file(self):
        jf = self.tmp / "jobs.json"
        jf.write_text(json.dumps([job("x", lastStatus="timeout")]))
        rc, out, run = self.verify("--jobs-file", str(jf))
        self.assertIn("FAILED: x", out)
        self.assertFalse(any("cron" in c for c in run.calls))

    def test_wake_command_is_optional_and_only_used_for_unsupported_claims(self):
        self.seed("I wrote the budget to `notes/budget.md`.")
        rc, out, run = self.verify("--to", "111", "--wake-cmd", "wake --session main -m")
        wake = [c for c in run.calls if c[0] == "wake"]
        self.assertEqual(len(wake), 1)
        self.assertIn("notes/budget.md", wake[0][-1])
        rc, out, run = self.verify("--to", "111", "--date", "2020-01-01", "--wake-cmd", "wake -m",
                                   run=FakeRun(jobs=[job("x", lastStatus="error")]))
        self.assertEqual([c for c in run.calls if c[0] == "wake"], [])

    def test_hourly_recheck_is_silent_and_closes_what_got_fixed(self):
        self.seed("I wrote the budget to `notes/budget.md`.", "I saved the list to `notes/list.md`.")
        self.verify("--to", "111")
        (self.ws / "notes/budget.md").write_text("now it exists")
        rc, out, run = self.verify("--mode", "recheck", "--to", "111")
        self.assertEqual((rc, out, run.sent()), (0, "", []))
        fu = {f["claim"][:20]: f["status"] for f in common.load_jsonl(self.state / "followup" / f"{DAY}.jsonl")}
        self.assertEqual(sorted(fu.values()), ["open", "resolved"])
        rows = {r["hint"]: r for r in common.load_jsonl(self.state / "verify" / f"{DAY}.jsonl")}
        self.assertEqual((rows["notes/budget.md"]["status"], rows["notes/budget.md"]["recheck_count"]), ("verified", 2))
        self.assertEqual(rows["notes/list.md"]["status"], "not_done")
        # nothing open any more -> the next recheck touches nothing, not even the scheduler
        (self.ws / "notes/list.md").write_text("x")
        self.verify("--mode", "recheck")
        rc, out, run = self.verify("--mode", "recheck")
        self.assertEqual((rc, out, run.calls), (0, "", []))

    def test_recheck_gives_up_after_a_day_of_retries(self):
        self.seed("I wrote the budget to `notes/budget.md`.")
        self.verify()
        for _ in range(verifier.MAX_RECHECKS + 5):
            self.verify("--mode", "recheck")
        row = common.load_jsonl(self.state / "verify" / f"{DAY}.jsonl")[0]
        self.assertEqual(row["recheck_count"], verifier.MAX_RECHECKS)

    def test_recheck_with_no_state_at_all_is_a_quiet_no_op(self):
        rc, out, run = self.verify("--mode", "recheck")
        self.assertEqual((rc, out, run.calls), (0, "", []))


class Hygiene(unittest.TestCase):
    FILES = sorted((ROOT / "overlay/tools/qc").glob("*.py")) + [ROOT / "overlay/workspace/rules/global/quality-control.md"]

    def test_no_home_paths_ids_or_named_services_in_the_shipped_files(self):
        self.assertGreaterEqual(len(self.FILES), 5)
        banned = ["/home/", "~/.openclaw", "expanduser", "api_key", "apikey", "no_reply"] + \
                 [a + b for a, b in (("alpha", " bot"), ("alpha", "bot"), ("alpha", "_bot"), ("robin", "hood"),
                                     ("opti", "plex"), ("brook", "lyn"))]
        patterns = [re.escape(w) for w in banned] + [r"\d{8,}"]                  # no chat ids or account numbers
        for p in self.FILES:
            low = p.read_text().lower()
            for pat in patterns:
                hit = re.search(pat, low)
                with self.subTest(file=p.name, pattern=pat):
                    self.assertIsNone(hit and hit.group(0))

    def test_tools_use_only_the_standard_library(self):
        allowed = {"__future__", "argparse", "fnmatch", "hashlib", "json", "os", "re", "shlex", "subprocess", "sys",
                   "datetime", "pathlib", "zoneinfo", "schedule_watch", "qc"}
        import ast
        for p in self.FILES[:-1]:
            for node in ast.walk(ast.parse(p.read_text())):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        [node.module] if isinstance(node, ast.ImportFrom) else []
                for n in names:
                    with self.subTest(file=p.name, module=n):
                        self.assertIn(n.split(".")[0], allowed)


if __name__ == "__main__":
    unittest.main()
