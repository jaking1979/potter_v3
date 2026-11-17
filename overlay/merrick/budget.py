"""Time budget for the slow path: a hard deadline, and an honest partial answer when it is hit.

v2's fallback wandered for minutes and said nothing. Here the fallback works in steps against a deadline; each
step records what it learned as observed / estimated / hypothesis, and names what it is still looking for. When
the deadline arrives the work stops (a step still running is abandoned, not waited for) and the report says what
is known so far and what is not.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

DEFAULT_BUDGET_S = 90.0


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, seconds: float = DEFAULT_BUDGET_S, clock: Callable[[], float] = time.monotonic):
        self.seconds, self.clock = float(seconds), clock
        self.started = clock()

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed())

    def expired(self) -> bool:
        return self.elapsed() >= self.seconds

    def check(self) -> None:
        if self.expired():
            raise BudgetExceeded(f"time budget of {self.seconds:g}s used up")


@dataclass
class PartialReport:
    """What is known so far. Every line carries its label; nothing unlabelled is ever rendered."""
    question: str = ""
    observed: list = field(default_factory=list)
    estimated: list = field(default_factory=list)
    hypothesis: list = field(default_factory=list)
    unknown: list = field(default_factory=list)
    complete: bool = False
    stopped_because: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _closed: bool = field(default=False, repr=False, compare=False)

    def close(self) -> None:
        """After this, a step that was left running can no longer change the report that was already given."""
        with self._lock:
            self._closed = True

    def _add(self, bucket: list, text: str) -> None:
        with self._lock:
            if text and text not in bucket and not self._closed:
                bucket.append(str(text))

    def observe(self, text: str) -> None:
        self._add(self.observed, text)

    def estimate(self, text: str) -> None:
        self._add(self.estimated, text)

    def suppose(self, text: str) -> None:
        self._add(self.hypothesis, text)

    def need(self, text: str) -> None:
        """Declare something being looked for. It stays 'unknown' until resolve() removes it."""
        self._add(self.unknown, text)

    def resolve(self, text: str) -> None:
        with self._lock:
            if text in self.unknown and not self._closed:
                self.unknown.remove(text)

    def render(self) -> str:
        with self._lock:
            head = "Finished." if self.complete else \
                f"Stopped early ({self.stopped_because or 'time budget reached'}). What I know so far:"
            lines = [head]
            for label, items in (("Observed", self.observed), ("Estimated", self.estimated),
                                 ("Hypothesis", self.hypothesis), ("Unknown", self.unknown)):
                lines += [f"- {label}: {i}" for i in items]
            if len(lines) == 1:
                lines.append("- Unknown: nothing was established before the deadline.")
            return "\n".join(lines)


def run_with_budget(steps: Iterable[tuple[str, Callable[[PartialReport], None]]], budget: Budget,
                    report: Optional[PartialReport] = None) -> PartialReport:
    """Run named steps in order until done or the deadline. Never returns later than the deadline (plus scheduling
    jitter): a step that is still running at the deadline is left behind in a daemon thread and its name is
    reported as unknown. A step that raises is recorded as unknown and the next step still runs."""
    rep = report or PartialReport()
    steps = list(steps)
    for i, (name, fn) in enumerate(steps):
        if budget.expired():
            for later, _ in steps[i:]:
                rep.need(f"{later} (not started)")
            rep.stopped_because = f"time budget of {budget.seconds:g}s reached"
            rep.close()
            return rep
        err: list = []

        def work(fn=fn):
            try:
                fn(rep)
            except Exception as e:                                # noqa: BLE001 - reported, not hidden
                err.append(e)

        t = threading.Thread(target=work, daemon=True, name=f"merrick-step-{name}")
        t.start()
        t.join(budget.remaining())
        if t.is_alive():
            rep.need(f"{name} (still running when time ran out)")
            for later, _ in steps[i + 1:]:
                rep.need(f"{later} (not started)")
            rep.stopped_because = f"time budget of {budget.seconds:g}s reached"
            rep.close()
            return rep
        if err:
            rep.need(f"{name} (failed: {type(err[0]).__name__})")
    rep.complete = True
    return rep
