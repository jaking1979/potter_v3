import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick.budget import Budget, BudgetExceeded, PartialReport, run_with_budget  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class BudgetTest(unittest.TestCase):
    def test_remaining_and_expiry_follow_the_clock(self):
        c = Clock()
        b = Budget(90, clock=c)
        self.assertEqual((b.remaining(), b.expired()), (90.0, False))
        b.check()
        c.t += 89.9
        self.assertFalse(b.expired())
        c.t += 0.1
        self.assertEqual((b.remaining(), b.expired()), (0.0, True))
        with self.assertRaises(BudgetExceeded):
            b.check()
        c.t += 500
        self.assertEqual(b.remaining(), 0.0)


class Report(unittest.TestCase):
    def test_every_line_is_labelled(self):
        r = PartialReport()
        r.observe("basement 41.0 F at 14:02")
        r.estimate("reaches 38 F in about 6 hours at the present rate")
        r.suppose("wind may be raising heat loss (2 samples)")
        r.need("whether the main zone is heating")
        text = r.render()
        self.assertTrue(text.startswith("Stopped early"))
        self.assertIn("- Observed: basement 41.0 F at 14:02", text)
        self.assertIn("- Estimated: reaches 38 F", text)
        self.assertIn("- Hypothesis: wind may", text)
        self.assertIn("- Unknown: whether the main zone is heating", text)

    def test_a_need_stays_unknown_until_resolved_and_duplicates_collapse(self):
        r = PartialReport()
        r.need("outdoor temperature")
        r.need("outdoor temperature")
        self.assertEqual(r.unknown, ["outdoor temperature"])
        r.resolve("outdoor temperature")
        self.assertEqual(r.unknown, [])

    def test_an_empty_stopped_report_says_nothing_was_established(self):
        self.assertIn("Unknown: nothing was established", PartialReport().render())


class Run(unittest.TestCase):
    def test_all_steps_run_when_there_is_time(self):
        rep = run_with_budget([("read basement", lambda r: r.observe("basement 52 F")),
                               ("read main zone", lambda r: r.observe("main 61 F"))], Budget(5))
        self.assertTrue(rep.complete)
        self.assertEqual(rep.observed, ["basement 52 F", "main 61 F"])
        self.assertTrue(rep.render().startswith("Finished."))

    def test_steps_after_the_deadline_are_not_started_and_are_listed_as_unknown(self):
        c = Clock()
        b = Budget(10, clock=c)

        def slow(r):
            r.observe("basement 52 F")
            c.t += 11                                     # this step used up the whole budget

        ran = []
        rep = run_with_budget([("read basement", slow), ("read history", lambda r: ran.append(1)),
                               ("compare", lambda r: ran.append(2))], b)
        self.assertEqual(ran, [])
        self.assertFalse(rep.complete)
        self.assertEqual(rep.observed, ["basement 52 F"])
        self.assertEqual(rep.unknown, ["read history (not started)", "compare (not started)"])
        self.assertIn("time budget of 10s reached", rep.render())

    def test_a_step_that_hangs_is_abandoned_at_the_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(r):
            r.observe("started looking")
            release.wait(30)
            r.observe("this arrives too late to change the report")

        t0 = time.monotonic()
        rep = run_with_budget([("first", lambda r: r.observe("main 61 F")), ("history", hang),
                               ("never", lambda r: r.observe("no"))], Budget(0.15))
        self.assertLess(time.monotonic() - t0, 2.0)       # returned at the deadline, not after the hang
        self.assertFalse(rep.complete)
        self.assertEqual(rep.observed, ["main 61 F", "started looking"])
        self.assertEqual(rep.unknown, ["history (still running when time ran out)", "never (not started)"])
        release.set()
        time.sleep(0.05)
        self.assertEqual(rep.observed, ["main 61 F", "started looking"])    # the report already given is frozen

    def test_a_failing_step_is_reported_as_unknown_and_the_rest_still_run(self):
        rep = run_with_budget([("history", lambda r: 1 / 0), ("read", lambda r: r.observe("main 61 F"))], Budget(5))
        self.assertTrue(rep.complete)
        self.assertEqual(rep.unknown, ["history (failed: ZeroDivisionError)"])
        self.assertEqual(rep.observed, ["main 61 F"])


if __name__ == "__main__":
    unittest.main()
