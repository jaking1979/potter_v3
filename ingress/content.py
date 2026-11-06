"""Content handling: size limits, quote/forward splitting, injection indicators.

Nothing here can raise trust. Splitting only ever moves text from the sender's
own kind to a *less* trusted kind (forwarded / quoted). Injection indicators
are flags for review and tracing; provenance never depends on them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .events import FORWARDED_CONTENT, PRINCIPAL_DIRECT, QUOTED_CONTENT, Segment


@dataclass(frozen=True)
class IngressLimits:
    max_raw_bytes: int = 2 * 1024 * 1024        # whole email / webhook / update
    max_text_bytes: int = 64 * 1024             # all text segments combined
    max_attachments: int = 10
    max_attachment_bytes: int = 1024 * 1024     # each attachment
    max_message_id_len: int = 256


_FORWARD_MARKERS = re.compile(
    r"^\s*(-{2,}\s*forwarded message\s*-{2,}|begin forwarded message:|"
    r"-{2,}\s*original message\s*-{2,})\s*$", re.IGNORECASE)
_REPLY_HEADER = re.compile(r"^\s*on .{1,200} wrote:\s*$", re.IGNORECASE)
_FWD_SUBJECT = re.compile(r"^\s*(fwd?|fw)\s*:", re.IGNORECASE)


def is_forward_subject(subject: str | None) -> bool:
    return bool(subject and _FWD_SUBJECT.match(subject))


def split_body(text: str, own_kind: str, *, forwarded: bool = False) -> tuple[Segment, ...]:
    """Split an email body into the sender's own text and quoted/forwarded runs.
    Everything after a forward marker is forwarded; a reply header and ``>``
    lines are quoted. ``forwarded=True`` marks the whole body forwarded."""
    if forwarded:
        return (Segment(FORWARDED_CONTENT, text),) if text else ()
    out: list[tuple[str, list[str]]] = []

    def push(kind: str, line: str) -> None:
        if out and out[-1][0] == kind:
            out[-1][1].append(line)
        else:
            out.append((kind, [line]))

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _FORWARD_MARKERS.match(line):
            push(FORWARDED_CONTENT, "\n".join(lines[i:]))
            break
        if _REPLY_HEADER.match(line) or line.lstrip().startswith(">"):
            push(QUOTED_CONTENT, line)
        else:
            push(own_kind, line)
    segs = []
    for kind, ls in out:
        body = "\n".join(ls)
        if body.strip():
            segs.append(Segment(kind, body))
    return tuple(segs)


# Indicators of content trying to act on policy. Flags only.
_FLAG_PATTERNS = {
    "claims_approval": re.compile(
        r"\b(approved|approval|pre-?approved|authori[sz]ed|/approve|issuer_signature|"
        r"decided_by)\b", re.IGNORECASE),
    "addresses_policy": re.compile(
        r"\b(principal_direct|system_policy|provenance|policy\s*engine|policyrequest|"
        r"trusted[_ ]source|c1)\b", re.IGNORECASE),
    "instruction_like": re.compile(
        r"\b(ignore (all |any )?(previous|prior) instructions|you are now|system prompt|"
        r"as the assistant|disregard)\b", re.IGNORECASE),
    "payment_request": re.compile(
        r"(\b(wire|pay|payment|transfer|remit|send)\b.{0,40}(\$|usd|\d{2,}|funds|money))",
        re.IGNORECASE | re.DOTALL),
}


def injection_flags(segments: tuple[Segment, ...], untrusted_only: bool = True) -> frozenset[str]:
    hits = set()
    for seg in segments:
        if untrusted_only and seg.provenance == PRINCIPAL_DIRECT:
            continue
        for name, pat in _FLAG_PATTERNS.items():
            if pat.search(seg.text):
                hits.add(name)
    return frozenset(hits)
