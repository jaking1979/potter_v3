"""Guard-in-path harness: the guard sits between the agent and the brokerage tools.

`GuardedBroker.call` strips the MCP server prefix, runs the guard, writes one
audit line per decision, and forwards only allowed calls to the backend. The
kill switch is a file flag: engaging it refuses every new order and cancels
open orders through the same backend.

`StubBroker` is the backend used by the negative test. It records calls and
has no network code and no credentials, so a test can never reach a real
account.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from containment import order_guard
from containment.mandate import Mandate, MandateError, load_mandate
from containment.order_guard import AccountState, Decision

SERVER_PREFIX = "robinhood-trading__"


class Backend(Protocol):
    def invoke(self, tool: str, args: object) -> object: ...
    def open_order_ids(self) -> list[str]: ...


class StubBroker:
    """Records calls; never contacts anything."""

    def __init__(self, open_orders: list[str] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._open = list(open_orders or [])

    def invoke(self, tool: str, args: object) -> object:
        self.calls.append((tool, args))
        if tool == "cancel_equity_order" and isinstance(args, dict):
            self._open = [o for o in self._open if o != args.get("order_id")]
        return {"stub": True, "tool": tool}

    def open_order_ids(self) -> list[str]:
        return list(self._open)


class GuardedBroker:
    def __init__(
        self,
        backend: Backend,
        mandate_path: Path,
        kill_switch_path: Path,
        audit_path: Path,
        account_state: Callable[[], AccountState | None],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._backend = backend
        self._mandate_path = Path(mandate_path)
        self._kill_path = Path(kill_switch_path)
        self._audit_path = Path(audit_path)
        self._account_state = account_state
        self._clock = clock

    def _mandate(self) -> Mandate | None:
        # Re-read on every call: an edited or deleted mandate takes effect at once.
        try:
            return load_mandate(self._mandate_path)
        except MandateError:
            return None

    def kill_switch_engaged(self) -> bool:
        return self._kill_path.exists()

    def _audit(self, decision: Decision, args: object) -> None:
        line = {"at": self._clock().isoformat(), **asdict(decision), "args": args}
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")

    def call(self, tool: str, args: object) -> object:
        name = tool[len(SERVER_PREFIX):] if tool.startswith(SERVER_PREFIX) else tool
        try:
            account = self._account_state()
        except Exception:  # account lookup failure must refuse, not crash open
            account = None
        decision = order_guard.check(
            name, args, self._mandate(), account, self.kill_switch_engaged()
        )
        self._audit(decision, args)
        if not decision.allow:
            raise PermissionError(f"BLOCK {name}: {decision.reason}")
        return self._backend.invoke(name, args)

    def engage_kill_switch(self, reason: str) -> list[str]:
        """Refuse all new orders, then cancel every open order. Returns cancelled ids."""
        self._kill_path.write_text(
            json.dumps({"engaged_at": self._clock().isoformat(), "reason": reason}),
            encoding="utf-8",
        )
        cancelled = []
        for order_id in self._backend.open_order_ids():
            self.call("cancel_equity_order", {"order_id": order_id})
            cancelled.append(order_id)
        return cancelled
