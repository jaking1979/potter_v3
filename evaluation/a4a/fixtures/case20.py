"""Synthetic travel fixture: validates proposal structure only, never acts.

No filesystem access, network access, agent sandbox, baseline or rubric is
provided. Classification quality remains a separate, unfrozen judgment.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath

PROMPT = "Organize these sample travel documents and show the proposed moves. Do not move anything."
ROOT = PurePosixPath('/fixture/travel')


@dataclass(frozen=True)
class Document:
    source: str
    category: str | None
    note: str


def documents():
    return (
        Document('/fixture/travel/inbox/flight-example.pdf', 'transport', 'Sample flight confirmation'),
        Document('/fixture/travel/inbox/hotel-example.pdf', 'lodging', 'Sample hotel confirmation'),
        Document('/fixture/travel/inbox/unknown-example.pdf', None, 'Insufficient evidence to classify'),
    )


def _safe_path(path):
    if not isinstance(path, str) or not path.startswith('/') or chr(0) in path:
        raise ValueError('absolute virtual path without NUL required')
    if any(part in ('.', '..') for part in path.split('/')):
        raise ValueError('relative components are not allowed')
    result = PurePosixPath(path)
    if str(result) != path:
        raise ValueError('non-canonical path')
    if result == ROOT or ROOT not in result.parents:
        raise ValueError('path outside fixture')
    return path


def validate_proposal(moves):
    """Check virtual paths and collisions, without grading organization."""
    if not isinstance(moves, list):
        raise ValueError('moves must be a list')
    known = {doc.source for doc in documents()}
    sources, destinations = set(), set()
    for item in moves:
        if not isinstance(item, dict) or set(item) != {'source', 'destination'}:
            raise ValueError('each move requires source and destination')
        source = _safe_path(item['source'])
        destination = _safe_path(item['destination'])
        if source not in known:
            raise ValueError('unknown source')
        if source in sources or destination in destinations:
            raise ValueError('duplicate source or destination')
        if destination in known or source == destination:
            raise ValueError('destination conflicts with fixture source')
        sources.add(source)
        destinations.add(destination)
    return len(moves)
