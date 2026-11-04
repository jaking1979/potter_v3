"""Negative-test levels 1 and 3 from the A1 containment proposal (PLAN, 2026-11-04).

Level 1: the guard refuses every out-of-mandate order and allows an in-mandate one.
Level 3: with the guard in the call path, refused calls never reach the broker
backend (a recording stub with no network), every attempt is audited, and a
positive control does reach the stub so the test is not vacuous.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from containment.guarded_broker import GuardedBroker, StubBroker
from containment.mandate import MandateError, load_mandate, parse_mandate
from containment.order_guard import AccountState, check

# Synthetic fixture only. The real mandate values are Josh's decision.
MANDATE = {
    "schema": "potter.mandate/v0",
    "ceiling_usd": "1000",
    "instruments": ["AAA", "BBB", "CCC"],
    "max_position_usd": "400",
    "loss_stop_usd": "100",
}
ACCOUNT = AccountState(
    positions={"AAA": D("2")}, prices={"AAA": D("50")}, loss_usd=D("0")
)


def order(**kw):
    base = {"symbol": "BBB", "side": "buy", "quantity": "1", "limit_price": "100"}
    base.update(kw)
    return base


class MandateParsing(unittest.TestCase):
    def test_valid(self):
        m = parse_mandate(MANDATE)
        self.assertEqual(m.instruments, frozenset({"AAA", "BBB", "CCC"}))

    def test_rejections(self):
        bad = [
            {**MANDATE, "ceiling_usd": 1000},              # number, not string
            {**MANDATE, "ceiling_usd": "-5"},
            {**MANDATE, "ceiling_usd": "NaN"},
            {**MANDATE, "instruments": []},
            {**MANDATE, "instruments": ["aaa"]},
            {**MANDATE, "instruments": ["AAA", "AAA"]},
            {**MANDATE, "max_position_usd": "2000"},       # above ceiling
            {**MANDATE, "extra": "x"},
            {k: v for k, v in MANDATE.items() if k != "loss_stop_usd"},
            {**MANDATE, "schema": "other"},
            [],
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(MandateError):
                parse_mandate(data)

    def test_file_floats_and_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps({**MANDATE, "ceiling_usd": 1000.5}))
            with self.assertRaises(MandateError):
                load_mandate(p)
            with self.assertRaises(MandateError):
                load_mandate(Path(d) / "absent.json")


class GuardLevel1(unittest.TestCase):
    def setUp(self):
        self.m = parse_mandate(MANDATE)

    def decide(self, tool="place_equity_order", args=None, mandate="default",
               account=ACCOUNT, kill=False):
        return check(tool, order() if args is None else args,
                     self.m if mandate == "default" else mandate, account, kill)

    def test_positive_control_allows_in_mandate_buy(self):
        self.assertTrue(self.decide().allow)

    def test_refusals(self):
        cases = {
            "disallowed ticker": dict(args=order(symbol="ZZZ")),
            "over max position": dict(args=order(quantity="5")),          # 500 > 400
            "over ceiling": dict(account=AccountState(
                {"AAA": D("6"), "CCC": D("4")}, {"AAA": D("50"), "CCC": D("100")}, D("0")),
                args=order(quantity="3.5")),                              # 700 + 350 > 1000
            "loss stop": dict(account=AccountState({}, {}, D("100"))),
            "option order": dict(tool="place_option_order"),
            "exercise": dict(tool="exercise_option"),
            "unknown tool": dict(tool="place_crypto_order"),
            "no mandate": dict(mandate=None),
            "no account state": dict(account=None),
            "kill switch": dict(kill=True),
            "market order (no limit)": dict(args={"symbol": "BBB", "side": "buy", "quantity": "1"}),
            "extra field": dict(args={**order(), "account": "x"}),
            "float quantity": dict(args=order(quantity=1)),
            "zero quantity": dict(args=order(quantity="0")),
            "infinite price": dict(args=order(limit_price="Infinity")),
            "bad side": dict(args=order(side="short")),
            "short sell": dict(args=order(symbol="AAA", side="sell", quantity="3")),
            "unpriced holding": dict(account=AccountState({"CCC": D("1")}, {}, D("0"))),
            "negative position": dict(account=AccountState({"AAA": D("-1")}, {"AAA": D("1")}, D("0"))),
            "non-dict args": dict(args="BUY BBB"),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.assertFalse(self.decide(**kw).allow, name)

    def test_ceiling_is_inclusive_boundary(self):
        acct = AccountState({"AAA": D("6"), "CCC": D("4")},
                            {"AAA": D("50"), "CCC": D("100")}, D("0"))   # 700 held
        self.assertTrue(self.decide(args=order(quantity="3"), account=acct).allow)   # =1000
        self.assertFalse(self.decide(args=order(quantity="3.01"), account=acct).allow)

    def test_sell_within_holdings_allowed_even_after_loss_stop(self):
        acct = AccountState({"AAA": D("2")}, {"AAA": D("50")}, D("500"))
        self.assertTrue(self.decide(args=order(symbol="AAA", side="sell", quantity="2"),
                                    account=acct).allow)

    def test_cancel_allowed_even_with_kill_switch(self):
        self.assertTrue(self.decide(tool="cancel_equity_order",
                                    args={"order_id": "1"}, kill=True).allow)


class GuardInPathLevel3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.mandate = d / "mandate.json"
        self.mandate.write_text(json.dumps(MANDATE))
        self.kill = d / "KILL"
        self.audit = d / "audit.jsonl"
        self.stub = StubBroker(open_orders=["o1", "o2"])
        self.broker = GuardedBroker(
            self.stub, self.mandate, self.kill, self.audit, lambda: ACCOUNT,
            clock=lambda: datetime(2026, 11, 4, tzinfo=timezone.utc))

    def tearDown(self):
        self.tmp.cleanup()

    def audit_lines(self):
        return [json.loads(l) for l in self.audit.read_text().splitlines()]

    def test_refused_calls_never_reach_backend_and_are_audited(self):
        attempts = [
            ("robinhood-trading__place_equity_order", order(symbol="ZZZ")),
            ("robinhood-trading__place_equity_order", order(quantity="5")),
            ("robinhood-trading__place_option_order", {"symbol": "AAA"}),
            ("robinhood-trading__exercise_option", {"id": "x"}),
        ]
        for tool, args in attempts:
            with self.assertRaises(PermissionError):
                self.broker.call(tool, args)
        self.assertEqual(self.stub.calls, [])
        lines = self.audit_lines()
        self.assertEqual(len(lines), len(attempts))
        self.assertTrue(all(not l["allow"] for l in lines))

    def test_positive_control_reaches_stub(self):
        self.broker.call("robinhood-trading__place_equity_order", order())
        self.assertEqual(self.stub.calls, [("place_equity_order", order())])
        self.assertTrue(self.audit_lines()[0]["allow"])

    def test_deleted_or_corrupt_mandate_refuses_immediately(self):
        self.mandate.write_text("{not json")
        with self.assertRaises(PermissionError):
            self.broker.call("place_equity_order", order())
        self.mandate.unlink()
        with self.assertRaises(PermissionError):
            self.broker.call("place_equity_order", order())
        self.assertEqual(self.stub.calls, [])

    def test_account_lookup_failure_refuses(self):
        def boom():
            raise RuntimeError("api down")
        broker = GuardedBroker(self.stub, self.mandate, self.kill, self.audit, boom)
        with self.assertRaises(PermissionError):
            broker.call("place_equity_order", order())
        self.assertEqual(self.stub.calls, [])

    def test_kill_switch_cancels_open_orders_then_blocks_new_ones(self):
        cancelled = self.broker.engage_kill_switch("test")
        self.assertEqual(cancelled, ["o1", "o2"])
        self.assertEqual(self.stub.open_order_ids(), [])
        self.assertEqual([t for t, _ in self.stub.calls], ["cancel_equity_order"] * 2)
        with self.assertRaises(PermissionError):
            self.broker.call("place_equity_order", order())
        self.assertEqual(len(self.stub.calls), 2)


if __name__ == "__main__":
    unittest.main()
