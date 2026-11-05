"""alpha-guard: the only caller of place_equity_order (T31 PASS 2 row 1, ratified via T32/T34).

The guard refuses (no order) unless ALL of row 1 item 2 hold:
  kill flag readable and "off"; T35 fencing epoch current; mandate file bytes
  hash to the version Josh approved; caller is the Alpha Bot cron; ticker in
  the mandate, equity MARKET order; positions/buying power read <= 60 s ago;
  post-trade exposure within the ceiling and position limit; account value
  above the loss stop.
Item 3: decision ID + UUIDv5 ref_id, journaled pending before the call, never
resubmitted blind. Item 5: an unreadable kill flag or a changed broker
tool-schema hash at startup means paused plus an alert.

Market orders are valued at the fresh quote plus PRICE_BUFFER, a developer
margin that absorbs ordinary slippage. It is NOT a hard bound: a market order
can fill further above the quote (Dolores review on 0b98bbb). Overruns are
handled after the fact: if fresh holdings already exceed the ceiling or the
position limit, every buy is refused and an "overrun" alert is raised until
holdings are back within the mandate; sells stay allowed so exposure can come
down. Whether the ratified market-order contract needs a hard pre-trade dollar
bound is a Rule 12 question for the Council, not something this code decides.
The mandate values themselves are not changed.

Concurrency: check, reservation, broker call and journal update run under an
exclusive cross-process file lock (fcntl.flock on <journal>.lock), shared with
the kill switch, so separate guard processes cannot both pass a check against
the same reserved exposure.

Cancels are not here: only Josh's kill-switch command holds cancel_equity_order
(containment/kill_switch.py). Nothing here is wired to the live account.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from containment.decision_journal import (UNRESOLVED, DecisionJournal, Entry,
                                          decision_id, ref_id_for)
from containment.mandate import MandateError, parse_mandate_bytes

ALPHA_BOT = "alpha-bot"
MAX_STATE_AGE = timedelta(seconds=60)
MAX_CLOCK_SKEW = timedelta(seconds=5)
PRICE_BUFFER = Decimal("0.02")
NEW_YORK = ZoneInfo("America/New_York")


class Refused(PermissionError):
    pass


@contextlib.contextmanager
def exclusive(lock_path: Path):
    """Cross-process exclusive lock; also serialises threads (separate open files)."""
    with open(lock_path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ok_amount(x: object) -> bool:
    return isinstance(x, Decimal) and x.is_finite() and x > 0


@dataclass(frozen=True)
class OpenOrder:
    ref_id: str | None
    symbol: str
    side: str
    quantity: Decimal


@dataclass(frozen=True)
class AccountState:
    read_at: datetime                       # when positions/buying power were read
    positions: dict[str, Decimal]           # ticker -> shares
    prices: dict[str, Decimal]              # ticker -> latest quote
    cash: Decimal                           # buying power
    open_orders: tuple[OpenOrder, ...] = ()
    known_ref_ids: frozenset[str] = field(default_factory=frozenset)  # open or filled


class Broker(Protocol):
    def invoke(self, tool: str, args: dict) -> dict: ...


def read_kill_flag(path: Path) -> str:
    """'off', 'on', or 'unreadable'. Anything but exactly 'off' blocks orders."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    return text if text in ("off", "on") else "unreadable"


class AlphaGuard:
    def __init__(self, *, broker: Broker, journal: DecisionJournal, mandate_path: Path,
                 approved_mandate_sha256: str, kill_flag_path: Path,
                 held_epoch: int, current_epoch: Callable[[], int],
                 account_state: Callable[[], AccountState],
                 expected_schema_sha256: str, current_schema_sha256: Callable[[], str],
                 audit_path: Path, alert: Callable[[str], None],
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.lock = threading.RLock()   # in-process; the file lock below is cross-process
        self.lock_path = Path(str(journal.path) + ".lock")
        self._broker, self._journal = broker, journal
        self._mandate_path, self._approved = Path(mandate_path), approved_mandate_sha256
        self.kill_flag_path = Path(kill_flag_path)
        self._held_epoch, self._current_epoch = held_epoch, current_epoch
        self._account_state, self._clock = account_state, clock
        self._audit_path, self._alert = Path(audit_path), alert
        self.paused_reason: str | None = None
        # Item 5: startup checks. Paused is sticky for this process.
        if read_kill_flag(self.kill_flag_path) == "unreadable":
            self._pause("kill flag unreadable at startup")
        try:
            schema_ok = current_schema_sha256() == expected_schema_sha256
        except Exception:
            schema_ok = False
        if not schema_ok:
            self._pause("broker tool schema changed or unreadable at startup")

    def _pause(self, reason: str) -> None:
        if self.paused_reason is None:
            self.paused_reason = reason
            self._alert(f"Alpha Bot paused: {reason}")

    def _audit(self, record: dict) -> None:
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def place(self, *, caller: str, symbol: str, side: str, quantity: str,
              signal_version: str) -> dict:
        with self.lock, exclusive(self.lock_path):
            now = self._clock()
            record = {"at": now.isoformat(), "caller": caller, "symbol": symbol,
                      "side": side, "quantity": quantity, "signal_version": signal_version}
            try:
                entry = self._check(caller, symbol, side, quantity, signal_version, now)
            except Refused as exc:
                record.update(allow=False, reason=str(exc))
                try:
                    self._audit(record)
                finally:
                    self._alert(f"Alpha Bot order refused: {exc}")
                raise
            record.update(allow=True, ref_id=entry.ref_id, decision_id=entry.decision_id)
            self._audit(record)                              # audit failure => no order
            self._journal.write_pending(entry, now)          # durable before the call
            args = {"symbol": symbol, "side": side, "quantity": quantity,
                    "type": "market", "ref_id": entry.ref_id}
            try:
                result = self._broker.invoke("place_equity_order", args)
            except BaseException:
                self._journal.set_status(entry.decision_id, "unknown", self._clock())
                raise
            order_id = result.get("order_id") if isinstance(result, dict) else None
            self._journal.set_status(entry.decision_id, "placed", self._clock(),
                                     order_id if isinstance(order_id, str) else None)
            return result

    def _check(self, caller: str, symbol: str, side: str, quantity: str,
               signal_version: str, now: datetime) -> Entry:
        if caller != ALPHA_BOT:
            raise Refused("only the Alpha Bot cron may place orders")
        if self.paused_reason:
            raise Refused(f"paused: {self.paused_reason}")
        flag = read_kill_flag(self.kill_flag_path)
        if flag != "off":
            raise Refused(f"kill flag {flag}")
        try:
            epoch_ok = self._current_epoch() == self._held_epoch
        except Exception:
            epoch_ok = False
        if not epoch_ok:
            raise Refused("fencing epoch stale or unreadable")
        try:
            raw = self._mandate_path.read_bytes()
        except OSError:
            raise Refused("mandate unreadable") from None
        if hashlib.sha256(raw).hexdigest() != self._approved:
            raise Refused("mandate is not the approved version")
        try:
            mandate = parse_mandate_bytes(raw)
        except MandateError as exc:
            raise Refused(f"mandate invalid: {exc}") from None
        if symbol not in mandate.instruments:
            raise Refused("ticker not in mandate")
        if side not in ("buy", "sell"):
            raise Refused("side must be buy or sell")
        if not isinstance(signal_version, str) or not signal_version or ":" in signal_version:
            raise Refused("signal_version required")
        try:
            qty = Decimal(quantity) if isinstance(quantity, str) else None
        except InvalidOperation:
            qty = None
        if qty is None or not qty.is_finite() or qty <= 0:
            raise Refused("quantity must be a positive decimal string")

        try:
            acct = self._account_state()
        except Exception:
            raise Refused("account state unavailable") from None
        if acct.read_at.tzinfo is None or not (
                -MAX_CLOCK_SKEW <= now - acct.read_at <= MAX_STATE_AGE):
            raise Refused("positions/buying power not read within 60 s")
        if (not isinstance(acct.cash, Decimal) or not acct.cash.is_finite() or acct.cash < 0
                or any(not isinstance(q, Decimal) or not q.is_finite() or q < 0
                       for q in acct.positions.values())
                or any(not _ok_amount(p) for p in acct.prices.values())
                or any(o.side not in ("buy", "sell") or not _ok_amount(o.quantity)
                       for o in acct.open_orders)):
            raise Refused("account state has invalid amounts")
        # Every symbol whose exposure is counted needs its OWN fresh quote: never
        # substitute another ticker's price (Dolores review on 0b98bbb).
        needed = {s for s, q in acct.positions.items() if q} | {symbol} | {
            o.symbol for o in acct.open_orders if o.side == "buy"}
        if not needed <= set(acct.prices):
            raise Refused("missing quote for a held position or open buy")
        price = acct.prices[symbol]
        holdings = sum((q * acct.prices[s] for s, q in acct.positions.items() if q),
                       Decimal(0))
        if holdings + acct.cash <= mandate.loss_stop_account_value_usd:
            raise Refused("account value at or below loss stop")

        decision = decision_id(now.astimezone(NEW_YORK).date().isoformat(), symbol, side,
                               signal_version)
        self._journal.reconcile(acct.known_ref_ids, now)
        prior = self._journal.get(decision)
        if prior is not None and prior.status in UNRESOLVED:
            raise Refused("previous attempt unresolved; reconcile before retry")
        if prior is not None and prior.status == "placed":
            raise Refused("decision already placed")
        # Exposure not yet visible in positions: broker open orders, plus our
        # journaled orders the broker has not reported yet.
        unseen = [e for e in self._journal.outstanding() if e.ref_id not in acct.known_ref_ids]
        held = acct.positions.get(symbol, Decimal(0))

        if side == "sell":
            pending_sells = sum((o.quantity for o in acct.open_orders
                                 if o.side == "sell" and o.symbol == symbol), Decimal(0))
            pending_sells += sum((e.quantity for e in unseen
                                  if e.side == "sell" and e.symbol == symbol), Decimal(0))
            if qty + pending_sells > held:
                raise Refused("sell exceeds shares held net of open sells")
            return Entry(decision, ref_id_for(decision), symbol, side, qty, Decimal(0),
                         "pending", None)

        # Overrun (a fill beyond the buffer, or any other excess): no buys until back in bounds.
        over_position = any(q * acct.prices[s] > mandate.max_position_usd
                            for s, q in acct.positions.items() if q)
        if holdings > mandate.ceiling_usd or over_position:
            self._alert("Alpha Bot overrun: holdings exceed the mandate; buys refused")
            raise Refused("holdings already exceed the mandate (overrun)")

        def worst(q: Decimal, s: str) -> Decimal:
            return q * acct.prices[s] * (1 + PRICE_BUFFER)

        open_buys = [(o.symbol, worst(o.quantity, o.symbol)) for o in acct.open_orders
                     if o.side == "buy"]
        if any(not _ok_amount(e.reserve_usd) for e in unseen if e.side == "buy"):
            raise Refused("journal reservation invalid")
        open_buys += [(e.symbol, e.reserve_usd) for e in unseen if e.side == "buy"]
        cost = worst(qty, symbol)
        if cost > acct.cash - sum(v for _, v in open_buys):
            raise Refused("insufficient buying power for worst-case fill")
        position = held * price + sum(v for s, v in open_buys if s == symbol) + cost
        if position > mandate.max_position_usd:
            raise Refused("post-trade position exceeds maximum")
        if holdings + sum(v for _, v in open_buys) + cost > mandate.ceiling_usd:
            raise Refused("post-trade exposure exceeds ceiling")
        return Entry(decision, ref_id_for(decision), symbol, side, qty, cost, "pending", None)
