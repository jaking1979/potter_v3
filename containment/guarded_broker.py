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
import threading
from dataclasses import asdict, replace
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from containment import order_guard
from containment.mandate import Mandate, MandateError, load_mandate
from containment.order_guard import AccountState, Decision, OpenOrder

SERVER_PREFIX = "robinhood-trading__"


class Backend(Protocol):
    def invoke(self, tool: str, args: object) -> object: ...
    def open_order_ids(self) -> list[str]: ...


class StubBroker:
    """Records calls; never contacts anything. `fail_cancel` ids raise on cancel."""

    def __init__(self, open_orders: list[str] | None = None,
                 fail_cancel: set[str] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._open = list(open_orders or [])
        self._fail_cancel = set(fail_cancel or ())
        self._next_id = 0

    def invoke(self, tool: str, args: object) -> object:
        self.calls.append((tool, args))
        if tool == "cancel_equity_order" and isinstance(args, dict):
            if args.get("order_id") in self._fail_cancel:
                raise RuntimeError("stub cancel failure")
            self._open = [o for o in self._open if o != args.get("order_id")]
        if tool == "place_equity_order":
            self._next_id += 1
            order_id = f"stub-{self._next_id}"
            self._open.append(order_id)
            return {"stub": True, "tool": tool, "order_id": order_id}
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
        # Check-and-forward is serialised, and every forwarded buy or sell is
        # reserved until the account state reports its order id, so concurrent
        # or back-to-back orders cannot jointly exceed the mandate. A reservation
        # with no order id (backend gave none, or raised) is never released by
        # this process: that fails toward refusing, not toward overspending.
        self._lock = threading.Lock()
        self._reservations: list[tuple[str | None, OpenOrder]] = []

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

    def _effective_account(self) -> AccountState | None:
        try:
            account = self._account_state()
        except Exception:  # account lookup failure must refuse, not crash open
            return None
        if account is None:
            return None
        self._reservations = [
            (oid, o) for oid, o in self._reservations
            if oid is None or oid not in account.known_order_ids
        ]
        return replace(
            account,
            open_orders=tuple(account.open_orders) + tuple(o for _, o in self._reservations),
        )

    def call(self, tool: str, args: object) -> object:
        name = tool[len(SERVER_PREFIX):] if tool.startswith(SERVER_PREFIX) else tool
        with self._lock:
            account = self._effective_account()
            decision = order_guard.check(
                name, args, self._mandate(), account, self.kill_switch_engaged()
            )
            self._audit(decision, args)
            if not decision.allow:
                raise PermissionError(f"BLOCK {name}: {decision.reason}")
            if name != order_guard.EQUITY_ORDER:
                return self._backend.invoke(name, args)
            reservation = OpenOrder(args["symbol"], args["side"],
                                    Decimal(args["quantity"]), Decimal(args["limit_price"]))
            try:
                result = self._backend.invoke(name, args)
            except Exception:
                self._reservations.append((None, reservation))  # may have been placed
                raise
            order_id = result.get("order_id") if isinstance(result, dict) else None
            self._reservations.append(
                (order_id if isinstance(order_id, str) else None, reservation))
            return result

    def engage_kill_switch(self, reason: str) -> list[str]:
        """Refuse all new orders, then attempt to cancel every open order.

        Each cancel is attempted independently; one failure does not leave the
        rest open. Returns the cancelled ids, or raises KillSwitchIncomplete
        naming the ids that could not be cancelled after trying all of them.
        """
        self._kill_path.write_text(
            json.dumps({"engaged_at": self._clock().isoformat(), "reason": reason}),
            encoding="utf-8",
        )
        cancelled, failed = [], []
        for order_id in self._backend.open_order_ids():
            try:
                self.call("cancel_equity_order", {"order_id": order_id})
            except Exception:
                failed.append(order_id)
            else:
                cancelled.append(order_id)
        if failed:
            raise KillSwitchIncomplete(cancelled, failed)
        return cancelled


class KillSwitchIncomplete(RuntimeError):
    def __init__(self, cancelled: list[str], failed: list[str]) -> None:
        super().__init__(f"kill switch engaged; cancel failed for {failed}")
        self.cancelled = cancelled
        self.failed = failed
