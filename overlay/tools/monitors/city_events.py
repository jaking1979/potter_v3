#!/usr/bin/env python3
"""City events fetcher: public venue and neighbourhood calendars -> new events for the events-digest skill.

No model and no judgment here. Each source page is fetched once. Events come from schema.org JSON-LD or the
source's own configured regex; a page with neither hands over only the text that is new since the last visit,
and the skill reads the events out of it. An item is handed to the skill once (seen store); everything listed
right now is also written to current.json for the weekend preview.

  city_events.py --config FILE --state-dir DIR [--baseline] [--dry-run]

Config section "city_events": {"window_days": 90, "sources": [{"id","name","url","area",
"mode"?: "auto|events|page|links", "pattern"?, "exclude_titles"?}]}.
Sources are configuration; none are written in this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor_lib as ml  # noqa: E402

NAME, SECTION = "city-events", "city_events"


def collect(cfg, get, now, seen):
    return ml.collect_sources(cfg, get, now, seen, default_mode="auto", default_kind="event")


def main(argv: list[str], opener=None, now=None, out=None, err=None) -> int:
    return ml.run_monitor(NAME, SECTION, collect, argv, opener=opener, now=now, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
