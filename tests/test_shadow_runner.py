import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shadow.runner import MAX_BACKOFF_S, Runner  # noqa: E402

NOW = datetime(2026, 11, 23, 8, 30)


class FE:
    def __init__(self, fail_on=()):
        self.recovered, self.handled, self.fail_on = 0, [], set(fail_on)

    def recover(self):
        self.recovered += 1

    def handle_update(self, u):
        if u in self.fail_on:
            raise RuntimeError("secret message text")
        self.handled.append(u)


class Brief:
    def __init__(self, boom=False):
        self.ticks, self.boom = [], boom

    def tick(self, now):
        self.ticks.append(now)
        if self.boom:
            raise RuntimeError("x")


def make(polls, *, fe=None, brief=None, iterations=None):
    fe = fe or FE()
    sleeps, logs = [], []
    it = iter(polls)
    n = {"i": 0}
    limit = iterations if iterations is not None else len(polls)

    def poll():
        n["i"] += 1
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    r = Runner(frontend=fe, brief=brief, poll=poll, now=lambda: NOW, sleep=sleeps.append,
               stop=lambda: len(sleeps) >= limit, log=logs.append)
    return r, fe, sleeps, logs


class T(unittest.TestCase):
    def test_recovers_once_at_start_then_handles_updates_in_order(self):
        r, fe, _, _ = make([[1, 2], [3]])
        r.run()
        self.assertEqual((fe.recovered, fe.handled), (1, [1, 2, 3]))

    def test_one_bad_update_does_not_stop_the_others_or_leak_text(self):
        r, fe, _, logs = make([[1, 2, 3]], fe=FE(fail_on=[2]))
        r.run()
        self.assertEqual(fe.handled, [1, 3])
        self.assertEqual(logs, ["update_failed:RuntimeError"])
        self.assertNotIn("secret", " ".join(logs))

    def test_poll_failure_backs_off_and_recovers(self):
        r, fe, sleeps, logs = make([OSError(), OSError(), [7]])
        r.run()
        self.assertEqual(fe.handled, [7])
        self.assertEqual(logs, ["poll_failed:OSError"] * 2)
        self.assertGreater(sleeps[1], sleeps[0])
        self.assertEqual(sleeps[2], 1.0)          # back to the idle interval after success

    def test_success_resets_the_backoff(self):
        r, _, sleeps, _ = make([OSError(), OSError(), [1], OSError()])
        r.run()
        self.assertEqual(sleeps[3], sleeps[0])

    def test_backoff_is_capped(self):
        r, _, sleeps, _ = make([OSError()] * 20)
        r.run()
        self.assertEqual(max(sleeps), MAX_BACKOFF_S)

    def test_brief_ticks_every_pass_and_its_failure_is_isolated(self):
        b = Brief(boom=True)
        r, fe, _, logs = make([[1], [2]], brief=b)
        r.run()
        self.assertEqual(len(b.ticks), 2)
        self.assertEqual(fe.handled, [1, 2])
        self.assertIn("brief_failed:RuntimeError", logs)

    def test_brief_still_ticks_when_polling_is_down(self):
        b = Brief()
        r, _, _, _ = make([OSError()], brief=b)
        r.run()
        self.assertEqual(b.ticks, [NOW])

    def test_a_failing_recover_does_not_prevent_the_loop(self):
        fe = FE()
        fe.recover = lambda: 1 / 0
        r, _, _, logs = make([[1]], fe=fe)
        r.run()
        self.assertEqual(fe.handled, [1])
        self.assertEqual(logs[0], "recover_failed:ZeroDivisionError")

    def test_stop_is_honoured_between_updates(self):
        fe = FE()
        state = {"n": 0}
        r = Runner(frontend=fe, poll=lambda: [1, 2, 3], now=lambda: NOW, sleep=lambda s: None,
                   stop=lambda: state.__setitem__("n", state["n"] + 1) or state["n"] > 3)
        r.run()
        self.assertEqual(fe.handled, [1, 2])


if __name__ == "__main__":
    unittest.main()
