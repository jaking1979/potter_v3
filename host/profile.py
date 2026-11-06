"""Assumed-host profile: the single place the build records what it believes
about the Catskills host while the host is unavailable (Josh directive
2026-11-06). Every fact is `reported` (from a dated source) or `assumed`.

Nothing here is evidence. A1/T5/T39 gates still require real-host checks;
`diff()` lists where the real host disagrees so fixes land in adapters.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROFILE_PATH = Path(__file__).with_name("assumed_host.json")
STATUSES = ("reported", "assumed", "verified")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Fact:
    name: str
    value: Any
    status: str
    source: str


def load(path: Path = PROFILE_PATH) -> dict[str, Fact]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"profile unreadable: {type(exc).__name__}") from exc
    if raw.get("schema") != "potter.assumed-host/v0" or not isinstance(raw.get("facts"), dict):
        raise ProfileError("unexpected profile schema")
    facts = {}
    for name, f in raw["facts"].items():
        if (not isinstance(f, dict) or set(f) != {"value", "status", "source"}
                or f["status"] not in STATUSES or not isinstance(f["source"], str) or not f["source"]):
            raise ProfileError(f"malformed fact: {name}")
        facts[name] = Fact(name, f["value"], f["status"], f["source"])
    return facts


def assumptions(facts: Mapping[str, Fact]) -> list[str]:
    """Names still resting on guesses; each must be checked before any gate."""
    return sorted(n for n, f in facts.items() if f.status == "assumed")


def diff(facts: Mapping[str, Fact], observed: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Facts whose observed value differs from the profile, as {name: (expected, observed)}.

    Facts absent from `observed` are not reported here; `unobserved()` lists them.
    """
    return {n: (facts[n].value, observed[n]) for n in facts
            if n in observed and observed[n] != facts[n].value}


def unobserved(facts: Mapping[str, Fact], observed: Mapping[str, Any]) -> list[str]:
    return sorted(n for n in facts if n not in observed)
