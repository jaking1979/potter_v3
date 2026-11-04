import unittest
from datetime import datetime, timedelta, timezone

from containment.watchdog import JobRecord, ha_status, health_line, stale_jobs

NOW = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
H = timedelta(hours=1)


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
        out = stale_jobs([job("fresh", None, created=NOW - DAY),
                          job("dead", None, created=NOW - 3 * DAY)], NOW)
        self.assertEqual([s.name for s in out], ["dead"])
        self.assertIsNone(out[0].age)

    def test_ha_monitor_case_from_host_report(self):
        dead = job("ha-monitor", datetime(2026, 10, 8, 14, 37, tzinfo=timezone.utc), cadence=H)
        self.assertEqual([s.name for s in stale_jobs([dead], NOW)], ["ha-monitor"])

    def test_naive_now_and_bad_cadence_rejected(self):
        with self.assertRaises(ValueError):
            stale_jobs([], NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            stale_jobs([job("x", NOW, cadence=timedelta(0))], NOW)


class HAStatus(unittest.TestCase):
    def test_no_observations_is_unknown(self):
        self.assertEqual(ha_status([], H, NOW), "unknown")

    def test_check_that_could_not_run_is_unknown(self):
        self.assertEqual(ha_status([(NOW - H, True), (NOW, None)], H, NOW), "unknown")

    def test_stale_coverage_is_unknown_even_if_last_was_ok(self):
        self.assertEqual(ha_status([(NOW - 3 * H, True)], H, NOW), "unknown")

    def test_one_miss_degraded_two_unreachable(self):
        self.assertEqual(ha_status([(NOW - H, True), (NOW, False)], H, NOW), "degraded")
        self.assertEqual(ha_status([(NOW - H, False), (NOW, False)], H, NOW), "unreachable")
        self.assertEqual(ha_status([(NOW - H, False), (NOW, True)], H, NOW), "reachable")

    def test_order_independent(self):
        self.assertEqual(ha_status([(NOW, False), (NOW - H, False)], H, NOW), "unreachable")


class Health(unittest.TestCase):
    def test_ok_only_with_jobs_and_confirmed_ha(self):
        self.assertTrue(health_line([], 24, {"catskills": "reachable"}).startswith("Health: OK"))

    def test_no_sources_is_not_ok(self):
        line = health_line([], 0, {})
        self.assertIn("ATTENTION", line)
        self.assertIn("no job data", line)
        self.assertIn("no HA instances", line)

    def test_partial_and_unknown_reported(self):
        line = health_line([], 24, {"catskills": "reachable", "bayridge": "unknown",
                                    "attic": "degraded"})
        self.assertIn("HA bayridge status unknown", line)
        self.assertIn("HA attic missed its last check", line)

    def test_attention_line(self):
        stale = stale_jobs([job("brief", NOW - 3 * DAY)], NOW)
        line = health_line(stale, 24, {"catskills": "unreachable"})
        self.assertIn("brief stale (last success 3d0h ago, limit 2d0h)", line)
        self.assertIn("HA catskills unreachable", line)


if __name__ == "__main__":
    unittest.main()
