"""Last-success watchdog (T5(b)) and HA reachability (T5(a)).

T5(b): any job stale beyond twice its cadence appears in the next brief.
T5(a): alert after 2 consecutive missed reachability checks per HA instance.

Coverage is reported honestly: "reachable" only when the latest check is
recent and succeeded. No observations, an unavailable check, or checks that
stopped arriving are "unknown", never healthy. One miss is "degraded" and is
shown, though it is not yet an alert. The brief's health line is always
emitted and says OK only when every job was evaluated and every configured HA
instance is confirmed reachable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class JobRecord:
    name: str
    enabled: bool
    cadence: timedelta | None          # None for one-shot jobs
    last_success: datetime | None
    created: datetime


@dataclass(frozen=True)
class Staleness:
    name: str
    age: timedelta | None              # None if never succeeded
    limit: timedelta


def _aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")


def stale_jobs(jobs: list[JobRecord], now: datetime) -> list[Staleness]:
    """Enabled recurring jobs whose last success is older than 2x cadence.

    A job that has never succeeded is stale once 2x cadence has passed since
    it was created; a fresh job gets one grace window, not forever.
    """
    _aware(now)
    out = []
    for job in jobs:
        if not job.enabled or job.cadence is None:
            continue
        if job.cadence <= timedelta(0):
            raise ValueError(f"{job.name}: cadence must be positive")
        limit = 2 * job.cadence
        reference = job.last_success or job.created
        if now - reference > limit:
            age = None if job.last_success is None else now - job.last_success
            out.append(Staleness(job.name, age, limit))
    return sorted(out, key=lambda s: s.name)


# One HA check: when it ran and its result (True reachable, False unreachable,
# None = the check itself could not run).
Check = tuple[datetime, "bool | None"]


def ha_status(checks: list[Check], cadence: timedelta, now: datetime) -> str:
    """'reachable' | 'degraded' (1 miss) | 'unreachable' (2+ misses, alert) | 'unknown'."""
    _aware(now)
    if not checks:
        return "unknown"
    ordered = sorted(checks, key=lambda c: c[0])
    last_at, last = ordered[-1]
    if now - last_at > 2 * cadence or last is None:
        return "unknown"                 # coverage stopped or the check failed to run
    if last:
        return "reachable"
    misses = 0
    for _, result in reversed(ordered):
        if result is False:
            misses += 1
        else:
            break
    return "unreachable" if misses >= 2 else "degraded"


def _fmt(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24}d{hours % 24}h" if hours >= 24 else f"{hours}h"


def health_line(stale: list[Staleness], jobs_checked: int, ha: dict[str, str]) -> str:
    """One line for the brief. Always emitted."""
    problems = []
    if jobs_checked == 0:
        problems.append("no job data")
    for s in stale:
        age = "never succeeded" if s.age is None else f"last success {_fmt(s.age)} ago"
        problems.append(f"{s.name} stale ({age}, limit {_fmt(s.limit)})")
    if not ha:
        problems.append("no HA instances configured for checks")
    for name, status in sorted(ha.items()):
        if status == "unreachable":
            problems.append(f"HA {name} unreachable (2+ missed checks)")
        elif status == "degraded":
            problems.append(f"HA {name} missed its last check")
        elif status != "reachable":
            problems.append(f"HA {name} status unknown (no current check)")
    if problems:
        return "Health: ATTENTION — " + "; ".join(problems) + "."
    return f"Health: OK — {jobs_checked} jobs within 2x cadence; HA {', '.join(sorted(ha))} reachable."
