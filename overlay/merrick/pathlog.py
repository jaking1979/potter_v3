"""Path log: one JSON line per request, so fast-path misses can be counted and the fast path widened.

  {"ts": "...Z", "id": "...", "intent": "set_temperature" | "fallback", "path": "fast" | "fallback",
   "ms": 412, "outcome": "ok", "source": "table" | "model" | "none", "reason": "...", "request": "..."}

A fallback is finished by the agent, not by this code, so when it ends one extra line is appended with
"event": "fallback_end" and the same id. report() counts requests from the first kind of line only and uses the
second kind for how long fallbacks really took and how they ended.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

MAX_REQUEST_CHARS = 200


def _iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def record(intent: str, path: str, ms: float, outcome: str, *, request: str = "", source: str = "",
           reason: str = "", request_id: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    if path not in ("fast", "fallback"):
        raise ValueError("path must be 'fast' or 'fallback'")
    return {"ts": _iso(now), "id": request_id or new_id(), "intent": "fallback" if path == "fallback" else intent,
            "path": path, "ms": int(round(ms)), "outcome": outcome, "source": source, "reason": reason,
            "request": " ".join(str(request).split())[:MAX_REQUEST_CHARS]}


def fallback_end(request_id: str, ms: float, outcome: str, now: Optional[datetime] = None) -> dict:
    return {"ts": _iso(now), "id": request_id, "event": "fallback_end", "ms": int(round(ms)), "outcome": outcome}


def append(log_path, rec: dict) -> bool:
    """Append one line. Returns False (never raises) if the log cannot be written: a full disk must not stop a
    request from being answered, but the caller is told so it can say the log is broken."""
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))   # one write = one whole line
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def read(log_path) -> list[dict]:
    out = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue                                    # a torn line is skipped, not fatal
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def _pct(values: list, q: float) -> Optional[int]:
    if not values:
        return None
    s = sorted(values)
    return int(s[min(len(s) - 1, int(round(q * (len(s) - 1))))])


def _shape(text: str) -> str:
    """Group near-identical requests: lower-case, numbers replaced, so 'set heat to 68' and '...to 70' count together."""
    return re.sub(r"\d+(\.\d+)?", "<n>", " ".join(str(text).lower().split()))


def report(records: Iterable[dict], since: Optional[str] = None, examples: int = 10) -> dict:
    """Counts by path, intent and fallback reason; timings; and the most common missed requests."""
    reqs, ends = [], {}
    for r in records:
        if not isinstance(r, dict) or (since and str(r.get("ts", "")) < since):
            continue
        if r.get("event") == "fallback_end":
            ends[r.get("id")] = r
        elif r.get("path") in ("fast", "fallback"):
            reqs.append(r)
    fast = [r for r in reqs if r["path"] == "fast"]
    misses = [r for r in reqs if r["path"] == "fallback"]
    done = [ends[r.get("id")] for r in misses if r.get("id") in ends]
    shapes, sample = Counter(), {}
    for r in misses:
        k = _shape(r.get("request", ""))
        shapes[k] += 1
        sample.setdefault(k, {"request": r.get("request", ""), "reason": r.get("reason", "")})
    fast_ms = [r.get("ms", 0) for r in fast]
    fb_ms = [e.get("ms", 0) for e in done]
    return {
        "total": len(reqs), "fast": len(fast), "fallback": len(misses),
        "fallback_rate": round(len(misses) / len(reqs), 3) if reqs else None,
        "by_intent": dict(Counter(r.get("intent") for r in fast).most_common()),
        "fast_outcomes": dict(Counter(r.get("outcome") for r in fast).most_common()),
        "fallback_reasons": dict(Counter(r.get("reason") or "unknown" for r in misses).most_common()),
        "fallback_outcomes": dict(Counter(e.get("outcome") for e in done).most_common()),
        "fallback_unfinished": len(misses) - len(done),       # started and never reported an end: lost or still running
        "fast_ms": {"median": _pct(fast_ms, 0.5), "p95": _pct(fast_ms, 0.95)},
        "fallback_ms": {"median": _pct(fb_ms, 0.5), "p95": _pct(fb_ms, 0.95)},
        "miss_examples": [{"count": n, **sample[k]} for k, n in shapes.most_common(examples)],
    }


def render(rep: dict) -> str:
    if not rep.get("total"):
        return "Merrick path log: no requests recorded."
    lines = [f"Merrick requests: {rep['total']} ({rep['fast']} fast, {rep['fallback']} fallback, "
             f"{round(100 * rep['fallback_rate'])}% missed the fast path)",
             f"Fast path: median {rep['fast_ms']['median']} ms, p95 {rep['fast_ms']['p95']} ms"]
    if rep["fallback_ms"]["median"] is not None:
        lines.append(f"Fallback: median {rep['fallback_ms']['median']} ms, p95 {rep['fallback_ms']['p95']} ms; "
                     f"{rep['fallback_unfinished']} never reported an end")
    if rep["fallback_reasons"]:
        lines.append("Why requests missed: " + ", ".join(f"{k} {v}" for k, v in rep["fallback_reasons"].items()))
    for m in rep["miss_examples"]:
        lines.append(f"- {m['count']}x [{m['reason'] or 'unknown'}] {m['request']}")
    return "\n".join(lines)
