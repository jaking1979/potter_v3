"""Pre-order code check (T32 step 2), fail-closed.

Every call to an order-capable brokerage tool goes through `check`. Anything
the guard does not positively recognise as an in-mandate equity order is
refused: unknown tools, options, exercises, malformed orders, a missing
mandate, missing account state, an engaged kill switch, or a breached loss stop.

Cancels are allowed because they only reduce exposure and the kill switch
depends on them. Sells are allowed for mandate instruments up to the quantity
held, so the bot can always exit; they cannot open a short.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping

from containment.mandate import Mandate

EQUITY_ORDER = "place_equity_order"
CANCEL_PREFIX = "cancel_"  # exact cancel tool names are confirmed on the host, not here
ALWAYS_REFUSED = frozenset({"place_option_order", "exercise_option"})
_ORDER_FIELDS = {"symbol", "side", "quantity", "limit_price"}


@dataclass(frozen=True)
class OpenOrder:
    symbol: str
    side: str                              # "buy" or "sell"
    quantity: Decimal                      # unfilled quantity
    limit_price: Decimal


@dataclass(frozen=True)
class AccountState:
    positions: Mapping[str, Decimal]       # symbol -> quantity held
    prices: Mapping[str, Decimal]          # symbol -> latest price used for valuation
    loss_usd: Decimal                      # realised + unrealised loss since mandate start (>= 0)
    # Unfilled orders count as if they will fill: buys at their limit price
    # against position and ceiling, sells against the quantity available to sell.
    open_orders: tuple[OpenOrder, ...] = field(default_factory=tuple)
    # Broker order ids already reflected above (open or filled). GuardedBroker
    # keeps its own reservation for each order it forwards until its id shows here.
    known_order_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    allow: bool
    tool: str
    reason: str


def _refuse(tool: str, reason: str) -> Decision:
    return Decision(False, tool, reason)


def _positive_decimal(raw: object) -> Decimal | None:
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def check(
    tool: str,
    args: object,
    mandate: Mandate | None,
    account: AccountState | None,
    kill_switch_engaged: bool,
) -> Decision:
    if tool.startswith(CANCEL_PREFIX):
        return Decision(True, tool, "cancel reduces exposure")
    if tool in ALWAYS_REFUSED:
        return _refuse(tool, "options and exercises are outside the mandate")
    if tool != EQUITY_ORDER:
        return _refuse(tool, "unknown order-capable tool")
    if kill_switch_engaged:
        return _refuse(tool, "kill switch engaged")
    if mandate is None:
        return _refuse(tool, "no valid mandate")
    if account is None:
        return _refuse(tool, "account state unavailable")
    if not isinstance(args, dict) or set(args) != _ORDER_FIELDS:
        return _refuse(tool, "order must have exactly symbol, side, quantity, limit_price")

    symbol, side = args["symbol"], args["side"]
    quantity = _positive_decimal(args["quantity"])
    limit_price = _positive_decimal(args["limit_price"])
    if quantity is None or limit_price is None:
        return _refuse(tool, "quantity and limit_price must be positive decimal strings")
    if symbol not in mandate.instruments:
        return _refuse(tool, "instrument not in mandate")
    if any(qty < 0 for qty in account.positions.values()):
        return _refuse(tool, "account state shows a negative position")
    held = account.positions.get(symbol, Decimal(0))
    pending = account.open_orders
    if any(o.side not in ("buy", "sell") or o.quantity <= 0 or o.limit_price <= 0 for o in pending):
        return _refuse(tool, "account state shows a malformed open order")

    if side == "sell":
        pending_sells = sum((o.quantity for o in pending
                             if o.side == "sell" and o.symbol == symbol), Decimal(0))
        if quantity + pending_sells > held:
            return _refuse(tool, "sell exceeds quantity held net of open sells (no shorting)")
        return Decision(True, tool, "sell within holdings")
    if side != "buy":
        return _refuse(tool, "side must be buy or sell")

    if account.loss_usd >= mandate.loss_stop_usd:
        return _refuse(tool, "loss stop reached")
    try:
        holdings_value = sum(
            (qty * account.prices[sym] for sym, qty in account.positions.items() if qty),
            Decimal(0),
        )
    except KeyError:
        return _refuse(tool, "missing price for a held position")
    # Value new and open buys at their limit price: the most they can cost.
    order_value = quantity * limit_price
    open_buys = [o for o in pending if o.side == "buy"]
    open_buy_value = sum((o.quantity * o.limit_price for o in open_buys), Decimal(0))
    open_buy_symbol = sum((o.quantity * o.limit_price for o in open_buys if o.symbol == symbol),
                          Decimal(0))
    position_value = held * account.prices.get(symbol, limit_price) + open_buy_symbol + order_value
    if position_value > mandate.max_position_usd:
        return _refuse(tool, "order exceeds maximum position (including open buys)")
    if holdings_value + open_buy_value + order_value > mandate.ceiling_usd:
        return _refuse(tool, "order exceeds ceiling (including open buys)")
    return Decision(True, tool, "buy within mandate")
