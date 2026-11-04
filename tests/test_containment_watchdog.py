import unittest
from datetime import datetime, timedelta, timezone

from containment.watchdog import JobRecord, ha_alerts, health_line, stale_jobs

NOW = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def job(name, last, cadence=DAY, enabled=True, created=NOW - 30 * DAY):
    return JobRecord(name, enabled, cadence, last, created)


class Staleness(unittest.TestCase):
    def test_two_x_cadence_boundary(self):
        jobs = [job("at-limit", NOW - 2 * DAY), job("over", NOW - 2 * DAY - timedelta(seconds=1))]
        self.assertEqual([s.name for s in stale_jobs(jobs, NOW)], ["over"])

    def test_disabled_and_one_shot_ignored(self):
        jobs = [job("off", NOW - 99 * DAY, enabled=False), job("once", None, cadence=None)]
        self.assertEqual(stale_jobs(jobs, NOW), [])

    def test_never_succeeded_gets_one_grace_window(self):
        fresh = job("fresh", None, created=NOW - DAY)
        old = job("dead", None, created=NOW - 3 * DAY)
        out = stale_jobs([fresh, old], NOW)
        self.assertEqual([s.name for s in out], ["dead"])
        self.assertIsNone(out[0].age)

    def test_ha_monitor_case_from_host_report(self):
        # ha-monitor.sh last success 2026-10-08 against an hourly cadence: must surface.
        dead = job("ha-monitor", datetime(2026, 10, 8, 14, 37, tzinfo=timezone.utc),
                   cadence=timedelta(hours=1))
        self.assertEqual([s.name for s in stale_jobs([dead], NOW)], ["ha-monitor"])

    def test_naive_now_and_bad_cadence_rejected(self):
        with self.assertRaises(ValueError):
            stale_jobs([], NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            stale_jobs([job("x", NOW, cadence=timedelta(0))], NOW)


class Health(unittest.TestCase):
    def test_line_always_present(self):
        self.assertTrue(health_line([], [], 24).startswith("Health: OK"))

    def test_attention_line(self):
        stale = stale_jobs([job("brief", NOW - 3 * DAY)], NOW)
        line = health_line(stale, ["catskills"], 24)
        self.assertIn("brief stale (last success 3d0h ago, limit 2d0h)", line)
        self.assertIn("HA catskills unreachable", line)

    def test_ha_two_misses(self):
        hist = {"a": [True, False, False], "b": [False, True], "c": [False]}
        self.assertEqual(ha_alerts(hist), ["a"])


if __name__ == "__main__":
    unittest.main()
