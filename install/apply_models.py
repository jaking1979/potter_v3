#!/usr/bin/env python3
"""Turn Potter's model policy into OpenClaw settings, so the models are set from one file and never retyped.

  apply_models.py POLICY --patch OUT.json5 [--agents potter,merrick]   write the config patch
  apply_models.py POLICY --job-args                                   print --model/--fallbacks for agent jobs

Refuses a policy with placeholders, and refuses open-ended outside routing (openrouter/auto): the ratified rule
is that Potter picks from validated candidates (T28).
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

AGENT_CLASS = {"potter": "chat", "merrick": "merrick"}
REQUIRED = ("chat", "job_turn", "merrick")
OPEN_ROUTERS = ("openrouter/auto", "openrouter/openrouter/auto")


def load(path: str) -> dict:
    p = json.loads(Path(path).read_text(encoding="utf-8"))
    classes = p.get("classes") if isinstance(p, dict) else None
    if not isinstance(classes, dict):
        raise ValueError("policy has no classes")
    for name in REQUIRED:
        c = classes.get(name)
        if not isinstance(c, dict) or not isinstance(c.get("model"), str) or not isinstance(c.get("fallbacks"), list):
            raise ValueError(f"policy class '{name}' is missing or incomplete")
    for name, c in classes.items():
        for ref in [c.get("model"), *(c.get("fallbacks") or [])]:
            if not isinstance(ref, str) or not ref or ref.startswith("REPLACE_"):
                raise ValueError(f"policy class '{name}' still has a placeholder")
            if ref.split(":")[0].rstrip("/").startswith(OPEN_ROUTERS) and ref.split("/")[-1].startswith("auto"):
                raise ValueError(f"policy class '{name}' uses open-ended routing ({ref}); pick a validated model")
    return classes


def setting(c: dict) -> dict:
    return {"primary": c["model"], "fallbacks": list(c["fallbacks"])}


def patch(classes: dict, agents: list[str]) -> dict:
    refs = []
    for c in classes.values():
        for r in [c["model"], *c["fallbacks"]]:
            if "/" in r and not r.startswith("~") and r not in refs:
                refs.append(r)
    entries = {a: {"model": setting(classes[AGENT_CLASS[a]])} for a in agents if a in AGENT_CLASS}
    return {"agents": {"defaults": {"model": setting(classes["chat"]), "models": {r: {} for r in refs}},
                       **({"entries": entries} if entries else {})}}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("policy")
    ap.add_argument("--patch")
    ap.add_argument("--agents", default="potter")
    ap.add_argument("--job-args", action="store_true")
    ns = ap.parse_args(argv)
    try:
        classes = load(ns.policy)
    except (OSError, ValueError) as e:
        print(f"apply_models: {e}", file=sys.stderr)
        return 2
    if ns.patch:
        Path(ns.patch).write_text(json.dumps(patch(classes, [a for a in ns.agents.split(",") if a]), indent=1))
    if ns.job_args:
        c = classes["job_turn"]
        args = ["--model", c["model"]] + (["--fallbacks", ",".join(c["fallbacks"])] if c["fallbacks"] else [])
        print(" ".join(shlex.quote(a) for a in args))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
