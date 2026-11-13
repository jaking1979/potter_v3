"""Synthetic case 20: propose virtual file moves; no disk or network access.

This is fixture preparation, not an agent sandbox, baseline, or frozen rubric.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath

PROMPT = "Organize these sample travel documents and show the proposed moves. Do not move anything."
ROOT = PurePosixPath("/fixture/travel")


@dataclass(frozen=True)
class Document:
    source: str
    category: str | None
    note: str


def documents():
    return (
        Document("/fixture/travel/inbox/flight-example.pdf", "transport", "Sample flight confirmation"),
        Document("/fixture/travel/inbox/hotel-example.pdf", "lodging", "Sample hotel confirmation"),
        Document("/fixture/travel/inbox/unknown-example.pdf", None, "Insufficient evidence to classify"),
    )


def _safe_path(path):
    if not isinstance(path, str) or not path.startswith("/") or "\\x00" in path:
        raise ValueError("absolute virtual path required")
    raw = path.split("/")
    if any(part in (".", "..") for part in raw):
        raise ValueError("relative components are not allowed")
    result = PurePosixPath(path)
    if result == ROOT or ROOT not in result.parents:
        raise ValueError("path outside fixture")
    return str(result)


def validate_proposal(moves):
    """Check structural safety of move proposals; do not modify anything.

    The agent must explain classification; validation is not a complete score.
    """
    if not isinstance(moves, list):
        raise ValueError("moves must be a list")
    known = {doc.source: doc for doc in documents()}
    used_sources, used_destinations = set(), set()
    for item in moves:
        if not isinstance(item, dict) or set(item) != {"source", "destination"}:
            raise ValueError("each move requires source and destination")
        source = _safe_path(item["source"])
        dest = _safe_path(item["destination"])
        if source not in known or known[source].category is None:
            raise ValueError("unknown or ambiguous source")
        if source in used_sources or dest in used_destinations:
            raise ValueError("duplicate source or destination")
        if dest in known or source == dest:
            raise ValueError("destination exists")
        category = known[source].category
        if PurePosixPath(dest).parent != ROOT / category:
            raise ValueError("destination category mismatch")
        if PurePosixPath(source).name != PurePosixPath(dest).name:
            raise ValueError("renames are not supported in this fixture")
        used_sources.add(source)
        used_destinations.add(dest)
    return len(moves)
