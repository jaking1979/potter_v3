"""Synthetic ID-only derived view; never an authority or a search backend.

All returned content is freshly read from the local canonical-store experiment.
There is deliberately no 15-minute content cache: every read revalidates. This
does not provide an action-time fence; C1/C2 must still revalidate at dispatch.
"""
from dataclasses import dataclass
import math
import threading

from .store import MemoryStore, Retrieval, Invalid


@dataclass(frozen=True)
class ViewResult:
    retrieval: Retrieval
    generation: int
    index_stale: bool
    source: str


def _time(now):
    if type(now) not in (float, int):
        raise Invalid('explicit finite synthetic clock required')
    try:
        result = float(now)
    except (OverflowError, ValueError):
        raise Invalid('finite synthetic clock required') from None
    if not math.isfinite(result) or result < 0:
        raise Invalid('finite synthetic clock required')
    return result


def _identity(result):
    # No values, source pointers, keys or content digests in index entries.
    return tuple((x.id, x.version, x.status) for x in result.active + result.provisional)


class LocalView:
    """Atomic whole-generation publication over explicit synthetic keys only.

    The view owns no content. A failed rebuild keeps its last good ID generation
    and marks it stale. Rebuilds serialize; readers see a complete old or new
    generation. Unknown keys and drift use direct canonical fallback.
    """
    def __init__(self, store):
        if not isinstance(store, MemoryStore):
            raise Invalid('local synthetic MemoryStore required')
        self._store = store
        self._lock = threading.RLock()
        self._rebuild_lock = threading.Lock()
        self._generation = 0
        self._entries = frozenset()
        self._built_at = None
        self._failed = False

    def rebuild(self, keys, *, now):
        now = _time(now)
        if type(keys) not in (list, tuple) or len(keys) > 1000:
            raise Invalid('bounded explicit keys required')
        keys = tuple(keys)
        for key in keys:
            MemoryStore._text(key, 'key')
        if len(set(keys)) != len(keys):
            raise Invalid('duplicate keys')
        with self._rebuild_lock:
            try:
                # One canonical snapshot, never a mixture of versions.
                snapshot = self._store.snapshot(keys)
                # Do not retain fact keys, including sensitive keys, in a derived view.
                # Empty snapshots are deliberately never treated as cache hits.
                entries = frozenset(identity for v in snapshot.values()
                                    if (identity := _identity(v)))
            except BaseException:
                with self._lock:
                    self._failed = True
                raise
            with self._lock:
                self._entries = entries
                self._built_at = now
                self._failed = False
                self._generation += 1
                return self._generation

    def retrieve(self, key, *, consequential, now):
        now = _time(now)
        # Always use the store's actual tier filtering, never cached content.
        current, advisory = self._store.view_read(key, consequential=consequential)
        with self._lock:
            identity = _identity(advisory)
            matched = bool(identity) and identity in self._entries
            stale = (self._failed or self._built_at is None or now < self._built_at
                     or now - self._built_at > 86400 or not matched)
            return ViewResult(current, self._generation, stale,
                              'canonical_revalidated' if matched else 'canonical_fallback')
