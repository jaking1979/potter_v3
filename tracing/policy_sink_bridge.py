"""Offline opt-in replay bridge; not effect-atomic or authenticated audit."""
from tracing.sqlite_sink import SqliteTraceSink
from tracing.trace import DecisionTrace


def emit_replay_safe(sink, trace):
    """Keep first timestamp for a semantic retry within one sink transaction."""
    if not isinstance(sink, SqliteTraceSink) or not isinstance(trace, DecisionTrace):
        raise TypeError("expected offline sink and decision trace")
    if trace.component != "policy":
        raise ValueError("only policy traces supported")
    return sink.put_time_retry(trace.trace_id, trace.jsonl())
