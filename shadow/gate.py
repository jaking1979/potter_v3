"""No-effect tool gate for the shadow install.

Every tool the shadow runtime can use is registered here with a declared
effect class. Only three classes exist, so a tool that sends, buys, trades or
writes cannot be registered at all:

* ``read``        - reads state and changes nothing;
* ``reply_owner`` - one message to the owner's own chat, nothing else;
* ``model``       - a model completion.

The gate enforces the declaration, the allowlist, the owner binding and the
audit-before-call rule. It cannot prove that a callable registered as ``read``
really is read-only; that is the wiring module's and the auditor's job, and
``tools()`` lists exactly what was registered so it can be checked.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

READ = "read"
REPLY_OWNER = "reply_owner"
MODEL = "model"
EFFECT_CLASSES = frozenset({READ, REPLY_OWNER, MODEL})


class EffectRefused(Exception):
    """The call was not made. ``reason`` is a closed code, never caller text."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolInfo:
    name: str
    effect_class: str


def _valid_name(name) -> bool:
    return (type(name) is str and 0 < len(name) <= 64
            and all(c.isascii() and (c.isalnum() or c in "._-") for c in name))


class ToolGate:
    def __init__(self, *, owner_chat_id: str, audit: Callable[[dict], None]):
        if type(owner_chat_id) is not str or not owner_chat_id:
            raise ValueError("owner_chat_id must be a non-empty string")
        self._owner = owner_chat_id
        self._audit = audit
        self._tools: dict[str, tuple[str, Callable]] = {}
        self._sealed = False

    def register(self, name: str, effect_class: str, fn: Callable) -> None:
        if self._sealed:
            raise EffectRefused("gate_sealed")
        if not _valid_name(name):
            raise EffectRefused("invalid_tool_name")
        if type(effect_class) is not str or effect_class not in EFFECT_CLASSES:
            raise EffectRefused("effect_class_not_allowed")
        if name in self._tools:
            raise EffectRefused("duplicate_tool")
        if not callable(fn):
            raise EffectRefused("not_callable")
        self._tools[name] = (effect_class, fn)

    def seal(self) -> None:
        """No tool can be added after wiring finishes."""
        self._sealed = True

    def tools(self) -> tuple[ToolInfo, ...]:
        return tuple(ToolInfo(n, c) for n, (c, _) in sorted(self._tools.items()))

    def call(self, name: str, /, **kwargs):
        entry = self._tools.get(name) if type(name) is str else None
        if entry is None:
            self._record(name if _valid_name(name) else "?", "?", "refused:unknown_tool")
            raise EffectRefused("unknown_tool")
        effect_class, fn = entry
        if not self._sealed:
            self._record(name, effect_class, "refused:gate_not_sealed")
            raise EffectRefused("gate_not_sealed")
        if effect_class == REPLY_OWNER:
            chat_id = kwargs.get("chat_id")
            if type(chat_id) is not str or chat_id != self._owner:
                self._record(name, effect_class, "refused:not_owner_chat")
                raise EffectRefused("not_owner_chat")
        # Audit first. If the audit sink is down the call is not made.
        try:
            self._audit({"tool": name, "effect_class": effect_class, "outcome": "attempt"})
        except Exception as exc:
            raise EffectRefused("audit_unavailable") from exc
        return fn(**kwargs)

    def _record(self, name, effect_class, outcome) -> None:
        try:
            self._audit({"tool": name, "effect_class": effect_class, "outcome": outcome})
        except Exception:
            pass  # the call is refused either way
