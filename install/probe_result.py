#!/usr/bin/env python3
"""Read the output of `openclaw models status --probe --json` on stdin and print one line:

  (empty)            every checked model answered
  REFUSED: <why>     OpenRouter answered and refused (bad key, no credit, unknown model): the install stops
  UNCLEAR: <why>     the check itself could not be read (OpenClaw's own error, if any): the install warns and goes on

Never prints a key: anything shaped like an OpenRouter key is masked. Exit 0 either way."""
import json
import re
import sys

KEY = re.compile(r"sk-or-[A-Za-z0-9_-]+")


def _clean(s) -> str:
    return KEY.sub("sk-or-…", re.sub(r"\x1b\[[0-9;]*m", "", str(s))).strip()[:300]


def parse(text: str):
    """The first JSON object in the output, even with log lines around it."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(obj, dict) and ("auth" in obj or "results" in obj or "error" in obj):
                return obj
    return None


def verdict(text: str) -> str:
    d = parse(text)
    if d is None:
        last = [ln for ln in (text or "").splitlines() if ln.strip() and "Deprecation" not in ln]
        return "UNCLEAR: " + (_clean(last[-1]) if last else "the check printed nothing")
    if isinstance(d.get("error"), dict) or d.get("ok") is False:
        e = d.get("error")
        return "UNCLEAR: " + _clean(e.get("message") if isinstance(e, dict) else e)
    res = ((d.get("auth") or {}).get("probes") or {}).get("results") or d.get("results") or []
    if not res:
        return "UNCLEAR: no OpenRouter model was checked"
    bad = [f"{r.get('model')}: {r.get('status')} ({_clean(r.get('error'))})" for r in res
           if isinstance(r, dict) and r.get("error")]
    return ("REFUSED: " + "; ".join(bad))[:400] if bad else ""


def problems(data) -> str:            # kept for callers that already parsed the JSON
    return verdict(json.dumps(data)) if isinstance(data, dict) else verdict("")


if __name__ == "__main__":
    print(verdict(sys.stdin.read()))
