import tempfile
import threading
import unittest
from pathlib import Path

from model_broker.broker import Broker, Candidate, Caps, Refused, Request


def req(call="c1", task="t1", privacy="private", day="2026-11-06", month="2026-11"):
    return Request(call, task, "research", privacy, day, month)


def candidate(model="local", expected=4, worst=10, **kwargs):
    return Candidate(model, "synthetic", kwargs.get("approved", True), frozenset({"research"}),
                     frozenset(kwargs.get("privacy", {"private"})), expected, worst,
                     kwargs.get("available", True))


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "broker.sqlite"
        self.caps = Caps(50, 20, 30, 60)
        self.b = Broker(self.path, self.caps)

    def test_selects_cheapest_validated_approved_private_available(self):
        selected = self.b.reserve(req(), [candidate("badprivacy", 1, 2, privacy={"public"}),
                                          candidate("offline", 0, 1, available=False),
                                          candidate("unapproved", 0, 1, approved=False),
                                          candidate("large", 8, 10), candidate("local", 3, 10)])
        self.assertEqual(selected.model, "local")

    def test_reservation_survives_restart_and_unknown_cost(self):
        self.b.reserve(req(), [candidate()])
        self.b.dispatch_once("c1")
        b2 = Broker(self.path, self.caps)
        with self.assertRaises(Refused): b2.settle("c1", None)
        with self.assertRaises(Refused): b2.dispatch_once("c1")
        b2.reserve(req("c2"), [candidate()])
        with self.assertRaisesRegex(Refused, "budget_ceiling"):
            b2.reserve(req("c3"), [candidate(worst=1, expected=1)])
        self.assertEqual(b2.status("c1")[-1], "dispatched")

    def test_settle_frees_unused_reservation_and_prevents_replay(self):
        self.b.reserve(req(), [candidate()]); self.b.dispatch_once("c1"); self.b.settle("c1", 3)
        self.b.reserve(req("c2"), [candidate(worst=17)])
        with self.assertRaises(Refused): self.b.settle("c1", 3)
        with self.assertRaises(Refused): self.b.reserve(req(), [candidate()])

    def test_overrun_blocks_subsequent_calls(self):
        self.b.reserve(req(), [candidate()]); self.b.dispatch_once("c1")
        with self.assertRaisesRegex(Refused, "cost_variance"): self.b.settle("c1", 11)
        with self.assertRaisesRegex(Refused, "unresolved_cost_variance"):
            self.b.reserve(req("c2"), [candidate()])

    def test_concurrent_reservations_cannot_overspend(self):
        barrier = threading.Barrier(2); outcomes = []
        def reserve(call):
            b = Broker(self.path, self.caps); barrier.wait()
            try: b.reserve(req(call), [candidate(worst=15)]); outcomes.append("ok")
            except Refused: outcomes.append("refused")
        threads = [threading.Thread(target=reserve, args=(x,)) for x in ("c1", "c2")]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(outcomes, ["ok", "refused"])

    def test_each_cap_is_checked(self):
        for cap in (Caps(9,99,99,99), Caps(99,9,99,99), Caps(99,99,9,99), Caps(99,99,99,9)):
            b = Broker(self.path, cap)
            with self.assertRaisesRegex(Refused, "budget_ceiling"): b.reserve(req(), [candidate()])

    def test_no_implicit_more_expensive_escalation(self):
        with self.assertRaisesRegex(Refused, "budget_ceiling"):
            self.b.reserve(req(), [candidate("cheap",1,21), candidate("expensive",2,10)])


if __name__ == "__main__": unittest.main()
