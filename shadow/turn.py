"""One chat turn of the shadow runtime.

inbound message -> owner check -> policy -> context plan -> model -> reply.

``policy`` and ``governor`` are seams: C1's PolicyEngine and the Context
Governor plug in here. ``/why`` answers from the last turn's record without a
model call, so Josh can see what context was chosen and what the reply cost.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

from shadow.gate import EffectRefused
from shadow.models import ModelRefused, ModelUnavailable


def _new_task_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Inbound:
    chat_id: str
    text: str
    # Set by ingress. event_id is the transport's own id (for example a Telegram
    # update id). task_id is the canonical UUID that policy, trace and the model
    # call all share for this turn; ingress derives it from event_id so a
    # redelivery maps to the same task. provenance is decided by ingress, never
    # by message text.
    event_id: str | None = None
    provenance: tuple = ("principal_direct",)
    task_id: str = field(default_factory=_new_task_id)


@dataclass(frozen=True)
class ContextPlan:
    task_class: str
    privacy_class: str
    context: tuple[str, ...]      # text handed to the model
    sources: tuple[str, ...]      # short labels shown by /why, never content
    reason: str


class Governor(Protocol):
    def plan(self, text: str) -> ContextPlan: ...


class PassthroughGovernor:
    """Placeholder until Context Governor v0 lands: no context, one class."""

    def plan(self, text: str) -> ContextPlan:
        return ContextPlan("chat", "personal", (), (), "no governor configured")


@dataclass(frozen=True)
class Outcome:
    status: str   # replied | ignored | denied | refused | failed
    detail: str = ""


def _usd(micros) -> str:
    return "unknown" if micros is None else f"${micros / 1_000_000:.4f}"


class ShadowRuntime:
    def __init__(self, *, owner_chat_id: str, gate, model, governor: Governor,
                 policy: Callable[[Inbound], bool], reply_tool: str = "telegram.reply",
                 max_text: int = 8000):
        self._owner = owner_chat_id
        self._gate = gate
        self._model = model
        self._governor = governor
        self._policy = policy
        self._reply_tool = reply_tool
        self._max_text = max_text
        self._last: dict | None = None

    def handle(self, msg: Inbound) -> Outcome:
        # Anyone but the owner gets no reply, no model call and no policy call.
        if type(msg.chat_id) is not str or msg.chat_id != self._owner:
            return Outcome("ignored", "not_owner")
        if type(msg.text) is not str or not msg.text.strip() or len(msg.text) > self._max_text:
            return Outcome("ignored", "bad_text")
        try:
            if str(uuid.UUID(msg.task_id)) != msg.task_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return Outcome("ignored", "bad_task_id")
        if msg.text.strip() == "/why":
            return self._reply(self._why())
        try:
            allowed = self._policy(msg) is True   # anything else, or an error, denies
        except Exception:
            allowed = False
        if not allowed:
            return Outcome("denied", "policy")
        try:
            plan = self._governor.plan(msg.text)
            degraded = False
        except Exception:
            # Charter rule: a failed decision layer degrades, it does not block.
            plan = ContextPlan("chat", "personal", (), (), "governor failed; no context used")
            degraded = True
        messages = [{"role": "system", "content": c} for c in plan.context]
        messages.append({"role": "user", "content": msg.text})
        try:
            result = self._model.complete(task_id=msg.task_id, task_class=plan.task_class,
                                          privacy_class=plan.privacy_class, messages=messages)
        except ModelRefused as exc:
            self._last = {"plan": plan, "degraded": degraded, "result": None, "error": exc.reason}
            self._reply(f"I can't answer that right now ({exc.reason}).")
            return Outcome("refused", exc.reason)
        except ModelUnavailable:
            self._last = {"plan": plan, "degraded": degraded, "result": None,
                          "error": "provider_call_failed"}
            self._reply("The model call failed. Its cost is unknown and stays reserved.")
            return Outcome("failed", "provider_call_failed")
        self._last = {"plan": plan, "degraded": degraded, "result": result, "error": None}
        return self._reply(result.text)

    def _why(self) -> str:
        if self._last is None:
            return "No previous turn in this session."
        plan, result = self._last["plan"], self._last["result"]
        lines = [f"Task class: {plan.task_class} ({plan.privacy_class})",
                 f"Why: {plan.reason}",
                 "Context used: " + (", ".join(plan.sources) if plan.sources else "none")]
        if self._last["degraded"]:
            lines.append("Context Governor failed on that turn.")
        if result is None:
            lines.append(f"No reply was produced: {self._last['error']}")
        else:
            lines.append(f"Model: {result.model} via {result.provider}")
            lines.append(f"Tokens: {result.input_tokens} in, {result.output_tokens} out")
            lines.append(f"Cost: {_usd(result.actual_usd_micros)} ({result.state})")
        return "\n".join(lines)

    def _reply(self, text: str) -> Outcome:
        try:
            self._gate.call(self._reply_tool, chat_id=self._owner, text=text)
        except EffectRefused as exc:
            return Outcome("failed", "reply_" + exc.reason)
        except Exception:
            return Outcome("failed", "reply_transport")
        return Outcome("replied")
