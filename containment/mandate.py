"""Alpha Bot mandate (T32 step 2; T31 PASS 2 row 1).

The mandate VALUES are Josh's (draft on the Council, 2026-10-23: $250 ceiling,
the 9 ETFs as equity market orders, up to 100% in one ETF, pause below $200
account value). They are not in this repository. The guard accepts a mandate
file only when its exact bytes hash to the version Josh approved (row 1 item 2),
so this module parses bytes and the guard checks the hash first.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

SCHEMA = "potter.mandate/v1"
_SYMBOL = re.compile(r"^[A-Z]{1,5}$")
_FIELDS = {"schema", "ceiling_usd", "instruments", "max_position_usd",
           "loss_stop_account_value_usd"}


class MandateError(ValueError):
    """The mandate is missing or malformed. Fail closed."""


@dataclass(frozen=True)
class Mandate:
    ceiling_usd: Decimal                    # max post-trade market value held
    instruments: frozenset[str]             # the only tickers that may be traded
    max_position_usd: Decimal               # max post-trade value of one ticker
    loss_stop_account_value_usd: Decimal    # no order when account value is at or below this


def _money(raw: object, name: str) -> Decimal:
    if not isinstance(raw, str):  # floats lose precision; bools are ints
        raise MandateError(f"{name} must be a decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise MandateError(f"{name} is not a decimal") from exc
    if not value.is_finite() or value <= 0:
        raise MandateError(f"{name} must be a positive finite amount")
    return value


def parse_mandate_bytes(raw: bytes) -> Mandate:
    try:
        # JSON floats become None so _money rejects them.
        data = json.loads(raw.decode("utf-8"), parse_float=lambda _s: None)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MandateError("mandate is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise MandateError(f"mandate fields must be exactly {sorted(_FIELDS)}")
    if data["schema"] != SCHEMA:
        raise MandateError("unknown mandate schema")
    symbols = data["instruments"]
    if (not isinstance(symbols, list) or not symbols
            or any(not isinstance(s, str) or not _SYMBOL.match(s) for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise MandateError("instruments must be distinct upper-case tickers")
    mandate = Mandate(
        ceiling_usd=_money(data["ceiling_usd"], "ceiling_usd"),
        instruments=frozenset(symbols),
        max_position_usd=_money(data["max_position_usd"], "max_position_usd"),
        loss_stop_account_value_usd=_money(data["loss_stop_account_value_usd"],
                                           "loss_stop_account_value_usd"),
    )
    if mandate.max_position_usd > mandate.ceiling_usd:
        raise MandateError("max_position_usd exceeds ceiling_usd")
    if mandate.loss_stop_account_value_usd >= mandate.ceiling_usd:
        raise MandateError("loss stop must be below the ceiling")
    return mandate
