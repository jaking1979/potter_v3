#!/usr/bin/env python3
"""Build the monitor config (state/monitors/config.json) from a Potter v2 export.

The owner's real sources (event venues, local papers and businesses, wine, deal blogs) are personal, so they are
never stored in the repo: they are read here, at install, from the files the previous assistant kept them in:
  - the city events scraper's SOURCES list            -> city_events.sources
  - the local monitor's SOURCES.md (news, businesses, ski shops, weekly searches) and the regional events
    calendar the second events scraper fetched        -> local_monitor
  - the wine watchdog's wine name and sale baseline   -> wine_watch (the old system searched; no retailer page)
  - the deal-blog table in the financial rule file    -> financial_feeds (feed address = the blog's standard
    WordPress feed; marked unverified until the first run proves it)

  monitors_from_v2.py EXPORT.zip OUT.json
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

URL = re.compile(r"https?://[A-Za-z0-9.\-]+(?:/[^\s|)\"'<>]*)?")
JS_SOURCE = re.compile(r"\{\s*id:\s*'([^']+)',\s*name:\s*'([^']+)',\s*url:\s*'([^']+)',\s*area:\s*'([^']+)'\s*\}")
FETCH_URL = re.compile(r"fetch\(\s*'(https?://[^']+)'")
DOMAIN = re.compile(r"^(?:www\.)?[a-z0-9\-]+(?:\.[a-z0-9\-]+)+$", re.I)
KNOWN_FEEDS = {           # public blog feeds; the address is the blog's own standard feed
    "doctor of credit": "https://www.doctorofcredit.com/feed/",
    "frequent miler": "https://frequentmiler.com/feed/",
    "miles earn and burn": "https://milesearnandburn.com/index.php/feed/",
    "one mile at a time": "https://onemileatatime.com/feed/",
    "u.s. credit card guide": "https://www.uscreditcardguide.com/feed/",
    "dan's deals": "https://www.dansdeals.com/feed/",
    "miles to memories": "https://milestomemories.com/feed/",
    "loyaltylobby": "https://loyaltylobby.com/feed/",
    "awardwallet blog": "https://awardwallet.com/blog/feed/",
}
PRIORITY = {"highest": "high", "high": "high", "medium": "medium", "low": "low"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "source"


def table_rows(md: str, heading_word: str) -> list[list[str]]:
    """Rows of the first markdown table under a heading containing heading_word."""
    out, on = [], False
    for line in md.splitlines():
        if line.startswith("#"):
            on = heading_word.lower() in line.lower()
            continue
        if on and line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells[0] not in ("#", "Source", "Business", "Shop"):
                out.append(cells)
    return out


def bullets(md: str, heading_word: str) -> list[str]:
    out, on = [], False
    for line in md.splitlines():
        if line.startswith("#"):
            on = heading_word.lower() in line.lower()
            continue
        if on and line.lstrip().startswith("- "):
            out.append(re.sub(r"^(Seasonal \(\w+\):\s*)", "", line.lstrip()[2:]).strip())
    return out


def site(cell: str) -> str | None:
    cell = cell.strip().strip("`")
    m = URL.search(cell)
    if m:
        return m.group(0).rstrip(".,")
    return f"https://{cell}" if DOMAIN.match(cell) else None


def build(files: dict[str, str]) -> dict:
    cfg = {"_built_from": "the previous assistant's export, at install", "_unverified": []}
    city = []
    for name, text in files.items():
        if name.endswith("-events-scraper.mjs"):
            for i, n, u, a in JS_SOURCE.findall(text):
                city.append({"id": slug(i), "name": n, "url": u, "area": a})
    if city:
        cfg["city_events"] = {"window_days": 90, "sources": city}

    md = next((t for n, t in files.items() if n.endswith("/SOURCES.md")), "")
    local = []
    for row in table_rows(md, "News"):
        u = site(row[1]) if len(row) > 1 else None
        if u:
            local.append({"id": slug(row[0]), "name": row[0], "url": u, "kind": "news", "mode": "links"})
    for word, kind in (("Businesses", "business"), ("Ski Shops", "seasonal")):
        for row in table_rows(md, word):
            u = next((site(c) for c in reversed(row) if site(c)), None)
            name = row[1] if len(row) > 1 else row[0]
            if u:
                local.append({"id": slug(name), "name": name, "url": u, "kind": kind, "mode": "page"})
    for n, t in files.items():
        if n.endswith("-events-scraper.mjs"):
            for u in FETCH_URL.findall(t):
                local.append({"id": slug(u.split("//")[1].split("/")[0]) + "-events", "name": "Regional events calendar",
                              "url": u, "kind": "events", "mode": "events"})
    if local:
        cfg["local_monitor"] = {"window_days": 60, "max_per_source": 15, "sources": local,
                                "search_queries": [q.replace('"', "").strip() for q in bullets(md, "Search")][:10]}

    wine = next((t for n, t in files.items() if n.endswith("wine-watchdog.sh")), "")
    w = re.search(r'^WINE="([^"]+)"', wine, re.M)
    sale = re.search(r"^BASELINE_SALE=([0-9.]+)", wine, re.M)
    if w:
        cfg["wine_watch"] = {"items": [{"id": slug(w.group(1)), "name": w.group(1),
                                        "alert_below": float(sale.group(1)) if sale else 0.0, "pages": []}],
                             "offer_pages": [], "search_queries": [f"{w.group(1)} price"]}

    fin = next((t for n, t in files.items() if n.endswith("financial-optimization.md")), "")
    feeds = []
    for row in table_rows(fin, "Blog/Newsletter Sources"):
        if len(row) >= 4 and "rss" in row[2].lower():
            u = KNOWN_FEEDS.get(row[0].lower())
            if u:
                feeds.append({"id": slug(row[0]), "name": row[0], "url": u,
                              "priority": PRIORITY.get(row[3].lower(), "medium")})
            else:
                cfg["_unverified"].append(f"no feed address known for '{row[0]}'")
    if feeds:
        cfg["financial_feeds"] = {"max_age_days": 14, "max_per_feed": 30, "feeds": feeds}
        cfg["_unverified"].append("blog feed addresses are each blog's standard feed; the first run proves them")
    return cfg


def from_zip(path: Path) -> dict:
    want = re.compile(r"(-events-scraper\.mjs|/SOURCES\.md|wine-watchdog\.sh|financial-optimization\.md)$")
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n).decode("utf-8", "ignore") for n in z.namelist() if want.search(n)}
    return build(files)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: monitors_from_v2.py EXPORT.zip OUT.json", file=sys.stderr)
        return 2
    try:
        cfg = from_zip(Path(argv[0]))
    except (zipfile.BadZipFile, OSError) as e:
        print(f"monitor config not built: {e}", file=sys.stderr)
        return 1
    if not any(k in cfg for k in ("city_events", "local_monitor", "wine_watch", "financial_feeds")):
        print("monitor config not built: no sources found in the export", file=sys.stderr)
        return 1
    Path(argv[1]).parent.mkdir(parents=True, exist_ok=True)
    Path(argv[1]).write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    print(json.dumps({k: len(v.get("sources") or v.get("items") or v.get("feeds") or [])
                      for k, v in cfg.items() if not k.startswith("_")}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
