"""Structural check for T19 item 1: no job sends directly.

Outside ``notifier/`` (the one notifier) and ``runtime/`` (C3's own home),
Python code may not:

* import channel adapters (``notifier.channels`` or names from it);
* import or construct the C3 outbox (``Outbox``, ``OutboxMessage``,
  ``runtime.outbox``) or call ``.relay(`` - that is the send path;
* import the ``Notifier`` service itself (jobs get a ``JobHandle``; only the
  future C8 wiring module, passed in ``allow``, may build one);
* import a network or mail client (``smtplib``, ``urllib.request``,
  ``http.client``, ``requests``, ``httpx``, ``aiohttp``, ``telegram``,
  ``telebot``, ``socket``). These are refused everywhere, ``notifier/``
  included, since E1 is offline/staging-only (T22).

``python3 -m notifier.lint [root]`` exits non-zero on any finding.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CHANNEL_NAMES = frozenset({"Channel", "FakeChannel", "TELEGRAM", "EMAIL", "POTTER_HOME"})
OUTBOX_NAMES = frozenset({"Outbox", "OutboxMessage", "SendFailed"})
NETWORK = frozenset({"smtplib", "urllib.request", "http.client", "requests", "httpx", "aiohttp",
                     "telegram", "telebot", "socket"})
EXEMPT_DIRS = ("notifier", "runtime", "tests", ".git")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def _mod_hits_network(mod: str) -> bool:
    return any(mod == n or mod.startswith(n + ".") for n in NETWORK)


def scan_source(src: str, path: str, *, exempt: bool) -> list[Finding]:
    out: list[Finding] = []
    tree = ast.parse(src, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _mod_hits_network(a.name):
                    out.append(Finding(path, node.lineno, f"network client import: {a.name}"))
                elif not exempt and a.name in ("notifier.channels", "runtime.outbox",
                                               "notifier.service"):
                    out.append(Finding(path, node.lineno, f"direct channel path: {a.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = {a.name for a in node.names}
            if _mod_hits_network(mod):
                out.append(Finding(path, node.lineno, f"network client import: {mod}"))
            if exempt:
                continue
            if mod in ("notifier.channels", "runtime.outbox", "notifier.service") or (
                    mod == "notifier" and names & ({"channels", "service", "Notifier"}
                                                   | CHANNEL_NAMES)) or (
                    mod == "runtime" and names & (OUTBOX_NAMES | {"outbox"})):
                out.append(Finding(path, node.lineno, f"direct channel path: from {mod}"))
        elif not exempt and isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "relay":
                out.append(Finding(path, node.lineno, "direct send: .relay()"))
            if isinstance(f, ast.Name) and f.id in OUTBOX_NAMES | {"FakeChannel", "Notifier"}:
                out.append(Finding(path, node.lineno, f"direct channel path: {f.id}()"))
    return out


def scan(root: str | Path, *, allow: Iterable[str] = ()) -> list[Finding]:
    root = Path(root)
    allowed = {str(Path(a)) for a in allow}
    findings: list[Finding] = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in (".git",):
            continue
        exempt = (rel.parts[0] in EXEMPT_DIRS) or str(rel) in allowed
        findings += scan_source(p.read_text(encoding="utf-8"), str(rel), exempt=exempt)
    return findings


def main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else "."
    found = scan(root)
    for f in found:
        print(f"{f.path}:{f.line}: {f.rule}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
