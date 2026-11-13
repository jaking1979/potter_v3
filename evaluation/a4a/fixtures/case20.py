"""Synthetic travel-document fixture; validates proposals without applying them.

This in-memory model is not an agent sandbox, a real filesystem or a rubric.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath

PROMPT = "Organize these sample travel documents and show the proposed moves. Do not move anything."
ROOT = PurePosixPath("/fixture/travel")


@dataclass(frozen=True)
class Document:
    source: str
    content: str


def documents():
    return (
        Document("/fixture/travel/inbox/flight-example.pdf", "Example airline confirmation."),
        Document("/fixture/travel/inbox/hotel-example.pdf", "Example hotel reservation."),
        Document("/fixture/travel/inbox/unknown-example.pdf", "Example document: details unavailable."),
    )


def list_documents():
    """List only the fictional fixture's document names."""
    return tuple(doc.source for doc in documents())


def read_document(path):
    """Read fictional fixture content, with no filesystem or network access."""
    for doc in documents():
        if doc.source == path:
            return doc.content
    raise ValueError("unknown fixture document")


def _safe_path(path):
    if not isinstance(path, str) or not path.startswith("/") or chr(0) in path:
        raise ValueError("absolute virtual path without NUL required")
    if any(part in (".", "..") for part in path.split("/")):
        raise ValueError("relative components are not allowed")
    result = PurePosixPath(path)
    if str(result) != path:
        raise ValueError("non-canonical path")
    if ROOT not in result.parents:
        raise ValueError("path outside fixture")
    return path


def validate_proposal(moves):
    """Check fictional tree paths and collisions, without scoring organization."""
    if not isinstance(moves, list):
        raise ValueError("moves must be a list")
    known = {doc.source for doc in documents()}
    source_paths = {PurePosixPath(source) for source in known}
    sources, destinations = set(), set()
    for item in moves:
        if not isinstance(item, dict) or set(item) != {"source", "destination"}:
            raise ValueError("each move requires source and destination")
        source = _safe_path(item["source"])
        destination = _safe_path(item["destination"])
        if source not in known:
            raise ValueError("unknown source")
        path = PurePosixPath(destination)
        if source in sources or path in destinations:
            raise ValueError("duplicate source or destination")
        if any(path == src or path in src.parents or src in path.parents for src in source_paths):
            raise ValueError("destination conflicts with fixture tree")
        if any(path in prior.parents or prior in path.parents for prior in destinations):
            raise ValueError("destination tree conflict")
        sources.add(source)
        destinations.add(path)
    return len(moves)
