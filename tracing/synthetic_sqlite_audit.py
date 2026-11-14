"""Offline C6 binding to the SQLite trace sink.

Caller chooses strict or time-only retry semantics explicitly. This helper
does not authenticate context, isolate code, or make traces atomic with effects.
Do not use with real personal data or production services.
"""
from .policy_binding import SyntheticPolicyAudit
from .sqlite_sink import SqliteTraceSink


def sqlite_policy_audit(sink, *, time_only_retries=False):
    """Build a synthetic audit hook using the existing sink and policy schema.

    Strict byte-identical retries are the default. Time-only retries require an
    explicit boolean opt-in and retain the first timestamp. This uses the
    policy binding's existing event IDs; it does not reconcile other lineages.
    """
    if not isinstance(sink, SqliteTraceSink):
        raise TypeError("offline SQLite sink required")
    if type(time_only_retries) is not bool:
        raise TypeError("time_only_retries must be a boolean")
    method = sink.put_time_retry if time_only_retries else sink.put
    return SyntheticPolicyAudit(method, environment=sink.environment)
