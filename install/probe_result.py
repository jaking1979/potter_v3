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


if __name__ == "__main__":
    try:
        d = json.loads(sys.stdin.read() or "null")
    except ValueError:
        d = None
    print(problems(d))
