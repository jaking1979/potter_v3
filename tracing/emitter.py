"""Explicit trace emission seam. The caller supplies a sink; no storage by default."""
from .trace import DecisionTrace


def emit(record: DecisionTrace, sink) -> None:
    """Emit one validated record; propagate sink errors to the caller.

    This is not an atomic journal and makes no durability or exactly-once claim.
    """
    if not isinstance(record, DecisionTrace):
        raise TypeError('expected DecisionTrace')
    sink(record.jsonl())
