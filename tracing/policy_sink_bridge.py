"""Offline policy replay bridge; not effect-atomic or authenticated audit."""
import json
from tracing.sqlite_sink import SqliteTraceSink, TraceStoreRefused, validate_jsonl
from tracing.trace import DecisionTrace


def emit_replay_safe(sink, trace):
    """Insert a decision trace or preserve first timestamp for an exact semantic retry.

    The sink is offline only. Event IDs are caller-provided stable trace IDs.
    No external effects, identity trust, retention or protected rollback is supplied.
    """
    if not isinstance(sink, SqliteTraceSink) or not isinstance(trace, DecisionTrace):
        raise TypeError("expected offline sink and decision trace")
    if trace.component != "policy":
        raise ValueError("only policy traces supported")
    line = trace.jsonl()
    try:
        return sink.put(trace.trace_id, line)
    except TraceStoreRefused:
        # Revalidate the entire authoritative store before considering a retry.
        # If the attempted insert failed for an unrelated reason this read must
        # fail as well or find no matching row, and the original error remains.
        rows = sink.records()
        prior = next((record for event_id, record in rows if event_id == trace.trace_id), None)
        if prior is None:
            raise
        first = json.loads(validate_jsonl(prior, sink.environment))
        retry = json.loads(validate_jsonl(line, sink.environment))
        first.pop("time")
        retry.pop("time")
        if first != retry:
            raise
        return False
