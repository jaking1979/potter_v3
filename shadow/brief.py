"""Shadow weekday Morning Brief.

A schedule tick that, once per weekday after the send time, runs one turn
through the same ShadowRuntime path as a chat message (policy -> governor ->
broker -> reply), so the brief is policy-traced, cost-capped and reaches only
the owner's chat. The prompt is fixed text plus the date: no external content
enters it in v0. Sources (home status, inbox) arrive in later packages and must
add their own provenance kinds so the policy engine sees the taint.

State is one small JSON file (date -> attempts/done) so a restart neither
repeats a delivered brief nor retries forever.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from shadow.turn import Inbound, Outcome, ShadowRuntime

_NS = uuid.UUID("6f1c2f0e-5a0b-4a39-9a55-7d1f3a2b9c01")
MAX_ATTEMPTS = 3
PROVENANCE = ("system_policy",)

PROMPT = ("Write the shadow Morning Brief for {day}. Greet Josh in one line, then say in two or "
          "three short lines what you can and cannot see yet in this shadow install "
          "(no calendar, inbox or home data unless provided). End with one question for him.")


class MorningBrief:
    def __init__(self, *, runtime: ShadowRuntime, owner_chat_id: str, state_path: str,
                 send_hour: int = 7, prompt: str = PROMPT):
        if not (isinstance(send_hour, int) and 0 <= send_hour <= 23):
            raise ValueError("send_hour must be 0-23")
        self._rt, self._owner, self._path = runtime, owner_chat_id, state_path
        self._hour, self._prompt = send_hour, prompt

    def tick(self, now: datetime) -> Outcome | None:
        """Call periodically with the current local time. None means nothing was due."""
        if now.weekday() >= 5 or now.hour < self._hour:
            return None
        day = now.date().isoformat()
        state = self._load()
        entry = state.get(day, {"attempts": 0, "done": False})
        if entry["done"] or entry["attempts"] >= MAX_ATTEMPTS:
            return None
        entry["attempts"] += 1
        state[day] = entry
        self._save(state)                       # count the attempt before sending
        msg = Inbound(self._owner, self._prompt.format(day=day), event_id=f"brief:{day}",
                      provenance=PROVENANCE,
                      task_id=str(uuid.uuid5(_NS, f"{day}:{entry['attempts']}")))
        outcome = self._rt.handle(msg)
        if outcome.status in ("replied", "denied", "refused"):
            entry["done"] = True                # delivered, or deliberately not sent: no retry
            self._save(state)
        return outcome

    def _load(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        keep = dict(sorted(state.items())[-14:])
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
