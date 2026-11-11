"""D4 offline retrieval experiment. No external connectors or durable effects.

This module is a simulator, not an authorization or memory freshness boundary.
Real callers must independently enforce T12 and T15/T27, including canonical
revalidation, before relying on retrieved context for consequential work.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Evidence:
    item_id: str
    text: str


class Backend(Protocol):
    async def search(self, query: str) -> Sequence[Evidence]: ...


@dataclass(frozen=True)
class TurnResult:
    items: tuple[Evidence, ...]
    status: str  # complete, unavailable, deadline
    elapsed_s: float
    blocked: bool


def _drain(task: asyncio.Task) -> None:
    """Consume exceptions from late work; never feed it into a turn."""
    if not task.cancelled():
        try:
            task.exception()
        except (Exception, asyncio.CancelledError):
            pass


async def _bounded(backend: Backend, query: str, timeout: float):
    from retrieval.isolated import SyntheticProcessBackend, bounded_search
    if isinstance(backend, SyntheticProcessBackend):
        return await bounded_search(backend, query, timeout)
    start = time.monotonic()
    task = asyncio.create_task(backend.search(query))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            task.add_done_callback(_drain)
            return 'deadline', (), time.monotonic() - start
        # asyncio's timer can run late when a backend blocks the event loop.
        # A completed task is not proof that its result met the deadline.
        if time.monotonic() - start >= timeout:
            _drain(task)
            return 'deadline', (), time.monotonic() - start
        try:
            raw = task.result()
            items = tuple(raw)
            if any(not isinstance(item, Evidence) or not item.item_id for item in items):
                raise ValueError('invalid evidence')
            if time.monotonic() - start >= timeout:
                return 'deadline', (), time.monotonic() - start
            return 'complete', items[:3], time.monotonic() - start
        except asyncio.CancelledError:
            # The backend may cancel itself. The caller was not cancelled:
            # report a failed retrieval instead of cancelling the whole turn.
            return 'unavailable', (), time.monotonic() - start
        except Exception:
            return 'unavailable', (), time.monotonic() - start
    except BaseException:
        task.cancel()
        task.add_done_callback(_drain)
        raise


def _positive_finite_duration(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


async def retrieve_turn(backend: Backend, query: str, *, consequential: bool,
                        timeout: float = 2.0) -> TurnResult:
    """Advisory timeouts expose no late results; consequential misses block.

    Consequential work remains blocked even after a successful simulated search:
    canonical revalidation and policy integration are not implemented here.
    """
    if not isinstance(consequential, bool) or not _positive_finite_duration(timeout):
        raise ValueError('invalid mode or timeout')
    from retrieval.isolated import SyntheticProcessBackend
    if isinstance(backend, SyntheticProcessBackend) and timeout > 2.0:
        raise ValueError('isolated production deadline exceeds two seconds')
    status, items, elapsed = await _bounded(backend, query, timeout)
    return TurnResult(items, status, elapsed, consequential)


@dataclass(frozen=True)
class BenchmarkResult:
    status: str
    top_ids: tuple[str, ...]
    elapsed_s: float
    hit: bool
    production_deadline_met: bool


async def benchmark(backend: Backend, query: str, expected_ids: Sequence[str], *,
                    production_deadline: float = 2.0, timeout: float = 30.0) -> BenchmarkResult:
    """Measure independently from production; never returns content for injection."""
    expected = tuple(expected_ids)
    if (not 1 <= len(expected) <= 3 or len(set(expected)) != len(expected)
            or any(not isinstance(i, str) or not i for i in expected)
            or not _positive_finite_duration(production_deadline) or not _positive_finite_duration(timeout)):
        raise ValueError('invalid evidence set or deadline')
    status, items, elapsed = await _bounded(backend, query, timeout)
    ids = tuple(i.item_id for i in items)
    return BenchmarkResult(status, ids, elapsed,
                           status == 'complete' and set(expected).issubset(ids),
                           status == 'complete' and elapsed <= production_deadline)


class FakeBackend:
    """Deterministic, synthetic backend used only in tests."""
    def __init__(self, items: Sequence[Evidence] = (), delay: float = 0,
                 error: bool = False):
        self.items, self.delay, self.error = tuple(items), delay, error

    async def search(self, query: str) -> Sequence[Evidence]:
        await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError('simulated outage')
        return self.items

