#!/usr/bin/env python3
"""Trading run gate: the checks a scheduled trading run must pass before and while it acts.

A plain program, no model, no brokerage access, no network. It decides nothing about WHAT to trade. It only
answers "may this run act at all, and are these exact orders inside the written mandate", and it keeps the run
log, so "logged before and after" does not depend on anyone remembering.

  trading_precheck.py check   --mandate F --kill-switch F --log F [--tz America/New_York] [--holidays F] [--record]
  trading_precheck.py orders  --mandate F --kill-switch F --log F --run-id ID --orders ORDERS.json [--record]
  trading_precheck.py result  --log F --ref REF --status placed|filled|rejected|cancelled|unknown [--broker-order-id X]
  trading_precheck.py end     --log F --run-id ID --status ok|error [--note TEXT]
  trading_precheck.py resolve --log F --note "what the owner decided"

Exit 0 = may proceed (prints "OK: ..."). Exit 1 = must not (prints "REFUSE: <one-line reason>"). Exit 2 = bad
arguments. With --record, `check` writes run_start or precheck_failed and `orders` writes one order_intent per
order BEFORE anything is sent to the brokerage.

The mandate is a text file that starts with a block of `key: value` lines between two `---` lines; the owner's
prose follows. Fields (see REQUIRED): status, approved_by, approved_on, window_start, window_end, account (a
label, never a number), capital_limit, max_order_value, max_orders_per_run, instruments, universe,
order_approval (per_order | autonomous), strategy.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REQUIRED = ("status", "approved_by", "approved_on", "window_start", "window_end", "account", "capital_limit",
            "max_order_value", "max_orders_per_run", "instruments", "universe", "order_approval", "strategy")
INSTRUMENTS = {"equity", "etf", "option", "crypto"}
APPROVAL = {"per_order", "autonomous"}
RESULT_STATUS = {"placed", "filled", "rejected", "cancelled", "unknown"}
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_NUMBER = re.compile(r"^\d+(\.\d+)?$")


class Refuse(Exception):
    """The run must not proceed. The message is the one-line reason."""


# ---------------------------------------------------------------- mandate

def parse_mandate(text: str) -> dict:
    """The `key: value` block at the top of a mandate file, as a dict of strings. Raises Refuse if malformed."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise Refuse("mandate has no field block (it must start with a line of ---)")
    out, closed = {}, False
    for raw in lines[1:]:
        line = raw.strip()
        if line == "---":
            closed = True
            break
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if not sep or not re.fullmatch(r"[a-z_]+", key):
            raise Refuse(f"mandate field block has a line that is not 'key: value': {line[:40]!r}")
        if key in out:
            raise Refuse(f"mandate field '{key}' appears twice")
        out[key] = value.strip()
    if not closed:
        raise Refuse("mandate field block is not closed with a line of ---")
    return out


def _date(fields: dict, key: str) -> date:
    try:
        return date.fromisoformat(fields[key])
    except ValueError:
        raise Refuse(f"mandate field '{key}' is not a date (YYYY-MM-DD)") from None


def _number(fields: dict, key: str) -> float:
    if not _NUMBER.match(fields[key]):
        raise Refuse(f"mandate field '{key}' must be a plain number")
    n = float(fields[key])
    if n <= 0:
        raise Refuse(f"mandate field '{key}' must be greater than zero")
    return n


def _list(fields: dict, key: str) -> list[str]:
    items = [x.strip() for x in fields[key].split(",") if x.strip()]
    if not items:
        raise Refuse(f"mandate field '{key}' is empty")
    return items


def validate_mandate(fields: dict) -> dict:
    """Check every required field and return the mandate with typed values. Raises Refuse on the first problem."""
    missing = [k for k in REQUIRED if not fields.get(k)]
    if missing:
        raise Refuse("mandate is missing required field(s): " + ", ".join(missing))
    m = {"status": fields["status"].lower(), "approved_by": fields["approved_by"], "strategy": fields["strategy"],
         "account": fields["account"], "order_approval": fields["order_approval"].lower()}
    for k in ("approved_on", "window_start", "window_end"):
        m[k] = _date(fields, k)
    if m["window_end"] < m["window_start"]:
        raise Refuse("mandate window ends before it starts")
    if re.search(r"\d{5,}", m["account"]):
        raise Refuse("mandate field 'account' must be a label, not an account number")
    m["capital_limit"] = _number(fields, "capital_limit")
    m["max_order_value"] = _number(fields, "max_order_value")
    if m["max_order_value"] > m["capital_limit"]:
        raise Refuse("mandate max_order_value is larger than capital_limit")
    if not fields["max_orders_per_run"].isdigit() or int(fields["max_orders_per_run"]) < 1:
        raise Refuse("mandate field 'max_orders_per_run' must be a whole number of at least 1")
    m["max_orders_per_run"] = int(fields["max_orders_per_run"])
    m["instruments"] = [x.lower() for x in _list(fields, "instruments")]
    unknown = sorted(set(m["instruments"]) - INSTRUMENTS)
    if unknown:
        raise Refuse("mandate names unknown instrument type(s): " + ", ".join(unknown))
    m["universe"] = [x.upper() for x in _list(fields, "universe")]
    bad = [s for s in m["universe"] if not _SYMBOL.match(s)]
    if bad:
        raise Refuse("mandate universe has an entry that is not a ticker symbol")
    if m["order_approval"] not in APPROVAL:
        raise Refuse("mandate field 'order_approval' must be per_order or autonomous")
    return m


def load_mandate(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Refuse("mandate file is missing") from None
    except (OSError, UnicodeError):
        raise Refuse("mandate file cannot be read") from None
    return validate_mandate(parse_mandate(text))


def check_authority(m: dict, today: date) -> None:
    """Is the mandate in force today?"""
    if m["status"] != "approved":
        raise Refuse(f"mandate status is '{m['status'][:20]}', not approved")
    if m["approved_on"] > today:
        raise Refuse("mandate approval date is in the future")
    if today < m["window_start"]:
        raise Refuse(f"mandate window has not started (starts {m['window_start']})")
    if today > m["window_end"]:
        raise Refuse(f"mandate expired on {m['window_end']}")


# ---------------------------------------------------------------- calendar, kill switch

def check_market_day(today: date, holidays: set[date] = frozenset()) -> None:
    if today.weekday() >= 5:
        raise Refuse(f"{today} is not a market weekday")
    if today in holidays:
        raise Refuse(f"{today} is a listed market holiday")


def load_holidays(path: str | None) -> set[date]:
    """Dates (YYYY-MM-DD, one per line, # comments) on which the market is closed. A bad file is a refusal."""
    if not path:
        return set()
    out = set()
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(date.fromisoformat(line))
    except (OSError, ValueError):
        raise Refuse("holiday file is missing or unreadable") from None
    return out


def check_kill_switch(path: Path) -> None:
    if os.path.lexists(path):
        raise Refuse("kill switch is on (the stop file exists)")


# ---------------------------------------------------------------- run log

def read_log(path: Path) -> list:
    """Run log as a list of events. A line that is not a JSON object with an 'event' becomes None."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        raise Refuse("run log cannot be read") from None
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            d = None
        out.append(d if isinstance(d, dict) and isinstance(d.get("event"), str) else None)
    return out


def _local_day(ev: dict, tz: ZoneInfo) -> date | None:
    try:
        d = datetime.fromisoformat(str(ev.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(tz).date()


def log_problem(events: list, today: date, tz: ZoneInfo, current_run: str | None = None) -> str | None:
    """Why the log forbids acting now, or None. Looks only at what follows the last 'resolved' entry.

    current_run=None: start of a run. Any error, any unfinished run, any order without a recorded result, or a
    run already completed today is a problem. current_run=ID: mid-run. That run must be the open one, and every
    order it already logged must have its result.
    """
    last = max((i for i, e in enumerate(events) if e and e["event"] == "resolved"), default=-1)
    tail = events[last + 1:]
    if any(e is None for e in tail):
        return "run log has an unreadable line"
    results = {e.get("ref"): e.get("status") for e in tail if e["event"] == "order_result"}
    for e in tail:
        if e["event"] == "error" or (e["event"] == "run_end" and e.get("status") != "ok"):
            return f"unresolved error in the run log ({str(e.get('ts'))[:19]})"
        if e["event"] == "order_intent":
            if e.get("ref") not in results:
                return f"order {str(e.get('ref'))[:40]} was logged as intended but has no recorded result"
            if results[e.get("ref")] == "unknown":
                return f"order {str(e.get('ref'))[:40]} is in an unknown state"
    ended = {e.get("run_id") for e in tail if e["event"] == "run_end"}
    open_runs = [e.get("run_id") for e in tail if e["event"] == "run_start" and e.get("run_id") not in ended]
    if current_run is None:
        if open_runs:
            return f"run {str(open_runs[-1])[:30]} started but never finished"
        if any(e["event"] == "run_end" and _local_day(e, tz) == today for e in tail):
            return "a run already completed today"
    elif open_runs != [current_run]:
        return "this run is not the one open in the run log"
    return None


def append_log(path: Path, event: dict, now: datetime) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now.astimezone(timezone.utc).isoformat(timespec="seconds"), **event}) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        raise Refuse("run log cannot be written, so nothing may be traded") from None


# ---------------------------------------------------------------- orders

def check_orders(m: dict, orders, already: list[dict]) -> None:
    """Are these exact proposed orders inside the mandate? `already` = this run's earlier order_intent entries."""
    if not isinstance(orders, list) or not orders:
        raise Refuse("no proposed orders given")
    if len(orders) + len(already) > m["max_orders_per_run"]:
        raise Refuse("more orders than the mandate allows in one run")
    seen = {(e.get("symbol"), e.get("side")) for e in already}
    spent = sum(float(e.get("value") or 0) for e in already if e.get("side") == "buy")
    for i, o in enumerate(orders, 1):
        if not isinstance(o, dict):
            raise Refuse(f"order {i} is not an object")
        sym, side, inst, value = o.get("symbol"), o.get("side"), o.get("instrument"), o.get("value")
        if side not in ("buy", "sell"):
            raise Refuse(f"order {i} has no valid side (buy or sell)")
        if sym not in m["universe"]:
            raise Refuse(f"order {i} is for a symbol outside the mandate")
        if inst not in m["instruments"]:
            raise Refuse(f"order {i} is for an instrument type outside the mandate")
        if (sym, side) in seen:
            raise Refuse(f"order {i} repeats an order already made in this run")
        seen.add((sym, side))
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
            raise Refuse(f"order {i} has a value that is not a positive number")
        if side == "buy":
            if value is None:
                raise Refuse(f"order {i} is a buy without a value")
            if value > m["max_order_value"]:
                raise Refuse(f"order {i} is larger than the mandate's limit for one order")
            spent += value
            if spent > m["capital_limit"]:
                raise Refuse("the buys in this run add up to more than the mandate's capital limit")
        if m["order_approval"] == "per_order" and o.get("owner_approved") is not True:
            raise Refuse(f"order {i} needs the owner's approval of that exact order first")


# ---------------------------------------------------------------- the two gates

def precheck(mandate: Path, kill_switch: Path, log: Path, today: date, tz: ZoneInfo,
             holidays: set[date] = frozenset(), current_run: str | None = None) -> dict:
    """Everything that must hold before a run may act. Returns the mandate. Raises Refuse with the reason."""
    check_kill_switch(kill_switch)
    m = load_mandate(mandate)
    check_authority(m, today)
    check_market_day(today, holidays)
    problem = log_problem(read_log(log), today, tz, current_run)
    if problem:
        raise Refuse(problem)
    return m


def gate_orders(mandate: Path, kill_switch: Path, log: Path, today: date, tz: ZoneInfo, run_id: str, orders,
                holidays: set[date] = frozenset()) -> tuple[dict, list[dict]]:
    """Mid-run gate for a batch of proposed orders. Returns (mandate, order_intent entries to log)."""
    m = precheck(mandate, kill_switch, log, today, tz, holidays, current_run=run_id)
    already = [e for e in read_log(log) if e and e["event"] == "order_intent" and e.get("run_id") == run_id]
    check_orders(m, orders, already)
    intents = [{"event": "order_intent", "run_id": run_id, "ref": f"{run_id}-{len(already) + n}",
                "symbol": o["symbol"], "side": o["side"], "instrument": o["instrument"], "value": o.get("value"),
                "quantity": o.get("quantity")} for n, o in enumerate(orders, 1)]
    return m, intents


def main(argv: list[str], now: datetime | None = None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "orders"):
        p = sub.add_parser(name)
        p.add_argument("--mandate", required=True)
        p.add_argument("--kill-switch", required=True)
        p.add_argument("--log", required=True)
        p.add_argument("--tz", default="America/New_York")
        p.add_argument("--holidays", default=None)
        p.add_argument("--today", default=None, help="YYYY-MM-DD, for testing")
        p.add_argument("--record", action="store_true")
        if name == "orders":
            p.add_argument("--run-id", required=True)
            p.add_argument("--orders", required=True, help="JSON file: [{symbol, side, instrument, value}]")
    p = sub.add_parser("result")
    p.add_argument("--log", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--status", required=True, choices=sorted(RESULT_STATUS))
    p.add_argument("--broker-order-id", default=None)
    p.add_argument("--note", default=None)
    p = sub.add_parser("end")
    p.add_argument("--log", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--status", required=True, choices=["ok", "error"])
    p.add_argument("--note", default=None)
    p = sub.add_parser("resolve")
    p.add_argument("--log", required=True)
    p.add_argument("--note", required=True)
    ns = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    log = Path(ns.log)

    def say(ok: bool, text: str) -> int:
        print(("OK: " if ok else "REFUSE: ") + " ".join(str(text).split())[:300], file=out)
        return 0 if ok else 1

    try:
        if ns.cmd == "result":
            if not any(e and e["event"] == "order_intent" and e.get("ref") == ns.ref for e in read_log(log)):
                raise Refuse("no logged order has that ref")
            append_log(log, {"event": "order_result", "ref": ns.ref, "status": ns.status,
                             "broker_order_id": ns.broker_order_id, "note": ns.note}, now)
            return say(True, f"result recorded for {ns.ref}")
        if ns.cmd == "end":
            append_log(log, {"event": "run_end", "run_id": ns.run_id, "status": ns.status, "note": ns.note}, now)
            return say(True, f"run {ns.run_id} closed as {ns.status}")
        if ns.cmd == "resolve":
            append_log(log, {"event": "resolved", "note": ns.note}, now)
            return say(True, "earlier problems marked resolved")

        try:
            tz = ZoneInfo(ns.tz)
            today = date.fromisoformat(ns.today) if ns.today else now.astimezone(tz).date()
        except (ValueError, OSError, LookupError):
            raise Refuse("time zone or date argument is not valid") from None
        holidays = load_holidays(ns.holidays)
        if ns.cmd == "check":
            try:
                m = precheck(Path(ns.mandate), Path(ns.kill_switch), log, today, tz, holidays)
            except Refuse as e:
                if ns.record:
                    try:
                        append_log(log, {"event": "precheck_failed", "reason": str(e)}, now)
                    except Refuse:
                        pass
                raise
            run_id = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            if ns.record:
                append_log(log, {"event": "run_start", "run_id": run_id, "order_approval": m["order_approval"]}, now)
            return say(True, f"may proceed run_id={run_id} order_approval={m['order_approval']} "
                             f"window_end={m['window_end']}")
        try:
            orders = json.loads(Path(ns.orders).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Refuse("orders file is missing or is not JSON") from None
        _m, intents = gate_orders(Path(ns.mandate), Path(ns.kill_switch), log, today, tz, ns.run_id, orders, holidays)
        if ns.record:
            for ev in intents:
                append_log(log, ev, now)
        return say(True, f"{len(intents)} order(s) inside the mandate; refs " + " ".join(e["ref"] for e in intents))
    except Refuse as e:
        return say(False, str(e))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
