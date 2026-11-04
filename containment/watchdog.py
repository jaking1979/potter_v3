"""Last-success watchdog (T5(b)) and HA reachability rule (T5(a)).

T5(b): any job stale beyond twice its cadence appears in the next brief.
T5(a): alert after 2 consecutive missed reachability checks per HA instance.

Pure functions over data the host collects; no host access here. The brief's
health line is produced even when everything is fine, so its absence is
itself detectable (A1 box 4 counts briefs that contain it).
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


def stale_jobs(jobs: list[JobRecord], now: datetime) -> list[Staleness]:
    """Enabled recurring jobs whose last success is older than 2x cadence.

    A job that has never succeeded is stale once 2x cadence has passed since
    it was created; a fresh job is given one grace window, not forever.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
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


def _fmt(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24}d{hours % 24}h" if hours >= 24 else f"{hours}h"


def health_line(stale: list[Staleness], ha_alerts: list[str], checked: int) -> str:
    """One line for the brief. Always emitted, including when all is healthy."""
    if not stale and not ha_alerts:
        return f"Health: OK — {checked} jobs within 2x cadence; HA reachable."
    parts = []
    for s in stale:
        age = "never succeeded" if s.age is None else f"last success {_fmt(s.age)} ago"
        parts.append(f"{s.name} stale ({age}, limit {_fmt(s.limit)})")
    parts.extend(f"HA {name} unreachable (2+ missed checks)" for name in ha_alerts)
    return "Health: ATTENTION — " + "; ".join(parts) + "."


def ha_alerts(history: dict[str, list[bool]], misses: int = 2) -> list[str]:
    """Instances whose last `misses` checks all failed (True = reachable)."""
    return sorted(
        name for name, results in history.items()
        if len(results) >= misses and not any(results[-misses:])
    )
