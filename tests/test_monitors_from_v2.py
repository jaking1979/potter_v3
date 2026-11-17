import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import monitors_from_v2 as mv  # noqa: E402

SCRAPER = """const SOURCES = [
  { id: 'venue-a', name: 'Venue A', url: 'https://a.example/events', area: 'North' },
  { id: 'board', name: 'Board', url: 'https://b.example/news', area: 'South' },
];
const res = await fetch('https://region.example/events/', { headers: {} });"""
SOURCES_MD = """# Local monitor
## News & Civic Sources (weekly check)
| Source | URL | Notes |
|--------|-----|-------|
| The Paper | https://paper.example | weekly |
| No Link | - | skip |
## Weekly Search Queries
- "Town NY"
- Seasonal (winter): "Ski Hill" news
## Businesses (food & retail)
| # | Business | Town | Type | IG | Website |
|---|----------|------|------|-----|---------|
| 1 | Cafe One | Town | cafe | @c | cafeone.example |
| 2 | No Site | Town | diner | @d | — |
## Ski Shops (north)
| # | Shop | Town | Website | Notes |
|---|------|------|---------|-------|
| 1 | Ski Shop | Hill | skishop.example | rentals |
"""
WINE = 'BASELINE_SALE=15.20\nWINE="Some Pinot 2021"\n'
FIN = """### Blog/Newsletter Sources
| Source | Type | Feed | Priority |
|--------|------|------|----------|
| Doctor of Credit | Blog | RSS via blogwatcher | Highest |
| Unknown Blog | Blog | RSS via blogwatcher | Medium |
| A Newsletter | Email | inbox | High |
"""
FILES = {"x/city-events-scraper.mjs": SCRAPER, "x/state/m/SOURCES.md": SOURCES_MD,
         "x/scripts/wine-watchdog.sh": WINE, "x/rules/domains/financial-optimization.md": FIN}


class Build(unittest.TestCase):
    def setUp(self):
        self.c = mv.build(FILES)

    def test_city_sources_come_from_the_scraper_list(self):
        self.assertEqual([s["url"] for s in self.c["city_events"]["sources"]],
                         ["https://a.example/events", "https://b.example/news"])

    def test_local_sources_news_businesses_shops_and_regional_calendar(self):
        got = {(s["name"], s["kind"], s["mode"], s["url"]) for s in self.c["local_monitor"]["sources"]}
        self.assertIn(("The Paper", "news", "links", "https://paper.example"), got)
        self.assertIn(("Cafe One", "business", "page", "https://cafeone.example"), got)
        self.assertIn(("Ski Shop", "seasonal", "page", "https://skishop.example"), got)
        self.assertIn(("Regional events calendar", "events", "events", "https://region.example/events/"), got)
        self.assertFalse(any(n in ("No Link", "No Site") for n, *_ in got))
        self.assertEqual(self.c["local_monitor"]["search_queries"], ["Town NY", "Ski Hill news"])

    def test_wine_and_feeds(self):
        self.assertEqual(self.c["wine_watch"]["items"][0]["name"], "Some Pinot 2021")
        self.assertEqual(self.c["wine_watch"]["items"][0]["alert_below"], 15.2)
        self.assertEqual([f["name"] for f in self.c["financial_feeds"]["feeds"]], ["Doctor of Credit"])
        self.assertEqual(self.c["financial_feeds"]["feeds"][0]["priority"], "high")
        self.assertTrue(any("Unknown Blog" in u for u in self.c["_unverified"]))

    def test_nothing_found_builds_nothing(self):
        self.assertEqual(sorted(k for k in mv.build({}) if not k.startswith("_")), [])

    def test_the_config_fits_the_fetchers(self):
        import json
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "c.json"
            cfg.write_text(json.dumps(self.c))
            for m in ("city_events", "local_monitor", "wine_price", "feed_scan"):
                r = subprocess.run([sys.executable, str(ROOT / f"overlay/tools/monitors/{m}.py"), "--config", str(cfg),
                                    "--state-dir", str(Path(t) / "s"), "--dry-run"],
                                   capture_output=True, text=True, timeout=60,
                                   env={"PATH": "/usr/bin:/bin", "MONITOR_OFFLINE": "1", "https_proxy": "http://127.0.0.1:9",
                                        "http_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                                        "HTTP_PROXY": "http://127.0.0.1:9"})
                self.assertNotIn("Traceback", r.stderr, m)


if __name__ == "__main__":
    unittest.main()
