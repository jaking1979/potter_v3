"""Context Governor v0: deterministic, offline, no model call.

Decides what the model sees for a chat turn: a short fixed persona plus the
last few completed exchanges from THIS session, inside a hard character budget
(oldest exchanges drop first, each exchange is truncated). It returns the plan
the turn loop records for ``/why``: labels and counts only, never content.

What it deliberately does not do: retrieve notes, read Notion, Gmail or Home
Assistant, persist anything across a restart, or classify task types with a
model. Untrusted content (an email body, a web page) never enters this
context; later read-only slices must carry such content as labelled data under
the same budget. The Jev layer or a small-model classifier can replace this
class behind the same ``plan``/``observe`` interface.
"""
from __future__ import annotations

import threading
from collections import deque

from shadow.turn import ContextPlan

PERSONA = ("You are Potter, Josh King's personal assistant, running as a shadow install. "
           "You can only chat: you cannot send email, buy, trade, change settings or write "
           "anywhere. If asked to do any of those, say you cannot do that here. Be brief and "
           "plain. Never state anything about the user's accounts or finances unless the user "
           "told you in this conversation.")
MAX_PERSONA_CHARS = 1_000


class ContextGovernorV0:
    def __init__(self, *, persona: str = PERSONA, max_exchanges: int = 4,
                 per_message_chars: int = 400, max_context_chars: int = 2_400):
        if type(persona) is not str or not persona.strip() or len(persona) > MAX_PERSONA_CHARS:
            raise ValueError("persona must be a non-empty string within the bound")
        for value in (max_exchanges, per_message_chars, max_context_chars):
            if type(value) is not int or value < 0:
                raise ValueError("limits must be non-negative integers")
        if max_context_chars < len(persona):
            raise ValueError("the context budget must at least hold the persona")
        self._persona = persona
        self._per = per_message_chars
        self._budget = max_context_chars
        self._history: deque[tuple[str, str]] = deque(maxlen=max_exchanges or 1)
        self._keep = max_exchanges
        self._lock = threading.Lock()

    def observe(self, user_text: str, reply_text: str) -> None:
        if type(user_text) is not str or type(reply_text) is not str:
            return
        if not user_text.strip() or not reply_text.strip() or self._keep == 0:
            return
        with self._lock:
            self._history.append((user_text, reply_text))

    def _clip(self, text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= self._per else text[: max(self._per - 1, 0)] + "…"

    def plan(self, text: str) -> ContextPlan:
        with self._lock:
            exchanges = list(self._history)[-self._keep:] if self._keep else []
        blocks = [f"User: {self._clip(u)}\nPotter: {self._clip(r)}" for u, r in exchanges]
        header = "Earlier in this conversation (oldest first):"
        used = len(self._persona)
        kept: list[str] = []
        for block in reversed(blocks):                     # newest first, so the oldest drop first
            cost = len(block) + 2 + (0 if kept else len(header) + 2)
            if used + cost > self._budget:
                break
            kept.append(block)
            used += cost
        kept.reverse()
        context = (self._persona,) + ((header + "\n\n" + "\n\n".join(kept),) if kept else ())
        sources = ("persona:v0", f"recent_exchanges:{len(kept)}")
        reason = f"v0 deterministic: persona plus the last {len(kept)} exchange(s) within {self._budget} characters"
        return ContextPlan("chat", "personal", context, sources, reason)
