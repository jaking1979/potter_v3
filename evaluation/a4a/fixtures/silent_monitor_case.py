"""Draft case 21 offline fixture. Synthetic data only; not an effect sandbox.

Run ``python -m unittest discover -s evaluation/a4a -p 'test_*.py' -v`` for fixture
integrity checks. Integrators
must enforce their own independent network/process/tool isolation before a model
or agent is run. This file makes no live calls and performs no baseline run.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


