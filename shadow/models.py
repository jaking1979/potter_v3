"""Model calls for the shadow runtime: reserve, dispatch, call, settle, log.

Budget accounting is D1's ``Broker``. A call whose cost is unknown (provider
error, or a result with no usable cost) is never settled, so its full
worst-case reservation stays counted against every ceiling.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable

from model_broker.broker import Broker, Candidate, Caps, Refused, Request

# T13/T28 ratified defaults, in microdollars: $0.50/request, $2/task,
# $10/day, $150/month.
RATIFIED_CAPS = Caps(request=500_000, task=2_000_000, day=10_000_000, month=150_000_000)


class ModelRefused(Exception):
    """No provider call was made. ``reason`` is the broker's closed code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ModelUnavailable(Exception):
    """The provider call was attempted and its outcome and cost are unknown."""


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    provider: str
    call_id: str
    input_tokens: int | None
    output_tokens: int | None
    actual_usd_micros: int | None
    state: str  # settled | overrun | unknown


def _count(value):
    return value if type(value) is int and value >= 0 else None


class BrokeredModel:
    """``gate`` must hold a ``model``-class tool named ``tool`` taking
    ``model``, ``provider`` and ``messages`` and returning a dict with
    ``text`` and optionally ``input_tokens``, ``output_tokens``,
    ``cost_usd_micros``."""

    def __init__(self, *, broker: Broker, candidates, gate, costlog,
                 today: Callable[[], str], now: Callable[[], str],
                 tool: str = "model.complete"):
        self._broker = broker
        self._candidates = tuple(candidates)
        self._gate = gate
        self._costlog = costlog
        self._today = today
        self._now = now
        self._tool = tool

    def complete(self, *, task_id: str, task_class: str, privacy_class: str,
                 messages) -> ModelResult:
        call_id = str(uuid.uuid4())
        day = self._today()
        req = Request(call_id=call_id, task_id=task_id, task_class=task_class,
                      privacy_class=privacy_class, day=day,
                      month=day[:7] if type(day) is str else "")
        try:
            chosen: Candidate = self._broker.reserve(req, list(self._candidates))
        except Refused as exc:
            self._log(call_id, task_id, "none", "none", 0, None, None, None, "refused")
            raise ModelRefused(str(exc)) from exc
        worst = chosen.worst_usd_micros
        try:
            self._broker.dispatch_once(call_id, dispatch_day=self._today())
        except Refused as exc:
            # Reservation stays counted; nothing was sent.
            self._log(call_id, task_id, chosen.model, chosen.provider, worst,
                      None, None, None, "refused")
            raise ModelRefused(str(exc)) from exc
        try:
            raw = self._gate.call(self._tool, model=chosen.model,
                                  provider=chosen.provider, messages=messages)
            text = raw["text"]
            if type(text) is not str:
                raise TypeError("provider text is not a string")
        except Exception as exc:
            self._log(call_id, task_id, chosen.model, chosen.provider, worst,
                      None, None, None, "unknown")
            raise ModelUnavailable("provider_call_failed") from exc
        tokens_in = _count(raw.get("input_tokens"))
        tokens_out = _count(raw.get("output_tokens"))
        actual = _count(raw.get("cost_usd_micros"))
        if actual is None:
            state = "unknown"  # keep the worst case reserved
        else:
            try:
                self._broker.settle(call_id, actual)
                state = "settled"
            except Refused as exc:
                if str(exc) != "cost_variance":
                    raise
                state = "overrun"  # broker now refuses all further calls
        self._log(call_id, task_id, chosen.model, chosen.provider, worst,
                  tokens_in, tokens_out, actual, state)
        return ModelResult(text=text, model=chosen.model, provider=chosen.provider,
                           call_id=call_id, input_tokens=tokens_in,
                           output_tokens=tokens_out, actual_usd_micros=actual, state=state)

    def _log(self, call_id, task_id, model, provider, reserved, tin, tout, actual, state):
        self._costlog.append({
            "ts": self._now(), "call_id": call_id, "task_id": task_id,
            "model": model, "provider": provider, "state": state,
            "reserved_usd_micros": reserved, "input_tokens": tin,
            "output_tokens": tout, "actual_usd_micros": actual})
