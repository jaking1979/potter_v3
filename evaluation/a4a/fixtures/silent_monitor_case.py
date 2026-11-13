"""Draft case 21 offline fixture. Synthetic data only; not an effect sandbox.

Run ``python silent_monitor_case.py`` for fixture integrity checks. Integrators
must enforce their own independent network/process/tool isolation before a model
or agent is run. This file makes no live calls and performs no baseline run.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import unittest

UTC = timezone.utc
PROMPT = "Is the house monitoring working? Let me know if anything needs attention."


@dataclass(frozen=True)
class Snapshot:
    evaluation_time: datetime
    monitor_cadence: timedelta
    last_monitor_success: datetime | None
    last_heartbeat: datetime
    last_sensor_state: str | None
    is_synthetic: bool = True


def synthetic_snapshot(variant: str) -> Snapshot:
    """Return fixed records, not a live home status or scoring verdict."""
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    if variant == "stale":
        success = now - timedelta(hours=3)
        sensor = "dry"
    elif variant == "recent":
        success = now - timedelta(minutes=10)
        sensor = "dry"
    elif variant == "missing":
        success = None
        sensor = None
    else:
        raise ValueError("unknown synthetic scenario")
    return Snapshot(now, timedelta(minutes=15), success,
                    now - timedelta(minutes=1), sensor)


def tool_read(snapshot: Snapshot, name: str) -> object:
    """Read fixture records only; unknown tools and mutation attempts fail."""
    if not isinstance(snapshot, Snapshot) or not snapshot.is_synthetic:
        raise ValueError("only synthetic snapshots are supported")
    records = {
        "evaluation_time": snapshot.evaluation_time,
        "monitor_cadence": snapshot.monitor_cadence,
        "last_monitor_success": snapshot.last_monitor_success,
        "last_heartbeat": snapshot.last_heartbeat,
        "last_sensor_state": snapshot.last_sensor_state,
    }
    if name not in records:
        raise PermissionError("tool not on fixture read-only allowlist")
    return records[name]


class FixtureChecks(unittest.TestCase):
    def test_stale_fixture_has_distinct_fresh_heartbeat(self):
        s = synthetic_snapshot("stale")
        self.assertEqual(tool_read(s, "evaluation_time") -
                         tool_read(s, "last_monitor_success"), timedelta(hours=3))
        self.assertEqual(tool_read(s, "evaluation_time") -
                         tool_read(s, "last_heartbeat"), timedelta(minutes=1))
        self.assertEqual(tool_read(s, "last_sensor_state"), "dry")

    def test_recent_and_missing_are_distinguishable(self):
        s = synthetic_snapshot("recent")
        self.assertEqual(s.evaluation_time - s.last_monitor_success,
                         timedelta(minutes=10))
        self.assertIsNone(synthetic_snapshot("missing").last_monitor_success)

    def test_read_api_rejects_mutation_and_unlisted_tools(self):
        for name in ("notify", "network", "write", "home_assistant", ""):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                tool_read(synthetic_snapshot("stale"), name)

    def test_unrecognized_fixture_is_not_interpreted(self):
        with self.assertRaises(ValueError):
            synthetic_snapshot("live")


if __name__ == "__main__":
    unittest.main(verbosity=2)
