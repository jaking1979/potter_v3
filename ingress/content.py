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


# Boundary detection is a heuristic and cannot establish authorship, so it is
# used only to fail closed: the sender's own kind covers ONLY the text before
# the first sign of quoted, forwarded or pasted-header material. Everything
# from that line to the end of the body is untrusted, including any later
# unprefixed lines (interleaved replies, wrapped quotes, continuations).
_RE = re.IGNORECASE
_FORWARD_MARKERS = re.compile(
    r"^\s*(-{2,}\s*(forwarded|original)\s+message\s*-{2,}|begin forwarded message\s*:?|"
    r"-{2,}\s*(weitergeleitete nachricht|message transf[eé]r[eé]|mensaje reenviado)\s*-{2,})"
    r"\s*$", _RE)
_SEPARATOR = re.compile(r"^\s*([-_=*]\s*){5,}$")          # bare rule lines (Outlook etc.)
_QUOTE_VERB = r"(wrote|writes|said|a [eé]crit|schrieb|escribi[oó]|ha scritto|schreef|skrev)"
_REPLY_TAIL = re.compile(r"\b" + _QUOTE_VERB + r"\b[^\n]{0,80}:\s*$", _RE)
_REPLY_START = re.compile(r"^\s*(on|le|am|el|il|op|den)\s+\S", _RE)
_HEADER_FROM = re.compile(r"^\s*\*?(from|von|de|da|van|fra)\s*:\*?\s*\S", _RE)
_HEADER_NEXT = re.compile(
    r"^\s*\*?(sent|date|to|cc|subject|envoy[eé]|gesendet|datum|an|betreff|objet|enviado|"
    r"asunto|para|à)\s*:", _RE)
_REPLY_HEADER_SPAN = 4      # a wrapped "On ... wrote:" header may span this many lines
_HEADER_BLOCK_SPAN = 6
_FWD_SUBJECT = re.compile(r"^\s*(fwd?|fw|wg|tr|rv|enc)\s*:", _RE)


def is_forward_subject(subject: str | None) -> bool:
    return bool(subject and _FWD_SUBJECT.match(subject))


def first_boundary(lines: list[str]) -> tuple[int, str | None]:
    """Index of the first line that starts quoted/forwarded material, and its
    kind; ``(len(lines), None)`` when no boundary is seen."""
    for i, line in enumerate(lines):
        if _FORWARD_MARKERS.match(line):
            return i, FORWARDED_CONTENT
        if _HEADER_FROM.match(line) and any(
                _HEADER_NEXT.match(nxt) for nxt in lines[i + 1:i + 1 + _HEADER_BLOCK_SPAN]):
            return i, FORWARDED_CONTENT          # pasted / forwarded header block
        if line.lstrip().startswith(">") or _SEPARATOR.match(line) or _REPLY_TAIL.search(line):
            return i, QUOTED_CONTENT
        if _REPLY_START.match(line):
            for j in range(2, _REPLY_HEADER_SPAN + 1):
                if _REPLY_TAIL.search(" ".join(x.strip() for x in lines[i:i + j])):
                    return i, QUOTED_CONTENT     # wrapped reply header
    return len(lines), None


def split_body(text: str, own_kind: str, *, forwarded: bool = False) -> tuple[Segment, ...]:
    """The sender's own text is the prefix before the first boundary; the rest
    of the body is one untrusted run (fail closed). ``forwarded=True`` marks
    the whole body forwarded."""
    if forwarded:
        return (Segment(FORWARDED_CONTENT, text),) if text.strip() else ()
    lines = text.splitlines()
    idx, kind = first_boundary(lines)
    segs = []
    own = "\n".join(lines[:idx])
    if own.strip():
        segs.append(Segment(own_kind, own))
    rest = "\n".join(lines[idx:])
    if kind is not None and rest.strip():
        segs.append(Segment(kind, rest))
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
