"""Josh's kill switch (T32 step 2; T31 PASS 2 row 1 items 1, 5, 7).

Only this path holds cancel_equity_order, and it is triggered by Josh's command,
not by any agent. Engaging takes the guard's lock, so an order already inside
the guard completes and is journaled before the sweep, and no new order can
start until the flag reads "on". The sweep cancels only the Alpha Bot's own
orders (ref_ids in the decision journal) that the broker still reports open;
it never touches other open orders. Each cancel is attempted independently.

Not wired to Telegram or the live account here; that is a host step.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from containment.alpha_guard import AlphaGuard
from containment.decision_journal import DecisionJournal


class Broker(Protocol):
    def invoke(self, tool: str, args: dict) -> dict: ...
    def open_orders(self) -> list[dict]: ...   # [{"order_id":..., "ref_id":...}, ...]


class KillSwitchIncomplete(RuntimeError):
    def __init__(self, cancelled: list[str], failed: list[str]) -> None:
        super().__init__(f"kill flag on; cancel failed for {failed}")
        self.cancelled, self.failed = cancelled, failed


def write_flag(path: Path, value: str) -> None:
    """Atomic replace so a reader never sees a partial flag."""
    if value not in ("on", "off"):
        raise ValueError("flag must be on or off")
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kill-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def engage(guard: AlphaGuard, broker: Broker, journal: DecisionJournal) -> list[str]:
    """Flag on, then cancel the bot's open orders. Returns cancelled order ids."""
    from containment.alpha_guard import exclusive
    with guard.lock, exclusive(guard.lock_path):
        write_flag(guard.kill_flag_path, "on")
        bot_ref_ids = journal.all_ref_ids()
        cancelled, failed = [], []
        for order in broker.open_orders():
            if order.get("ref_id") not in bot_ref_ids:
                continue
            try:
                broker.invoke("cancel_equity_order", {"order_id": order["order_id"]})
            except Exception:
                failed.append(order["order_id"])
            else:
                cancelled.append(order["order_id"])
        if failed:
            raise KillSwitchIncomplete(cancelled, failed)
        return cancelled
