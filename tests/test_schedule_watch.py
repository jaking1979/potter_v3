import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay/tools"))
import schedule_watch as sw  # noqa: E402

NOW = 10_000_000_000


def job(name, **state):
    return {"id": "id-" + name, "name": name, "enabled": True, "state": state}


class Evaluate(unittest.TestCase):
    def kinds(self, jobs, **kw):
        return {f["name"]: f["kind"] for f in sw.evaluate(jobs, NOW, **kw)}

    def test_healthy_jobs_produce_nothing(self):
        jobs = [job("ok", lastStatus="ok", lastRunAtMs=NOW - 1000, nextRunAtMs=NOW + 60000),
                job("new", nextRunAtMs=NOW + 5),
                job("slightly-late", lastStatus="ok", nextRunAtMs=NOW - 599_000)]
        self.assertEqual(sw.evaluate(jobs, NOW), [])

    def test_failed_missed_undelivered_and_skipped_are_each_found(self):
        jobs = [job("boom", lastStatus="error", lastError="command exited with code 3", consecutiveErrors=4, lastRunAtMs=1),
                job("late", lastStatus="ok", nextRunAtMs=NOW - 3_600_000),
                job("mute", lastStatus="ok", lastDeliveryStatus="failed", nextRunAtMs=NOW + 1, lastRunAtMs=2),
                job("skippy", lastStatus="ok", consecutiveSkipped=3, nextRunAtMs=NOW + 1),
                job("timeout", lastRunStatus="timeout", nextRunAtMs=NOW + 1)]
        self.assertEqual(self.kinds(jobs), {"boom": "failed", "late": "missed", "mute": "undelivered",
                                            "skippy": "skipped", "timeout": "failed"})
        f = {x["name"]: x for x in sw.evaluate(jobs, NOW)}
        self.assertIn("4 times in a row", f["boom"]["detail"])
        self.assertIn("60 minutes", f["late"]["detail"])

    def test_disabled_jobs_and_the_watchdog_itself_are_ignored(self):
        d = job("off", lastStatus="error")
        d["enabled"] = False
        me = job(sw.SELF, lastStatus="error")
        self.assertEqual(sw.evaluate([d, me, "junk", None], NOW), [])

    def test_grace_is_respected_at_the_boundary(self):
        j = [job("edge", lastStatus="ok", nextRunAtMs=NOW - 600_000)]
        self.assertEqual(sw.evaluate(j, NOW), [])
        self.assertEqual(self.kinds(j, grace_ms=599_999), {"edge": "missed"})

    def test_display_name_is_preferred(self):
        j = job("x", lastStatus="error")
        j["displayName"] = "Morning Brief"
        self.assertEqual(sw.evaluate([j], NOW)[0]["name"], "Morning Brief")


class Dedupe(unittest.TestCase):
    def test_a_problem_is_reported_once_then_again_only_if_it_changes_or_returns(self):
        f1 = sw.evaluate([job("boom", lastStatus="error", lastRunAtMs=1)], NOW)
        rep, seen = sw.new_findings(f1, {})
        self.assertEqual(len(rep), 1)
        rep, seen = sw.new_findings(f1, seen)                       # same failure, next tick
        self.assertEqual(rep, [])
        f2 = sw.evaluate([job("boom", lastStatus="error", lastRunAtMs=2)], NOW)   # failed again on a new run
        rep, seen = sw.new_findings(f2, seen)
        self.assertEqual(len(rep), 1)
        rep, seen = sw.new_findings([], seen)                       # recovered
        self.assertEqual(seen, {})
        rep, seen = sw.new_findings(f2, seen)                       # fails later with the same signature
        self.assertEqual(len(rep), 1)

    def test_render_is_plain_and_bounded(self):
        fs = [{"id": str(i), "name": f"job{i}", "kind": "failed", "detail": "x", "sig": "s"} for i in range(20)]
        text = sw.render(fs)
        self.assertIn("20 job(s) need attention", text)
        self.assertIn("...and 5 more", text)
        self.assertEqual(text.count("FAILED"), 15)


if __name__ == "__main__":
    unittest.main()
