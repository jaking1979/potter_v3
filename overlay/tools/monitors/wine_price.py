#!/usr/bin/env python3
"""Wine price fetcher: product pages -> a finding only when a price is below the owner's threshold; offer pages
-> a finding when a watched phrase appears. Everything else is silence.

No model. A price is read from structured data only (JSON-LD offers, price meta tags, or the page's configured
regex with a named group "price"); a price that cannot be read reliably is a source error in the log, never a
guess. Each (wine, retailer, price) is reported at most once per calendar month.

  wine_price.py --config FILE --state-dir DIR [--baseline] [--dry-run]

Config section "wine_watch": {"items": [{"id","name","alert_below": 0.0, "pages": [{"retailer","url",
"pattern"?}]}], "offer_pages": [{"id","name","url","terms": [...]}], "search_queries": [...]}.
The search queries are for the wine-watch skill; this program only reads the listed pages.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor_lib as ml  # noqa: E402

NAME, SECTION = "wine-watch", "wine_watch"
META_KEYS = ("product:price:amount", "og:price:amount", "price")


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if 0 < v < 100000 else None
    if isinstance(v, str):
        m = re.fullmatch(r"\s*\$?\s*(\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)\s*", v)
        if m:
            return _num(float(m.group(1).replace(",", "")))
    return None


def find_prices(html: str, pattern: str | None = None) -> list[float]:
    page = ml.parse_page(html)
    found = []
    for o in page["jsonld"]:
        typ = o.get("@type")
        typ = " ".join(map(str, typ)) if isinstance(typ, list) else str(typ or "")
        if "offer" in typ.lower():
            found += [_num(o.get(k)) for k in ("price", "lowPrice")]
    if not any(found):
        found += [_num(page["meta"].get(k)) for k in META_KEYS]
    if not any(found) and pattern:
        found += [_num(m.group("price")) for m in re.finditer(pattern, html, re.I | re.S)]
    return sorted({p for p in found if p})


def collect(cfg, get, now, seen):
    items, statuses = [], []
    for wine in cfg.get("items") or []:
        if not isinstance(wine, dict) or not wine.get("id"):
            continue
        limit = _num(wine.get("alert_below"))
        for page in wine.get("pages") or []:
            if not isinstance(page, dict) or not page.get("url"):
                continue
            sid = f"{wine['id']}@{page.get('retailer') or page['url']}"
            try:
                prices = find_prices(get(page["url"]), page.get("pattern"))
            except (ml.FetchError, re.error, IndexError) as e:
                statuses.append({"source": sid, "ok": False, "error": ml.clean(str(e), 160)})
                continue
            if not prices or limit is None:
                statuses.append({"source": sid, "ok": False,
                                 "error": "no readable price" if limit is not None else "no alert_below in config"})
                continue
            statuses.append({"source": sid, "ok": True, "price": prices[0]})
            if prices[0] < limit:
                key = ml.item_id(wine["id"], page.get("retailer"), f"{prices[0]:.2f}", now.strftime("%Y-%m"))
                items.append({"key": key, "id": key, "kind": "price-drop", "source": str(wine.get("name") or wine["id"]),
                              "source_id": str(wine["id"]), "retailer": str(page.get("retailer") or ""),
                              "title": f"{wine.get('name') or wine['id']}: {prices[0]:.2f} (alert below {limit:.2f})",
                              "price": prices[0], "threshold": limit, "url": page["url"]})
    for op in cfg.get("offer_pages") or []:
        if not isinstance(op, dict) or not op.get("id") or not op.get("url"):
            continue
        try:
            text = ml.parse_page(get(op["url"]))["text"]
        except ml.FetchError as e:
            statuses.append({"source": str(op["id"]), "ok": False, "error": ml.clean(str(e), 160)})
            continue
        statuses.append({"source": str(op["id"]), "ok": True})
        flat = re.sub(r"\s+", " ", text)
        for term in [str(t) for t in op.get("terms") or [] if str(t).strip()]:
            i = flat.lower().find(term.lower())
            if i < 0:
                continue
            excerpt = flat[max(0, i - 150): i + len(term) + 200]
            key = ml.item_id(op["id"], term, excerpt)
            items.append({"key": key, "id": key, "kind": "offer", "source": str(op.get("name") or op["id"]),
                          "source_id": str(op["id"]), "title": f"{op.get('name') or op['id']}: mentions \"{term}\"",
                          "detail": excerpt, "url": op["url"]})
    return items, statuses, None


def main(argv: list[str], opener=None, now=None, out=None, err=None) -> int:
    return ml.run_monitor(NAME, SECTION, collect, argv, opener=opener, now=now, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
