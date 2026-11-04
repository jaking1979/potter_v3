"""Alpha Bot mandate (T32 step 2): ceiling, instruments, maximum position, loss stop.

The mandate VALUES are Josh's decision and are not in this repository. This
module only defines the shape and refuses anything malformed. A missing or
invalid mandate is an error; callers must treat that as "refuse every order".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "potter.mandate/v0"
_SYMBOL = re.compile(r"^[A-Z]{1,5}$")
_FIELDS = {"schema", "ceiling_usd", "instruments", "max_position_usd", "loss_stop_usd"}


class MandateError(ValueError):
    """The mandate is missing or malformed. Fail closed."""


@dataclass(frozen=True)
class Mandate:
    ceiling_usd: Decimal          # maximum total market value held across all instruments
    instruments: frozenset[str]   # the only symbols that may be bought
    max_position_usd: Decimal     # maximum market value of any single symbol
    loss_stop_usd: Decimal        # realised + unrealised loss at which buying stops


def _money(raw: object, name: str) -> Decimal:
    # Strings only: floats silently lose precision and bools are ints in Python.
    if not isinstance(raw, str):
        raise MandateError(f"{name} must be a decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise MandateError(f"{name} is not a decimal") from exc
    if not value.is_finite() or value <= 0:
        raise MandateError(f"{name} must be a positive finite amount")
    return value


def parse_mandate(data: object) -> Mandate:
    if not isinstance(data, dict):
        raise MandateError("mandate must be a JSON object")
    if set(data) != _FIELDS:
        raise MandateError(f"mandate fields must be exactly {sorted(_FIELDS)}")
    if data["schema"] != SCHEMA:
        raise MandateError("unknown mandate schema")
    symbols = data["instruments"]
    if not isinstance(symbols, list) or not symbols:
        raise MandateError("instruments must be a non-empty list")
    if any(not isinstance(s, str) or not _SYMBOL.match(s) for s in symbols):
        raise MandateError("instruments must be upper-case ticker symbols")
    if len(set(symbols)) != len(symbols):
        raise MandateError("instruments contain duplicates")
    mandate = Mandate(
        ceiling_usd=_money(data["ceiling_usd"], "ceiling_usd"),
        instruments=frozenset(symbols),
        max_position_usd=_money(data["max_position_usd"], "max_position_usd"),
        loss_stop_usd=_money(data["loss_stop_usd"], "loss_stop_usd"),
    )
    if mandate.max_position_usd > mandate.ceiling_usd:
        raise MandateError("max_position_usd exceeds ceiling_usd")
    return mandate


def load_mandate(path: Path) -> Mandate:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MandateError("mandate file unreadable") from exc
    try:
        # JSON floats become None so _money rejects them; amounts must be strings.
        data = json.loads(text, parse_float=lambda _s: None)
    except json.JSONDecodeError as exc:
        raise MandateError("mandate is not valid JSON") from exc
    return parse_mandate(data)
