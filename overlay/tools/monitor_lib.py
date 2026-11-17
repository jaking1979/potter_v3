#!/usr/bin/env python3
"""Shared parts of every Potter monitor. Standard library only, no model.

The monitor pattern: a command job FETCHES (a program under monitors/ built on this file), a skill JUDGES, and
the owner hears something only when the skill finds it worth saying.

  fetch()            one public page: http(s) only, timeout, size cap, fixed user agent, injectable opener
  parse_page()       HTML -> text, links, JSON-LD objects, meta tags (page content is data, never instructions)
  SeenStore          JSON file of item keys already reported, bounded by count and age, written atomically
  append_findings()  cleaned, length-capped items appended to a JSON-lines inbox that a skill reads
  silent_unless()    nothing new -> no output, exit 0
  collect_sources()  the three ways a page becomes findings: structured events, headlines, changed text
  run_monitor()      the shared command line and run order used by every fetcher

State directory of one monitor (the skill reads these, see the monitor skills):
  findings.jsonl  one JSON object per new item, each with a rising "seq"
  cursor.json     {"through": <seq>} written by the skill after judging; older findings are pruned on the next run
  current.json    everything the sources list right now (new or not), for digests that need the whole picture
  status.json     per-source result of the last run, for the health check
  seen.json       the seen store
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

USER_AGENT = "PotterMonitor/3 (personal assistant; public pages only)"
TIMEOUT, MAX_BYTES = 20, 2_000_000
SEEN_MAX, SEEN_DAYS = 5000, 90
INBOX_MAX, MAX_NEW = 500, 40
CAPS = {"title": 200, "detail": 1500, "url": 500}
FIELDS = ("id", "kind", "category", "source", "source_id", "area", "title", "date", "location", "url", "detail", "retailer",
          "price", "threshold", "published", "priority")
BLOCKED = re.compile(r"sorry, you have been blocked|why have i been blocked|attention required! \| cloudflare", re.I)
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
BREAK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "td", "th"}
DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%A, %B %d, %Y", "%a, %b %d, %Y", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z", "%m/%d/%Y", "%B %d %Y", "%b %d %Y")


class MonitorError(Exception):
    """Anything that should fail the job (and so reach the schedule watchdog) instead of being guessed around."""


class FetchError(MonitorError):
    pass


# ---------------------------------------------------------------- text helpers
def clean(value, n: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def item_id(*parts) -> str:
    return hashlib.sha256("|".join(str(p).strip().lower() for p in parts).encode("utf-8")).hexdigest()[:20]


def parse_date(value, default_year: int | None = None, now: datetime | None = None) -> datetime | None:
    """Best effort, never guesses silently: unknown shapes return None. Naive results are treated as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value.strip())
    d = None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        tries = [(s, f) for f in DATE_FORMATS]
        if default_year:
            tries += [(f"{s}, {default_year}", f) for f in ("%B %d, %Y", "%b %d, %Y", "%A, %B %d, %Y", "%a, %b %d, %Y")]
        for text, fmt in tries:
            try:
                d = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if d is not None and default_year and str(default_year) not in s and now is not None:
            ref = now.replace(tzinfo=None)
            if d < ref - timedelta(days=180):          # "January 5" seen in December means next January
                d = d.replace(year=d.year + 1)
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def in_window(d: datetime | None, now: datetime, days: int) -> bool:
    """Undated items pass (the skill decides); dated ones must be from yesterday up to `days` ahead."""
    if d is None:
        return True
    return now - timedelta(days=1) <= d <= now + timedelta(days=days)


# ---------------------------------------------------------------- fetching
def check_url(url: str) -> str:
    parts = urlsplit(url if isinstance(url, str) else "")
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise FetchError(f"not a public http(s) address: {clean(str(url), 80)}")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise FetchError("address is not public")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise FetchError("address is not public")
    except ValueError:
        pass                                            # a normal host name
    return url


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener(request, timeout):
    return urllib.request.build_opener(_CheckedRedirect).open(request, timeout=timeout)


def fetch(url: str, opener=None, timeout: int = TIMEOUT, max_bytes: int = MAX_BYTES,
          user_agent: str = USER_AGENT) -> str:
    """Return the page as text. `opener(request, timeout)` is injected in tests; nothing else touches the network."""
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5"})
    try:
        resp = (opener or default_opener)(req, timeout)
        status = getattr(resp, "status", None) or 200
        raw = resp.read(max_bytes + 1)
        charset = None
        try:
            charset = resp.headers.get_content_charset()
        except AttributeError:
            pass
        close = getattr(resp, "close", None)
        if close:
            close()
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}")
    except Exception as e:                              # timeouts, DNS, TLS, resets: all one kind of failure
        raise FetchError(f"{type(e).__name__}: {clean(str(e), 120)}")
    if status >= 400:
        raise FetchError(f"HTTP {status}")
    raw = raw[:max_bytes]
    try:
        text = raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if BLOCKED.search(text[:20000]):
        raise FetchError("blocked by the site's firewall")
    return text


# ---------------------------------------------------------------- HTML
class _Doc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.jsonld, self.meta = [], [], [], {}
        self._skip, self._ld, self._a = 0, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and "ld+json" in (a.get("type") or "").lower():
            self._ld = []
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BREAK_TAGS:
            self.text.append("\n")
        if tag == "a" and a.get("href"):
            self._a = [a["href"], []]
        if tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and a.get("content") is not None:
                self.meta.setdefault(key.lower(), a["content"])
        elif a.get("itemprop") and a.get("content") is not None:
            self.meta.setdefault(a["itemprop"].lower(), a["content"])

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.jsonld.append("".join(self._ld))
            self._ld = None
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BREAK_TAGS:
            self.text.append("\n")
        if tag == "a" and self._a:
            self.links.append((self._a[0], "".join(self._a[1])))
            self._a = None

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        elif not self._skip:
            self.text.append(data)
            if self._a:
                self._a[1].append(data)


def _flatten(x, out):
    if isinstance(x, list):
        for v in x:
            _flatten(v, out)
    elif isinstance(x, dict):
        out.append(x)
        for v in x.values():
            if isinstance(v, (dict, list)):
                _flatten(v, out)
    return out


def parse_page(html: str, base_url: str = "") -> dict:
    """{"text", "links": [(absolute url, text)], "jsonld": [flattened objects], "meta": {name: content}}"""
    doc = _Doc()
    try:
        doc.feed(html or "")
        doc.close()
    except Exception:
        pass                                            # keep whatever was parsed from a broken page
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", ln).strip() for ln in "".join(doc.text).split("\n")]
    links, seen = [], set()
    for href, label in doc.links:
        url = urljoin(base_url, href.strip())
        if urlsplit(url).scheme in ("http", "https") and (url, label) not in seen:
            seen.add((url, label))
            links.append((url, clean(label, 300)))
    objs = []
    for raw in doc.jsonld:
        try:
            _flatten(json.loads(re.sub(r"^\s*<!--|-->\s*$", "", raw.strip())), objs)
        except ValueError:
            continue                                    # some sites emit broken JSON-LD; other extractors still run
    return {"text": "\n".join(ln for ln in lines if ln), "links": links, "jsonld": objs, "meta": doc.meta}


def html_to_text(html: str) -> str:
    return parse_page(html)["text"]


def _http_url(value, base: str) -> str:
    url = urljoin(base, value) if isinstance(value, str) and value.strip() else base
    return url if urlsplit(url).scheme in ("http", "https") else base


def extract_events(html: str, base_url: str, pattern: str | None = None, now: datetime | None = None) -> list[dict]:
    """schema.org Event objects from JSON-LD; if there are none and the source has a configured regex `pattern`
    (named groups: title, and optionally date, location, url), that is used on the raw HTML instead."""
    now = now or datetime.now(timezone.utc)
    out = []
    for o in parse_page(html, base_url)["jsonld"]:
        typ = o.get("@type")
        typ = " ".join(map(str, typ)) if isinstance(typ, list) else str(typ or "")
        title = o.get("name") or o.get("headline")
        if not isinstance(title, str) or not ("event" in typ.lower() or o.get("startDate")):
            continue
        loc = o.get("location")
        if isinstance(loc, list):
            loc = loc[0] if loc else ""
        if isinstance(loc, dict):
            addr = loc.get("address")
            loc = loc.get("name") or (addr.get("streetAddress") if isinstance(addr, dict) else addr) or ""
        start = o.get("startDate")
        d = parse_date(start if isinstance(start, str) else "")
        out.append({"title": clean(unescape(title), CAPS["title"]), "when": d,
                    "location": clean(unescape(loc) if isinstance(loc, str) else ""),
                    "url": _http_url(o.get("url") or o.get("@id"), base_url),
                    "detail": clean(html_to_text(o.get("description") if isinstance(o.get("description"), str) else ""),
                                    CAPS["detail"])})
    if not out and pattern:
        for m in re.finditer(pattern, html, re.I | re.S):
            g = m.groupdict()
            title = clean(html_to_text(g.get("title") or ""), CAPS["title"])
            if not title:
                continue
            out.append({"title": title, "when": parse_date(html_to_text(g.get("date") or ""), now.year, now),
                        "location": clean(html_to_text(g.get("location") or "")),
                        "url": _http_url(g.get("url"), base_url), "detail": ""})
    return out


# ---------------------------------------------------------------- sources -> items
DEAD = re.compile(r"sold out|cancelled|canceled|postponed", re.I)
MIN_WORDS, MIN_LINE, CHUNK, MAX_CHUNKS = 4, 12, 1300, 3


def _event_items(src, events, now, days):
    skip = re.compile(src["exclude_titles"], re.I) if src.get("exclude_titles") else None
    out = []
    for e in events:
        if DEAD.search(e["title"]) or (skip and skip.search(e["title"])) or not in_window(e["when"], now, days):
            continue
        date = e["when"].isoformat(timespec="minutes") if e["when"] else ""
        key = item_id(e["title"], date[:10], e["location"])
        out.append({"key": key, "kind": "event", "title": e["title"], "date": date, "location": e["location"],
                    "url": e["url"], "detail": e["detail"]})
    return out


def _link_items(src, page, cap):
    words = [str(k).lower() for k in src.get("keywords") or [] if str(k).strip()]
    out, titles = [], set()
    for url, label in page["links"]:
        low = label.lower()
        if len(label.split()) < MIN_WORDS or low in titles or (words and not any(w in low for w in words)):
            continue
        titles.add(low)
        out.append({"key": item_id(src["id"], label), "kind": "headline", "title": label, "url": url})
        if len(out) >= cap:
            break
    return out


def _page_items(src, page, seen, now):
    """Text that was not on the page last time, in a few bounded chunks. The first visit only records the page."""
    sid = str(src["id"])
    lines = [ln for ln in dict.fromkeys(page["text"].split("\n")) if len(ln) >= MIN_LINE and " " in ln]
    keyed = [(item_id(sid, "line", ln), ln) for ln in lines]
    if f"{sid}|baseline" not in seen:
        for key in [f"{sid}|baseline"] + [k for k, _ in keyed]:
            seen.add(key, now)
        return []
    out, chunk, keys = [], [], []
    for key, ln in [(k, ln) for k, ln in keyed if k not in seen] + [(None, None)]:
        if key is None or sum(len(x) + 3 for x in chunk) + len(ln) > CHUNK:
            if chunk:
                text = " / ".join(chunk)
                out.append({"key": item_id(sid, "change", text), "kind": "page-change", "also_seen": keys,
                            "title": f"{src.get('name') or sid}: new text on the page", "url": src["url"],
                            "detail": text})
            chunk, keys = [], []
            if key is None or len(out) >= MAX_CHUNKS:
                break                                   # the rest stays unseen and is picked up next run
        chunk.append(clean(ln, CHUNK))
        keys.append(key)
    return out


def collect_sources(cfg: dict, get, now: datetime, seen, default_mode: str = "auto", default_kind: str = "news"):
    """One pass over cfg["sources"]. Per source "mode":
         events  schema.org events from JSON-LD, or the source's configured regex "pattern"
         links   headlines: links whose text is a real sentence (matching a keyword, if the source lists any)
         page    the lines of text that are new since the last visit (for pages with no structure at all)
         auto    events when the page has them, otherwise page
       Each item's "kind" says what it is (event, headline, page-change); its "category" is the source's own
       "kind" from the config (news, business, ...). Returns (items, statuses, current) for run_monitor."""
    days = cfg.get("window_days", 90)
    days = days if isinstance(days, int) and 0 < days <= 366 else 90
    cap = cfg.get("max_per_source", 15)
    cap = cap if isinstance(cap, int) and 0 < cap <= 100 else 15
    items, statuses, current = [], [], []
    for src in cfg.get("sources") or []:
        if not isinstance(src, dict) or not src.get("id") or not src.get("url"):
            continue
        sid, name, mode = str(src["id"]), str(src.get("name") or src["id"]), str(src.get("mode") or default_mode)
        base = {"source": name, "source_id": sid, "area": str(src.get("area") or "")}
        if mode not in ("auto", "events", "links", "page"):
            statuses.append({"source": sid, "ok": False, "error": f"unknown mode {clean(mode, 20)}"})
            continue
        try:
            html = get(src["url"])
            found, used = [], mode
            if mode in ("events", "auto"):
                found = _event_items(src, extract_events(html, src["url"], src.get("pattern"), now), now, days)
                current += [clean_item({**base, **f}) for f in found]
                used = "events"
            if mode == "links":
                found, used = _link_items(src, parse_page(html, src["url"]), cap), "links"
            elif mode == "page" or (mode == "auto" and not found):
                found, used = _page_items(src, parse_page(html, src["url"]), seen, now), "page"
                current.append(clean_item({**base, "kind": "page", "url": src["url"],
                                           "title": f"{name}: no structured listing, read the page"}))
        except (FetchError, re.error) as e:
            statuses.append({"source": sid, "ok": False, "error": clean(str(e), 160)})
            continue
        for f in found:
            items.append({"id": f["key"], "category": str(src.get("kind") or default_kind), **base, **f})
        statuses.append({"source": sid, "ok": True, "mode": used, "extracted": len(found)})
    current.sort(key=lambda r: (r.get("date") or "9999", r.get("title", "")))
    return items, statuses, current


# ---------------------------------------------------------------- files
def atomic_write(path: Path, text: str) -> None:
    """Write the whole file or leave the old one untouched. Never a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue                                    # one bad line never hides the rest
        if isinstance(r, dict):
            out.append(r)
    return out


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


class SeenStore:
    """Keys already reported. A missing file is an empty store; an unreadable one is an error, because guessing
    "nothing seen yet" would re-send everything."""

    def __init__(self, path: Path, max_items: int = SEEN_MAX, max_age_days: int = SEEN_DAYS):
        self.path, self.max_items, self.max_age = Path(path), max_items, timedelta(days=max_age_days)
        self.items: dict[str, dict] = {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            raise MonitorError(f"seen store unreadable: {e}")
        try:
            items = json.loads(raw)["items"]
            assert isinstance(items, dict)
        except (ValueError, KeyError, TypeError, AssertionError):
            raise MonitorError(f"seen store is damaged: {self.path}")
        self.items = {str(k): v for k, v in items.items() if isinstance(v, dict)}

    def __contains__(self, key: str) -> bool:
        return key in self.items

    def __len__(self) -> int:
        return len(self.items)

    def add(self, key: str, now: datetime) -> bool:
        """Record a sighting. True when the key is new."""
        stamp = _iso(now)
        if key in self.items:
            self.items[key]["last"] = stamp
            return False
        self.items[key] = {"first": stamp, "last": stamp}
        return True

    def save(self, now: datetime) -> None:
        cutoff = _iso(now - self.max_age)
        kept = sorted(((k, v) for k, v in self.items.items() if str(v.get("last", "")) >= cutoff),
                      key=lambda kv: str(kv[1].get("last", "")))[-self.max_items:]
        self.items = dict(kept)
        atomic_write(self.path, json.dumps({"version": 1, "items": self.items}, indent=1) + "\n")


def read_cursor(state_dir: Path) -> int:
    try:
        v = json.loads((Path(state_dir) / "cursor.json").read_text(encoding="utf-8")).get("through", 0)
        return v if isinstance(v, int) and v >= 0 else 0
    except (OSError, ValueError, AttributeError):
        return 0


def clean_item(item: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = item.get(k)
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str) and v.strip():
            if k == "url" and urlsplit(v).scheme not in ("http", "https"):
                continue
            out[k] = clean(v, CAPS.get(k, 120))
    return out


def append_findings(state_dir: Path, monitor: str, items: list[dict], now: datetime, inbox_max: int = INBOX_MAX) -> int:
    """Append cleaned items to findings.jsonl with rising seq numbers; drop what the skill has already judged
    (seq <= cursor) and anything beyond the newest `inbox_max`. Returns how many were appended."""
    state_dir = Path(state_dir)
    path, cursor = state_dir / "findings.jsonl", read_cursor(state_dir)
    old = read_jsonl(path)
    seq = max([cursor] + [r["seq"] for r in old if isinstance(r.get("seq"), int)])
    keep = [r for r in old if isinstance(r.get("seq"), int) and r["seq"] > cursor]
    added = 0
    for it in items:
        rec = clean_item(it)
        if not rec:
            continue
        seq += 1
        keep.append({"seq": seq, "ts": _iso(now), "monitor": monitor, **rec})
        added += 1
    if added or len(keep) != len(old):
        atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep[-inbox_max:]))
    return added


def silent_unless(count: int, line: str, out=None) -> int:
    """The silence rule: with nothing new, print nothing. Always exit code 0."""
    if count > 0:
        print(line, file=out or sys.stdout)
    return 0


# ---------------------------------------------------------------- the shared run
def load_section(config_path: str, section: str) -> dict:
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except OSError as e:
        raise MonitorError(f"config not readable: {e}")
    except ValueError:
        raise MonitorError("config is not valid JSON")
    sec = cfg.get(section) if isinstance(cfg, dict) else None
    if not isinstance(sec, dict):
        raise MonitorError(f'config has no "{section}" section')
    return sec


def run_monitor(name: str, section: str, collect, argv: list[str], opener=None, now: datetime | None = None,
                out=None, err=None) -> int:
    """collect(cfg, get, now, seen) -> (items, statuses, current)
         items     dicts with a "key" (what makes it the same item next time) plus finding fields
         statuses  [{"source", "ok", "error"?}] one per source tried
         current   list for current.json, or None
       Exit codes: 0 ran (also when nothing is new), 1 every source failed or state is damaged, 2 bad config."""
    out, err = out or sys.stdout, err or sys.stderr
    ap = argparse.ArgumentParser(prog=name)
    ap.add_argument("--config", required=True, help="JSON file with this monitor's sources (see config.example.json)")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--max-new", type=int, default=MAX_NEW, help="most new items handed to the skill per run")
    ap.add_argument("--baseline", action="store_true", help="mark everything listed now as seen; report nothing")
    ap.add_argument("--dry-run", action="store_true", help="print what would be reported; write nothing")
    ns = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    state = Path(ns.state_dir)
    try:
        cfg = load_section(ns.config, section)
    except MonitorError as e:
        print(f"{name}: {e}", file=err)
        return 2
    try:
        seen = SeenStore(state / "seen.json")
        items, statuses, current = collect(cfg, lambda url: fetch(url, opener=opener), now, seen)
        fresh, keys = [], set()
        for it in items:
            key = str(it.get("key") or "")
            if not key or key in keys or key in seen:
                if key in seen:
                    seen.add(key, now)                  # still listed: keep it from ageing out
                continue
            keys.add(key)
            fresh.append(it)
        if not ns.baseline:
            fresh = fresh[: max(0, ns.max_new)]         # the rest stays unseen and waits for the next run
        report = [] if ns.baseline else fresh           # baseline: everything becomes seen, nothing is reported
        if ns.dry_run:
            print(json.dumps({"new": [clean_item(i) for i in report], "sources": statuses}, indent=1), file=out)
            return 0
        added = append_findings(state, name, report, now)          # findings first: a crash repeats, never loses
        for it in fresh:
            for key in [it["key"], *(it.get("also_seen") or [])]:
                seen.add(str(key), now)
        seen.save(now)
        atomic_write(state / "status.json", json.dumps({"ran_at": _iso(now), "new": added, "sources": statuses},
                                                       indent=1) + "\n")
        if current is not None:
            atomic_write(state / "current.json", json.dumps({"generated_at": _iso(now), "items": current[:400]},
                                                            ensure_ascii=False, indent=1) + "\n")
    except MonitorError as e:
        print(f"{name}: {e}", file=err)
        return 1
    failed = [s for s in statuses if not s.get("ok")]
    for s in failed:
        print(f"{name}: source {s.get('source')} failed: {s.get('error')}", file=err)      # the log, not chat
    if statuses and len(failed) == len(statuses):
        return 1
    return silent_unless(added, f"{name}: {added} new item(s) for the skill", out)
