#!/usr/bin/env python3
"""Read `openclaw models status --probe --json` on stdin. Print nothing when every model answered; otherwise one
line with each model's status and the provider's own error (never the key). Exit 0 either way."""
import json
import sys


def problems(data) -> str:
    if not isinstance(data, dict):
        return "the check returned nothing readable"
    res = ((data.get("auth") or {}).get("probes") or {}).get("results") or data.get("results") or []
    if not res:
        return "no OpenRouter model was checked"
    bad = [f"{r.get('model')}: {r.get('status')} ({r.get('error')})" for r in res if isinstance(r, dict) and r.get("error")]
    return "; ".join(bad)[:400]


def parse(text: str):
    """The JSON object in the output, even when log lines are printed before or after it."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(obj, dict) and ("auth" in obj or "results" in obj):
                return obj
    return None


if __name__ == "__main__":
    print(problems(parse(sys.stdin.read())))
