#!/usr/bin/env python3
"""Local monitor fetcher: news sites, business pages and event calendars for one area -> new items for the
local-monitor skill.

No model. The source's "mode" in the config picks how its page becomes findings (see collect_sources in
monitor_lib): "links" for news headlines (optionally only those matching the source's keywords), "page" for a
business or shop page (only text that is new since the last visit; the first visit just records the page),
"events" for a calendar. Each source's "kind" (news, business, seasonal, events) is passed through to the skill.

  local_monitor.py --config FILE --state-dir DIR [--baseline] [--dry-run]

Config section "local_monitor": {"window_days": 60, "max_per_source": 15, "sources": [{"id","name","url",
"kind","mode","keywords"?: [...], "pattern"?}], "search_queries": [...]}.
The search queries are for the skill; this program only reads pages.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor_lib as ml  # noqa: E402

NAME, SECTION = "local-monitor", "local_monitor"


def collect(cfg, get, now, seen):
    items, statuses, current = ml.collect_sources(cfg, get, now, seen, default_mode="links", default_kind="news")
    return items, statuses, [c for c in current if c.get("kind") == "event"] or None


def main(argv: list[str], opener=None, now=None, out=None, err=None) -> int:
    return ml.run_monitor(NAME, SECTION, collect, argv, opener=opener, now=now, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
