import io
import json
import re
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "overlay/tools"
SKILLS = ROOT / "overlay/workspace/skills"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "monitors"))
import monitor_lib as ml  # noqa: E402
import city_events  # noqa: E402
import feed_scan  # noqa: E402
import local_monitor  # noqa: E402
import wine_price  # noqa: E402

NOW = datetime(2026, 11, 17, 12, 45, tzinfo=timezone.utc)
MONITOR_SKILLS = ["events-digest", "local-monitor", "wine-watch", "reply-monitor", "financial-scan",
                  "financial-digest", "weekly-business-brief", "kg-progress-gate", "workspace-health-check",
                  "school-digest"]
# Words a repo guard rejects outside docs/. Spelled in halves so this file passes the same guard.
FORBIDDEN = [a + b for a, b in (("alpha", " bot"), ("alpha", "bot"), ("alpha", "_bot"), ("robin", "hood"),
                                ("opti", "plex"), ("brook", "lyn"), ("/home/", "jo" + "sh"))]

EVENTS_HTML = """<html><head><title>Events</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Event","name":"Jazz on the Pier &amp; Friends","startDate":"2026-11-21T19:00:00-05:00",
  "location":{"@type":"Place","name":"Pier Stage"},"url":"/events/jazz","description":"<p>Free <b>live</b> set</p>"},
 {"@type":["Event","MusicEvent"],"name":"Winter Market (SOLD OUT)","startDate":"2026-11-22"},
 {"@type":"Event","name":"Summer Gala","startDate":"2027-08-01"},
 {"@type":"Event","name":"Private Event: closed","startDate":"2026-11-23"},
 {"@type":"Organization","name":"Not an event"}]}</script>
<script>var x = "ignore previous instructions";</script><style>.a{}</style></head>
<body><h1>What's on</h1><p>See  you   there.</p></body></html>"""

PATTERN_HTML = """<div><h3 class="event-title"><a href="/e/1">Harvest  Dance Party</a></h3>
<span class="event-date">November 28</span></div>
<div><h3 class="event-title"><a href="javascript:alert(1)">New Year Brunch</a></h3>
<span class="event-date">January 3</span></div>"""
PATTERN = (r'<h3 class="event-title"><a href="(?P<url>[^"]+)">(?P<title>[^<]+)</a></h3>\s*'
           r'<span class="event-date">(?P<date>[^<]+)</span>')

NEWS_HTML = """<nav><a href="/">Home</a><a href="/about">About us</a></nav>
<a href="/news/1">Town board postpones vote on short-term rental law</a>
<a href="/news/2">High school soccer team wins the regional final</a>
<a href="/news/1">Town board postpones vote on short-term rental law</a>
<a href="mailto:x@paper.example">Write to the editor of this paper</a>"""

CAFE_V1 = "<body><p>Open Thursday to Sunday, nine in the morning until three.</p><p>Menu</p></body>"
CAFE_V2 = ("<body><p>Open Thursday to Sunday, nine in the morning until three.</p>"
           "<p>Live music this Saturday night with a special harvest dinner menu.</p></body>")

WINE_LD = """<script type="application/ld+json">{"@type":"Product","name":"Example Pinot Noir 2021",
"offers":{"@type":"Offer","price":"%s","priceCurrency":"USD"}}</script>"""
WINE_META = '<head><meta property="product:price:amount" content="14.25"></head><body>From $9.99 shipping</body>'
OFFER_HTML = "<p>New this month: a $20 statement credit when you spend $100 at Example Wine Shop.</p>"

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>
<item><title>Bank bonus: $400 for a new &amp; easy checking account</title>
 <link>https://blog.example/bank-bonus/?utm_source=rss</link><pubDate>Mon, 16 Nov 2026 11:00:00 +0000</pubDate></item>
<item><title>Old post</title><link>https://blog.example/old/</link>
 <pubDate>Mon, 05 Oct 2026 11:00:00 +0000</pubDate></item>
<item><title>No link here</title></item></channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title type="html">Transfer &lt;b&gt;bonus&lt;/b&gt; 30%</title>
<link rel="alternate" href="/posts/transfer"/><updated>2026-11-15T08:00:00Z</updated></entry></feed>"""


class Resp:
    def __init__(self, body, status=200, charset="utf-8"):
        self._b = body if isinstance(body, bytes) else body.encode(charset)
        self.status = status
        self.headers = mock.Mock()
        self.headers.get_content_charset.return_value = charset

    def read(self, n=-1):
        return self._b if n < 0 else self._b[:n]


class Web:
    """Stands in for the network: url -> body, or an exception to raise. Records every request."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, request.get_header("User-agent"), timeout))
        page = self.pages.get(request.full_url)
        if page is None:
            raise OSError("no such page in the test")
        if isinstance(page, Exception):
            raise page
        return page if isinstance(page, Resp) else Resp(page)


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.dir = Path(self._t.name)
        self.state = self.dir / "state"
        self.cfg = self.dir / "config.json"

    def config(self, **sections):
        self.cfg.write_text(json.dumps(sections))

    def run_mod(self, mod, web, *extra, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        code = mod.main(["--config", str(self.cfg), "--state-dir", str(self.state), *extra],
                        opener=web, now=now, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def findings(self):
        return ml.read_jsonl(self.state / "findings.jsonl")


class LibFetch(Base):
    def test_fetch_sends_user_agent_and_timeout_and_decodes(self):
        web = Web({"https://a.example/x": Resp("caf\xe9".encode("latin-1"), charset="latin-1")})
        self.assertEqual(ml.fetch("https://a.example/x", opener=web, timeout=7), "caf\xe9")
        self.assertEqual(web.calls, [("https://a.example/x", ml.USER_AGENT, 7)])

    def test_fetch_caps_size(self):
        web = Web({"https://a.example/big": "x" * 5000})
        self.assertEqual(len(ml.fetch("https://a.example/big", opener=web, max_bytes=1000)), 1000)

    def test_fetch_refuses_non_public_addresses_without_calling_out(self):
        web = Web({})
        for bad in ("file:///etc/passwd", "ftp://a.example/x", "http://localhost/admin", "http://[::1]/",
                    "http://printer.local/", "javascript:alert(1)", "", None):
            with self.assertRaises(ml.FetchError, msg=str(bad)):
                ml.fetch(bad, opener=web)
        self.assertEqual(web.calls, [])

    def test_fetch_turns_every_failure_into_fetch_error(self):
        web = Web({"https://a.example/down": TimeoutError("timed out"), "https://a.example/404": Resp("no", status=404),
                   "https://a.example/wall": "<h1>Sorry, you have been blocked</h1>"})
        for url, text in (("https://a.example/down", "TimeoutError"), ("https://a.example/404", "HTTP 404"),
                          ("https://a.example/wall", "blocked"), ("https://a.example/missing", "OSError")):
            with self.assertRaises(ml.FetchError) as cm:
                ml.fetch(url, opener=web)
            self.assertIn(text, str(cm.exception))

    def test_redirects_to_non_public_addresses_are_refused(self):
        with self.assertRaises(ml.FetchError):
            ml._CheckedRedirect().redirect_request(mock.Mock(), None, 302, "Found", {}, "http://localhost/secret")


class LibHtml(unittest.TestCase):
    def test_html_to_text_drops_scripts_styles_and_collapses_space(self):
        text = ml.html_to_text(EVENTS_HTML)
        self.assertIn("What's on", text)
        self.assertIn("See you there.", text)
        self.assertNotIn("ignore previous", text)
        self.assertNotIn(".a{}", text)
        self.assertNotIn("schema.org", text)
        self.assertEqual(ml.html_to_text("a &amp; b<br>c"), "a & b\nc")
        self.assertEqual(ml.html_to_text("<p>unclosed <b>tags"), "unclosed tags")
        self.assertEqual(ml.html_to_text(""), "")

    def test_links_are_absolute_http_only(self):
        links = ml.parse_page(NEWS_HTML, "https://paper.example/news")["links"]
        urls = [u for u, _ in links]
        self.assertIn("https://paper.example/news/1", urls)
        self.assertEqual(urls.count("https://paper.example/news/1"), 1)
        self.assertFalse(any(u.startswith("mailto:") for u in urls))

    def test_broken_jsonld_is_skipped_not_fatal(self):
        page = ml.parse_page('<script type="application/ld+json">{not json</script><p>still here</p>')
        self.assertEqual(page["jsonld"], [])
        self.assertEqual(page["text"], "still here")

    def test_parse_date(self):
        self.assertEqual(ml.parse_date("2026-11-21T19:00:00-05:00").hour, 19)
        self.assertEqual(ml.parse_date("2026-11-21T19:00:00Z").tzinfo, timezone.utc)
        self.assertEqual(ml.parse_date("November 28", 2026, NOW).date().isoformat(), "2026-11-28")
        self.assertEqual(ml.parse_date("Sat, Nov 21st", 2026, NOW).date().isoformat(), "2026-11-21")
        self.assertEqual(ml.parse_date("January 3", 2026, NOW).year, 2027)        # next January, not last
        self.assertEqual(ml.parse_date("Mon, 16 Nov 2026 11:00:00 +0000").day, 16)
        for junk in ("soon", "", None, "13/45/2026"):
            self.assertIsNone(ml.parse_date(junk))

    def test_clean_strips_control_characters_and_caps_length(self):
        self.assertEqual(ml.clean("a\n\nb\x00\tc  d"), "a b c d")
        self.assertEqual(len(ml.clean("x" * 500, 50)), 50)
        self.assertEqual(ml.clean(None), "")


class LibStore(Base):
    def test_seen_store_reports_an_item_once_across_runs(self):
        p = self.state / "seen.json"
        s = ml.SeenStore(p)
        self.assertTrue(s.add("a", NOW))
        self.assertFalse(s.add("a", NOW))
        s.save(NOW)
        s2 = ml.SeenStore(p)
        self.assertIn("a", s2)
        self.assertNotIn("b", s2)
        self.assertFalse(s2.add("a", NOW + timedelta(days=1)))
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_seen_store_is_bounded_by_count_and_age(self):
        p = self.state / "seen.json"
        s = ml.SeenStore(p, max_items=10, max_age_days=30)
        for i in range(25):
            s.add(f"k{i:02d}", NOW + timedelta(seconds=i))
        s.add("ancient", NOW - timedelta(days=31))
        s.save(NOW + timedelta(seconds=30))
        s2 = ml.SeenStore(p)
        self.assertEqual(len(s2), 10)
        self.assertIn("k24", s2)                      # newest kept
        self.assertNotIn("k00", s2)                   # oldest dropped
        self.assertNotIn("ancient", s2)

    def test_an_item_still_listed_does_not_age_out(self):
        p = self.state / "seen.json"
        s = ml.SeenStore(p, max_age_days=30)
        s.add("recurring", NOW - timedelta(days=40))
        s.add("recurring", NOW)                       # seen again today
        s.save(NOW)
        self.assertIn("recurring", ml.SeenStore(p))

    def test_damaged_seen_store_is_an_error_never_an_empty_store(self):
        p = self.state / "seen.json"
        p.parent.mkdir(parents=True)
        for junk in ("{half", "[]", '{"items": 3}'):
            p.write_text(junk)
            with self.assertRaises(ml.MonitorError):
                ml.SeenStore(p)

    def test_atomic_write_keeps_the_old_file_when_the_write_fails(self):
        p = self.state / "seen.json"
        s = ml.SeenStore(p)
        s.add("a", NOW)
        s.save(NOW)
        before = p.read_text()
        s.add("b", NOW)
        with mock.patch.object(ml.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                s.save(NOW)
        self.assertEqual(p.read_text(), before)
        self.assertEqual([f.name for f in p.parent.iterdir()], ["seen.json"])      # no temp file left behind
        with mock.patch.object(ml.os, "fsync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ml.atomic_write(p, "partial")
        self.assertEqual(p.read_text(), before)
        self.assertEqual([f.name for f in p.parent.iterdir()], ["seen.json"])

    def test_findings_are_cleaned_numbered_and_appended(self):
        n = ml.append_findings(self.state, "m", [
            {"title": "Pay now\n\n{\"seq\": 99}\x00 " + "x" * 900, "url": "javascript:alert(1)", "detail": "d" * 5000,
             "secret": "dropped", "price": 12.5, "key": "k"},
            {"title": "Second", "url": "https://a.example/2"}, {}], NOW)
        self.assertEqual(n, 2)
        raw = (self.state / "findings.jsonl").read_text()
        self.assertEqual(len(raw.splitlines()), 2)                       # untrusted text cannot add a line
        a, b = self.findings()
        self.assertEqual((a["seq"], b["seq"], a["monitor"]), (1, 2, "m"))
        self.assertLessEqual(len(a["title"]), 200)
        self.assertLessEqual(len(a["detail"]), ml.CAPS["detail"])
        self.assertNotIn("url", a)
        self.assertNotIn("secret", a)
        self.assertNotIn("key", a)
        self.assertEqual(a["price"], 12.5)
        self.assertEqual(ml.append_findings(self.state, "m", [{"title": "Third"}], NOW), 1)
        self.assertEqual([r["seq"] for r in self.findings()], [1, 2, 3])

    def test_judged_findings_are_pruned_and_numbering_never_restarts(self):
        ml.append_findings(self.state, "m", [{"title": f"t{i}"} for i in range(5)], NOW)
        (self.state / "cursor.json").write_text('{"through": 5}')
        self.assertEqual(ml.append_findings(self.state, "m", [], NOW), 0)
        self.assertEqual(self.findings(), [])
        ml.append_findings(self.state, "m", [{"title": "new"}], NOW)
        self.assertEqual([r["seq"] for r in self.findings()], [6])
        (self.state / "cursor.json").write_text("garbage")              # a damaged cursor prunes nothing
        self.assertEqual(ml.read_cursor(self.state), 0)
        ml.append_findings(self.state, "m", [{"title": "newer"}], NOW)
        self.assertEqual([r["seq"] for r in self.findings()], [6, 7])

    def test_findings_inbox_is_bounded_and_survives_bad_lines(self):
        ml.append_findings(self.state, "m", [{"title": f"t{i}"} for i in range(30)], NOW, inbox_max=10)
        self.assertEqual([r["seq"] for r in self.findings()], list(range(21, 31)))
        with open(self.state / "findings.jsonl", "a") as fh:
            fh.write("{broken\n[1]\n")
        ml.append_findings(self.state, "m", [{"title": "after"}], NOW, inbox_max=10)
        self.assertEqual(self.findings()[-1]["seq"], 31)

    def test_silent_unless(self):
        out = io.StringIO()
        self.assertEqual(ml.silent_unless(0, "nothing", out), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(ml.silent_unless(2, "2 new", out), 0)
        self.assertEqual(out.getvalue(), "2 new\n")


class CityEvents(Base):
    SRC = {"id": "venue-a", "name": "Example Venue", "url": "https://venue.example/events", "area": "Area A",
           "exclude_titles": "private event"}

    def test_new_events_reported_once_then_silence(self):
        self.config(city_events={"sources": [self.SRC]})
        web = Web({self.SRC["url"]: EVENTS_HTML})
        code, out, err = self.run_mod(city_events, web)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "city-events: 1 new item(s) for the skill\n")
        (f,) = self.findings()
        self.assertEqual(f["title"], "Jazz on the Pier & Friends")
        self.assertEqual(f["url"], "https://venue.example/events/jazz")
        self.assertEqual((f["location"], f["area"], f["source"], f["kind"]), ("Pier Stage", "Area A", "Example Venue", "event"))
        self.assertEqual(f["detail"], "Free live set")
        self.assertTrue(f["date"].startswith("2026-11-21T19:00"))
        code, out, err = self.run_mod(city_events, web, now=NOW + timedelta(days=1))
        self.assertEqual((code, out, err), (0, "", ""))                  # nothing new: no output at all
        self.assertEqual(len(self.findings()), 1)
        current = json.loads((self.state / "current.json").read_text())["items"]
        self.assertEqual([c["title"] for c in current], ["Jazz on the Pier & Friends"])   # still listed for the preview

    def test_configured_pattern_is_used_when_a_page_has_no_structured_events(self):
        src = {"id": "street-b", "name": "Street", "url": "https://street.example/cal", "pattern": PATTERN}
        self.config(city_events={"sources": [src]})
        code, out, _ = self.run_mod(city_events, Web({src["url"]: PATTERN_HTML}))
        self.assertEqual(code, 0)
        got = {f["title"]: f for f in self.findings()}
        self.assertEqual(set(got), {"Harvest Dance Party", "New Year Brunch"})
        self.assertTrue(got["Harvest Dance Party"]["date"].startswith("2026-11-28"))
        self.assertEqual(got["Harvest Dance Party"]["url"], "https://street.example/e/1")
        self.assertTrue(got["New Year Brunch"]["date"].startswith("2027-01-03"))
        self.assertEqual(got["New Year Brunch"]["url"], src["url"])      # a script link falls back to the source page

    def test_one_failing_source_is_logged_not_fatal_and_all_failing_fails_the_job(self):
        bad = {"id": "down", "name": "Down", "url": "https://down.example/"}
        self.config(city_events={"sources": [self.SRC, bad]})
        code, out, err = self.run_mod(city_events, Web({self.SRC["url"]: EVENTS_HTML}))
        self.assertEqual(code, 0)
        self.assertIn("source down failed", err)
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual([s["ok"] for s in status["sources"]], [True, False])
        self.config(city_events={"sources": [bad]})
        code, out, err = self.run_mod(city_events, Web({}))
        self.assertEqual((code, out), (1, ""))                           # failure goes to the log, never to chat

    def test_page_without_structured_events_hands_over_only_new_text(self):
        src = {"id": "cal", "name": "Calendar", "url": "https://cal.example/", "area": "Area B"}
        self.config(city_events={"sources": [src]})
        web = Web({src["url"]: "<ul><li>Nov 20 - Open mic night</li><li>Menu</li></ul>"})
        self.assertEqual(self.run_mod(city_events, web)[:2], (0, ""))    # first visit records the page, says nothing
        current = json.loads((self.state / "current.json").read_text())["items"]
        self.assertEqual([(c["kind"], c["url"]) for c in current], [("page", src["url"])])
        web.pages[src["url"]] = "<ul><li>Nov 20 - Open mic night</li><li>Nov 27 - 90s dance party</li></ul>"
        self.run_mod(city_events, web)
        (f,) = self.findings()
        self.assertEqual((f["kind"], f["detail"], f["area"]), ("page-change", "Nov 27 - 90s dance party", "Area B"))
        self.assertEqual(self.run_mod(city_events, web)[:2], (0, ""))

    def test_a_rewritten_page_is_handed_over_in_bounded_chunks_and_nothing_is_lost(self):
        src = {"id": "cal", "name": "Calendar", "url": "https://cal.example/", "mode": "page"}
        self.config(city_events={"sources": [src]})
        web = Web({src["url"]: "<p>Old line of text</p>"})
        self.run_mod(city_events, web)
        lines = [f"Dec {i % 28 + 1} - Event number {i:03d} at the hall" for i in range(200)]
        web.pages[src["url"]] = "".join(f"<p>{ln}</p>" for ln in lines)
        got, runs = "", 0
        while runs < 10:
            runs += 1
            self.run_mod(city_events, web)
            new = self.findings()
            if len(new) == 0 or " / ".join(f["detail"] for f in new) == got:
                break
            got = " / ".join(f["detail"] for f in new)
            self.assertLessEqual(len(new[-1]["detail"]), ml.CAPS["detail"])
        self.assertGreater(runs, 2)                                      # not one giant message
        self.assertEqual(got.split(" / "), lines)                        # every line, once, in order

    def test_unknown_mode_is_a_source_error(self):
        self.config(city_events={"sources": [{"id": "x", "url": "https://x.example/", "mode": "magic"}]})
        code, out, err = self.run_mod(city_events, Web({"https://x.example/": "<p>hi</p>"}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("unknown mode", err)

    def test_baseline_marks_everything_seen_and_reports_nothing(self):
        self.config(city_events={"sources": [self.SRC]})
        web = Web({self.SRC["url"]: EVENTS_HTML})
        self.assertEqual(self.run_mod(city_events, web, "--baseline")[:2], (0, ""))
        self.assertEqual(self.findings(), [])
        self.assertEqual(self.run_mod(city_events, web)[:2], (0, ""))

    def test_dry_run_writes_nothing(self):
        self.config(city_events={"sources": [self.SRC]})
        code, out, _ = self.run_mod(city_events, Web({self.SRC["url"]: EVENTS_HTML}), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["new"]), 1)
        self.assertFalse(self.state.exists())

    def test_max_new_holds_the_rest_for_the_next_run(self):
        src = {"id": "street-b", "name": "Street", "url": "https://street.example/cal", "pattern": PATTERN}
        self.config(city_events={"sources": [src]})
        web = Web({src["url"]: PATTERN_HTML})
        self.run_mod(city_events, web, "--max-new", "1")
        self.assertEqual(len(self.findings()), 1)
        self.run_mod(city_events, web, "--max-new", "1")
        self.assertEqual(len(self.findings()), 2)

    def test_bad_config_and_damaged_state_fail_loudly_in_the_log_only(self):
        code, out, err = self.run_mod(city_events, Web({}))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("config", err)
        self.config(other={})
        self.assertEqual(self.run_mod(city_events, Web({}))[0], 2)
        self.config(city_events={"sources": [self.SRC]})
        self.state.mkdir()
        (self.state / "seen.json").write_text("{half")
        code, out, err = self.run_mod(city_events, Web({self.SRC["url"]: EVENTS_HTML}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("damaged", err)
        self.assertEqual(self.findings(), [])                            # nothing re-sent on a guess


class LocalMonitor(Base):
    def test_headlines_filtered_by_keywords_and_reported_once(self):
        src = {"id": "paper-a", "name": "Paper", "url": "https://paper.example/news", "kind": "news", "mode": "links",
               "keywords": ["short-term rental", "zoning"]}
        self.config(local_monitor={"sources": [src]})
        web = Web({src["url"]: NEWS_HTML})
        code, out, _ = self.run_mod(local_monitor, web)
        self.assertEqual(code, 0)
        (f,) = self.findings()
        self.assertEqual(f["title"], "Town board postpones vote on short-term rental law")
        self.assertEqual((f["url"], f["kind"], f["category"]), ("https://paper.example/news/1", "headline", "news"))
        self.assertEqual(self.run_mod(local_monitor, web)[:2], (0, ""))
        web.pages[src["url"]] = NEWS_HTML + '<a href="/news/3">Zoning board approves the short-term rental law</a>'
        self.run_mod(local_monitor, web)                                 # a status change is a new item
        self.assertEqual(len(self.findings()), 2)

    def test_headlines_without_keywords_skip_navigation_links(self):
        src = {"id": "village", "url": "https://paper.example/news", "mode": "links"}
        self.config(local_monitor={"sources": [src]})
        self.run_mod(local_monitor, Web({src["url"]: NEWS_HTML}))
        titles = [f["title"] for f in self.findings()]
        self.assertEqual(len(titles), 2)
        self.assertNotIn("Home", titles)
        self.assertNotIn("About us", titles)

    def test_business_page_first_visit_is_silent_then_only_the_new_text_is_reported(self):
        src = {"id": "cafe-a", "name": "Example Cafe", "url": "https://cafe.example/", "kind": "business", "mode": "page"}
        self.config(local_monitor={"sources": [src]})
        web = Web({src["url"]: CAFE_V1})
        self.assertEqual(self.run_mod(local_monitor, web)[:2], (0, ""))
        self.assertEqual(self.findings(), [])
        self.assertEqual(self.run_mod(local_monitor, web)[:2], (0, ""))  # unchanged page
        web.pages[src["url"]] = CAFE_V2
        code, out, _ = self.run_mod(local_monitor, web)
        (f,) = self.findings()
        self.assertEqual((f["title"], f["kind"], f["category"]), ("Example Cafe: new text on the page", "page-change", "business"))
        self.assertIn("Live music this Saturday", f["detail"])
        self.assertNotIn("Open Thursday", f["detail"])
        self.assertEqual(self.run_mod(local_monitor, web)[:2], (0, ""))

    def test_events_mode_shares_the_event_extractor(self):
        src = {"id": "region", "name": "Region", "url": "https://region.example/events", "kind": "events",
               "mode": "events", "area": "Upstate"}
        self.config(local_monitor={"sources": [src]})
        self.run_mod(local_monitor, Web({src["url"]: EVENTS_HTML}))
        titles = sorted(f["title"] for f in self.findings())
        self.assertEqual(titles, ["Jazz on the Pier & Friends", "Private Event: closed"])
        self.assertTrue(all(f["kind"] == "event" for f in self.findings()))
        self.assertTrue((self.state / "current.json").exists())


class WinePrice(Base):
    def cfg_wine(self, **page):
        self.config(wine_watch={"items": [{"id": "wine-a", "name": "Example Pinot Noir 2021", "alert_below": 15.2,
                                           "pages": [{"retailer": "Shop", "url": "https://wine.example/p", **page}]}],
                                "offer_pages": [{"id": "offers-a", "name": "Offers", "url": "https://offers.example/",
                                                 "terms": ["statement credit"]}]})

    def test_price_at_or_above_threshold_is_silent(self):
        self.cfg_wine()
        for price in ("19.00", "15.20"):
            web = Web({"https://wine.example/p": WINE_LD % price, "https://offers.example/": "<p>Nothing for wine.</p>"})
            self.assertEqual(self.run_mod(wine_price, web)[:2], (0, ""))
        self.assertEqual(self.findings(), [])

    def test_price_drop_reported_once_a_month_and_offers_once(self):
        self.cfg_wine()
        web = Web({"https://wine.example/p": WINE_LD % "13.99", "https://offers.example/": OFFER_HTML})
        code, out, _ = self.run_mod(wine_price, web)
        self.assertEqual((code, out), (0, "wine-watch: 2 new item(s) for the skill\n"))
        drop, offer = self.findings()
        self.assertEqual((drop["kind"], drop["price"], drop["threshold"], drop["retailer"]), ("price-drop", 13.99, 15.2, "Shop"))
        self.assertEqual(offer["kind"], "offer")
        self.assertIn("$20 statement credit", offer["detail"])
        self.assertEqual(self.run_mod(wine_price, web, now=NOW + timedelta(days=3))[:2], (0, ""))
        self.run_mod(wine_price, web, now=NOW + timedelta(days=20))      # next month, still cheap: one reminder
        self.assertEqual([f["kind"] for f in self.findings()], ["price-drop", "offer", "price-drop"])

    def test_price_sources_meta_and_configured_pattern_but_never_a_guess(self):
        self.assertEqual(wine_price.find_prices(WINE_META), [14.25])     # the "$9.99 shipping" text is not a price
        self.assertEqual(wine_price.find_prices("<b>Only $12.00 today</b>"), [])
        self.assertEqual(wine_price.find_prices('<span class="now">$12.00</span>', r'class="now">(?P<price>[^<]+)<'), [12.0])
        self.assertEqual(wine_price.find_prices(WINE_LD % "free"), [])

    def test_unreadable_price_is_a_logged_source_failure(self):
        self.config(wine_watch={"items": [{"id": "wine-a", "alert_below": 15.2,
                                           "pages": [{"retailer": "Shop", "url": "https://wine.example/p"}]}]})
        code, out, err = self.run_mod(wine_price, Web({"https://wine.example/p": "<p>Call for price</p>"}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no readable price", err)


class FeedScan(Base):
    FEEDS = [{"id": "blog-a", "name": "Blog A", "url": "https://blog.example/feed/", "priority": "high"},
             {"id": "blog-b", "name": "Blog B", "url": "https://other.example/atom.xml"}]

    def test_rss_and_atom_new_articles_once(self):
        self.config(financial_feeds={"feeds": self.FEEDS})
        web = Web({self.FEEDS[0]["url"]: RSS, self.FEEDS[1]["url"]: ATOM})
        code, out, err = self.run_mod(feed_scan, web)
        self.assertEqual((code, err), (0, ""))
        got = {f["title"]: f for f in self.findings()}
        self.assertEqual(set(got), {"Bank bonus: $400 for a new & easy checking account", "Transfer bonus 30%"})
        self.assertEqual(got["Transfer bonus 30%"]["url"], "https://other.example/posts/transfer")
        self.assertEqual(got["Transfer bonus 30%"]["published"], "2026-11-15")
        self.assertEqual(got["Bank bonus: $400 for a new & easy checking account"]["priority"], "high")
        self.assertEqual(self.run_mod(feed_scan, web, now=NOW + timedelta(days=1))[:2], (0, ""))

    def test_same_article_with_different_tracking_link_is_not_new(self):
        self.config(financial_feeds={"feeds": self.FEEDS[:1]})
        web = Web({self.FEEDS[0]["url"]: RSS})
        self.run_mod(feed_scan, web)
        web.pages[self.FEEDS[0]["url"]] = RSS.replace("utm_source=rss", "utm_source=mail")
        self.assertEqual(self.run_mod(feed_scan, web)[:2], (0, ""))

    def test_hostile_or_broken_feeds_are_refused(self):
        bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel><item><title>&lol;</title></item></channel></rss>'
        for body in (bomb, "<rss><channel><item>", "<html>not a feed</html>"):
            self.config(financial_feeds={"feeds": self.FEEDS[:1]})
            code, out, err = self.run_mod(feed_scan, Web({self.FEEDS[0]["url"]: body}))
            self.assertEqual(out, "")
            self.assertEqual(self.findings(), [])
        with self.assertRaises(ml.FetchError):
            feed_scan.parse_feed(bomb, "https://blog.example/")


class Files(unittest.TestCase):
    def test_example_config_has_every_section_and_only_placeholders(self):
        cfg = json.loads((TOOLS / "monitors/config.example.json").read_text())
        for mod in (city_events, local_monitor, wine_price, feed_scan):
            self.assertIsInstance(cfg[mod.SECTION], dict, mod.SECTION)
        hosts = set(re.findall(r"https?://([^/\"\s]+)", (TOOLS / "monitors/config.example.json").read_text()))
        self.assertTrue(hosts)
        self.assertTrue(all(h.endswith(".example") for h in hosts), hosts)

    def test_example_config_runs_end_to_end_without_a_network(self):
        with tempfile.TemporaryDirectory() as d:
            for mod in (city_events, local_monitor, wine_price, feed_scan):
                out, err = io.StringIO(), io.StringIO()
                code = mod.main(["--config", str(TOOLS / "monitors/config.example.json"), "--state-dir",
                                 str(Path(d) / mod.NAME)], opener=Web({}), now=NOW, out=out, err=err)
                self.assertEqual((code, out.getvalue()), (1, ""), mod.NAME)      # every source "down": log only

    def test_skills_have_valid_front_matter_and_stay_short(self):
        for name in MONITOR_SKILLS:
            text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
            m = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n# .+\n", text)
            self.assertIsNotNone(m, name)
            self.assertEqual(m.group(1), name)
            self.assertGreater(len(m.group(2)), 40, name)
            self.assertLessEqual(len(text.splitlines()), 42, name)
            self.assertIn("the owner", text, name)
            self.assertRegex(text, r"rules/[\w./-]+\.md|state/kg-roadmap\.md", name)       # names its authority file
            self.assertRegex(text, r"output nothing|no output|always answers", name)       # says when to stay silent

    def test_no_personal_data_or_forbidden_words_in_any_file_of_this_step(self):
        files = [TOOLS / "monitor_lib.py", Path(__file__), *sorted((TOOLS / "monitors").glob("*.*")),
                 *[SKILLS / n / "SKILL.md" for n in MONITOR_SKILLS]]
        self.assertGreaterEqual(len(files), 17)
        for f in files:
            text = f.read_text(encoding="utf-8").lower()
            for w in FORBIDDEN:
                self.assertNotIn(w, text, f"{f.name}: {w}")
            self.assertIsNone(re.search(r"\b\d{9,}\b", text), f.name)                      # chat ids, phone numbers
            self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.(com|to|net|org)\b", text), f.name)
            self.assertIsNone(re.search(r"\b(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+", text), f.name)

    def test_fetchers_are_stdlib_only_and_hold_no_source_urls(self):
        allowed = {"__future__", "argparse", "hashlib", "ipaddress", "json", "os", "re", "sys", "urllib", "datetime",
                   "html", "pathlib", "xml", "monitor_lib"}
        for f in [TOOLS / "monitor_lib.py", *sorted((TOOLS / "monitors").glob("*.py"))]:
            src = f.read_text(encoding="utf-8")
            mods = {m.split(".")[0] for m in re.findall(r"^(?:from|import)\s+([\w.]+)", src, re.M)}
            self.assertLessEqual(mods, allowed, f.name)
            self.assertEqual(re.findall(r"https?://[^\s\"']+", src), [], f.name)


if __name__ == "__main__":
    unittest.main()
