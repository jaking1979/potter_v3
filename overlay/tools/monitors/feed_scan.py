#!/usr/bin/env python3
"""Feed fetcher for the financial monitor: RSS/Atom feeds -> new articles for the financial-scan skill.

No model. Replaces the old chain of an agent turn running a script that shelled out to a feed reader: reading
feeds is deterministic, so it is a command job. Each article is handed over once; nothing is analysed here.

  feed_scan.py --config FILE --state-dir DIR [--baseline] [--dry-run]

Config section "financial_feeds": {"max_age_days": 14, "max_per_feed": 30, "feeds": [{"id","name","url",
"priority"?}]}. Feeds with a document type or entity declaration are refused (no XML tricks).
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor_lib as ml  # noqa: E402

NAME, SECTION = "financial-feeds", "financial_feeds"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_feed(xml: str, base_url: str) -> list[dict]:
    """[{"title","url","published": datetime|None}] from RSS 2.0 or Atom."""
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", xml[:200000], re.I):
        raise ml.FetchError("feed declares a document type or entities; refused")
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError as e:
        raise ml.FetchError(f"not a readable feed: {ml.clean(str(e), 80)}")
    out = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = link = when = ""
        for ch in node:
            tag = _local(ch.tag)
            if tag == "title":
                title = ml.html_to_text("".join(ch.itertext()))
            elif tag == "link" and not link:
                if ch.get("href") and ch.get("rel", "alternate") == "alternate":
                    link = ch.get("href")
                elif (ch.text or "").strip():
                    link = ch.text.strip()
            elif tag in ("pubdate", "published", "updated", "date") and not when:
                when = (ch.text or "").strip()
        url = urljoin(base_url, link) if link else ""
        if title and urlsplit(url).scheme in ("http", "https"):
            out.append({"title": ml.clean(title, 200), "url": url, "published": ml.parse_date(when)})
    return out


def collect(cfg, get, now, seen):
    age = cfg.get("max_age_days", 14)
    age = age if isinstance(age, int) and 0 < age <= 366 else 14
    cap = cfg.get("max_per_feed", 30)
    cap = cap if isinstance(cap, int) and 0 < cap <= 200 else 30
    items, statuses = [], []
    for feed in cfg.get("feeds") or []:
        if not isinstance(feed, dict) or not feed.get("id") or not feed.get("url"):
            continue
        sid = str(feed["id"])
        try:
            entries = parse_feed(get(feed["url"]), feed["url"])
        except ml.FetchError as e:
            statuses.append({"source": sid, "ok": False, "error": ml.clean(str(e), 160)})
            continue
        n = 0
        for e in entries:
            if e["published"] and e["published"] < now - timedelta(days=age):
                continue
            key = ml.item_id(e["url"].split("#")[0].split("?utm_")[0].rstrip("/"))
            items.append({"key": key, "id": key, "kind": "article", "source": str(feed.get("name") or sid),
                          "source_id": sid, "priority": str(feed.get("priority") or ""), "title": e["title"],
                          "url": e["url"],
                          "published": e["published"].date().isoformat() if e["published"] else ""})
            n += 1
            if n >= cap:
                break
        statuses.append({"source": sid, "ok": True, "extracted": n})
    return items, statuses, None


def main(argv: list[str], opener=None, now=None, out=None, err=None) -> int:
    return ml.run_monitor(NAME, SECTION, collect, argv, opener=opener, now=now, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
