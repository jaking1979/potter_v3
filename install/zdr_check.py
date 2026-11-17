#!/usr/bin/env python3
"""Which models Potter uses have no zero-data-retention endpoint on OpenRouter?

An OpenRouter account set to require zero data retention refuses every other model (HTTP 404, "ZDR violation").
Reads the model policy and the embedding model, compares them with OpenRouter's public list of ZDR endpoints
(fetched with curl, no key needed), and prints one line per model that would be refused. Prints nothing when all
are fine or when the list cannot be fetched. Exit 0 always: this is a warning, not a gate.

  zdr_check.py POLICY [--embedding MODEL] [--list FILE]
"""
import argparse
import json
import subprocess
import sys

URL = "https://openrouter.ai/api/v1/endpoints/zdr"


def models_in(policy: dict, embedding: str | None) -> list[str]:
    out = []
    for c in (policy.get("classes") or {}).values():
        for ref in [c.get("model"), *(c.get("fallbacks") or [])]:
            if isinstance(ref, str) and ref.startswith("openrouter/"):
                out.append(ref[len("openrouter/"):])
    if embedding:
        out.append(embedding)
    return list(dict.fromkeys(out))


def missing(models: list[str], zdr_rows: list[dict]) -> list[str]:
    ok = {r.get("model_id") for r in zdr_rows if isinstance(r, dict)}
    return [m for m in models if m not in ok]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("policy")
    ap.add_argument("--embedding")
    ap.add_argument("--list", help="a saved copy of the ZDR list (tests)")
    ns = ap.parse_args(argv)
    try:
        policy = json.load(open(ns.policy))
        if ns.list:
            data = json.load(open(ns.list))
        else:
            r = subprocess.run(["curl", "-s", "-m", "20", URL], capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return 0
    for m in missing(models_in(policy, ns.embedding), rows):
        print(f"{m}: no zero-data-retention provider on OpenRouter; an account that requires it will refuse this model")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
