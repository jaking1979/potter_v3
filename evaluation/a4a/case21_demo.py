"""Local, fictional case-21 walkthrough; this does not run a model or grade.

The callback is trusted in-process. It is not a sandbox or proof of no effects.
Only use the built-in synthetic callback. No network or production tools.
Run: python evaluation/a4a/case21_demo.py [stale|recent|missing]
"""
import argparse
import json
from datetime import datetime, timedelta

from case21_runner import run_case21
from fixtures.case21_exercise import FIELDS


def _encode(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"seconds": value.total_seconds()}
    raise TypeError("unsupported fixture value")


class NonSyntheticFixture(ValueError):
    pass


def _collect(_prompt, tools):
    marker = tools.call("read", "is_synthetic")
    if marker is not True:
        raise NonSyntheticFixture("fixture must be explicitly synthetic")
    records = {"is_synthetic": marker}
    records.update({field: tools.call("read", field) for field in FIELDS if field != "is_synthetic"})
    return json.dumps(records, default=_encode, sort_keys=True)


def walkthrough(scenario):
    """Return an explicitly ungraded synthetic observation and run status."""
    marker_refused = False
    def collect(prompt, tools):
        nonlocal marker_refused
        try:
            return _collect(prompt, tools)
        except NonSyntheticFixture:
            marker_refused = True
            raise
    result = run_case21(scenario, collect)
    status = "refused_non_synthetic" if marker_refused else result.status
    observations = json.loads(result.output) if result.output is not None else None
    required = ("evaluation_time", "monitor_cadence", "last_monitor_success",
                "last_sensor_state", "last_sensor_time", "scheduler_window_start",
                "scheduler_window_end", "scheduler_records")
    incomplete = (None if observations is None else
                  observations["scheduler_history_complete"] is not True or
                  any(observations[field] is None for field in required))
    return {
        "scenario_label_notice": "Scenario name is shown for demonstration and may reveal the expected pattern.",
        "observations_incomplete": incomplete,
        "provenance": ("fictional synthetic fixture; not live monitoring"
                       if status == "completed_ungraded" and observations is not None else None),
        "scenario": result.scenario,
        "status": status,
        "graded": False,
        "observations": observations,
        "fields_read": result.fields_read,
        "read_count": result.read_count,
        "events_dropped": result.events_dropped,
        "limitations": "Trusted in-process callback; no model, isolation, or baseline.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("stale", "recent", "missing"),
                        nargs="?", default="stale")
    args = parser.parse_args()
    report = walkthrough(args.scenario)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "completed_ungraded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
