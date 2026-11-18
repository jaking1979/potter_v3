"""House insights: nothing is proposed until each detector has the evidence it declares it needs."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import insights  # noqa: E402
from merrick.testing import HOUSE  # noqa: E402

T0 = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class Insights(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def runs(self, n, outdoor, rate=2.0, zone="family_room", start=T0, days_apart=1):
        snaps, eps = [], []
        for i in range(n):
            t = start + timedelta(days=i * days_apart)
            snaps.append({"ts": t.isoformat(), "temps": {"basement": 50.0}, "outdoor_f": outdoor})
            eps.append({"zone": zone, "type": "active_heating", "start_time": t.isoformat(),
                        "end_time": (t + timedelta(hours=1)).isoformat(), "rate_f_per_hour": rate})
        return snaps, eps

    def test_no_cold_data_means_no_cold_proposal_and_says_how_far_along_it_is(self):
        snaps, eps = self.runs(6, 38.0)
        cold_s, cold_e = self.runs(2, 20.0, rate=1.2, start=T0 + timedelta(days=10))
        write(self.state / "room-snapshots.jsonl", snaps + cold_s)
        write(self.state / "thermal-episodes.jsonl", eps + cold_e)
        rep = insights.analyse(self.state, HOUSE)
        ready = [f for f in rep["ready"] if f["detector"] == "preheat"]
        self.assertEqual([(f["zone"], f["outdoor"]) for f in ready], [("family_room", "35-45 F")])
        cold = [w for w in rep["waiting"] if w["detector"] == "preheat" and w["zone"] == "family_room" and w["outdoor"] == "15-25 F"]
        self.assertEqual((cold[0]["have"], cold[0]["need"]), (2, 5))
        text = insights.render(rep)
        self.assertIn("warms about 2.0 F an hour (6 heating runs)", text)
        self.assertIn("15-25 F (0 of 5", text)                      # main zone has none there; the lowest count is shown
        self.assertEqual(rep["coldest_seen_f"], 20.0)

    def test_everything_waits_on_an_empty_state_and_nothing_is_invented(self):
        rep = insights.analyse(self.state, HOUSE)
        self.assertEqual(rep["ready"], [])
        self.assertIn("Nothing the data can support yet.", insights.render(rep))
        self.assertTrue({"vacant_overheat", "never_reaches", "slowing", "repeats", "sensor_gaps"} <= {w["detector"] for w in rep["waiting"]})

    def test_slowing_needs_four_weeks_then_flags_a_drop(self):
        snaps, fast = self.runs(6, 38.0, rate=2.0, days_apart=2)
        s2, slow = self.runs(6, 38.0, rate=1.4, start=T0 + timedelta(days=30), days_apart=2)
        write(self.state / "room-snapshots.jsonl", snaps + s2)
        write(self.state / "thermal-episodes.jsonl", fast + slow)
        found = [f for f in insights.analyse(self.state, HOUSE)["ready"] if f["detector"] == "slowing"]
        self.assertEqual(found[0]["zone"], "family_room")
        self.assertIn("30% slower", found[0]["text"])

    def test_repeated_manual_requests_become_an_automation_candidate(self):
        rows = [{"ts": (T0 + timedelta(days=i * 7)).isoformat(), "intent": "set_temperature", "zone": "main", "temperature": 68,
                 "outcome": "ok"} for i in range(3)]
        write(self.state / "path-log.jsonl", rows)
        found = [f for f in insights.analyse(self.state, HOUSE)["ready"] if f["detector"] == "repeats"]
        self.assertIn("3 times", found[0]["text"])


if __name__ == "__main__":
    unittest.main()
