"""alpha-guard and kill switch against T31 PASS 2 row 1 (stub broker only).

Row 1 item 7 release tests are named rt_*; the rest cover Dolores's and
Codex's review findings. No test touches a network or a real account.
"""
import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from containment import kill_switch
from containment.alpha_guard import AccountState, AlphaGuard, OpenOrder, Refused
from containment.decision_journal import DecisionJournal, decision_id, ref_id_for

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)   # 10:00 New York
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III"]
MANDATE = json.dumps({   # synthetic; mirrors the draft's shape, not Josh's file
    "schema": "potter.mandate/v1", "ceiling_usd": "250", "instruments": TICKERS,
    "max_position_usd": "250", "loss_stop_account_value_usd": "200",
}).encode()
SCHEMA = "schema-v1"


class StubBroker:
    """Records calls; dedupes by ref_id like the upstream; no network."""

    def __init__(self):
        self.calls, self._open, self._by_ref = [], [], {}
        self.fail_after_submit = False
        self.fail_before_submit = False
        self.fail_cancel = set()
        self.on_place = None

    def invoke(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == "place_equity_order":
            if self.on_place:
                self.on_place()
            if self.fail_before_submit:
                raise ConnectionError("refused before submit")
            ref = args["ref_id"]
            if ref not in self._by_ref:
                oid = f"o{len(self._by_ref) + 1}"
                self._by_ref[ref] = oid
                self._open.append({"order_id": oid, "ref_id": ref})
            if self.fail_after_submit:
                raise TimeoutError("timeout after submit")
            return {"order_id": self._by_ref[ref]}
        if tool == "cancel_equity_order":
            if args["order_id"] in self.fail_cancel:
                raise RuntimeError("cancel failed")
            self._open = [o for o in self._open if o["order_id"] != args["order_id"]]
            return {}
        raise AssertionError(tool)

    def open_orders(self):
        return list(self._open)

    def placements(self):
        return [a for t, a in self.calls if t == "place_equity_order"]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.mandate = d / "mandate.json"
        self.mandate.write_bytes(MANDATE)
        self.approved = hashlib.sha256(MANDATE).hexdigest()
        self.flag = d / "kill.flag"
        kill_switch.write_flag(self.flag, "off")
        self.audit = d / "audit.jsonl"
        self.journal_path = d / "journal.db"
        self.journal = DecisionJournal(self.journal_path, create=True)
        self.broker = StubBroker()
        self.epoch = {"current": 7}
        self.schema = SCHEMA
        self.alerts = []
        self.state = AccountState(read_at=NOW - timedelta(seconds=5), positions={},
                                  prices={t: D("10") for t in TICKERS}, cash=D("1000"))
        self.guard = self.make_guard()

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def make_guard(self, journal=None):
        return AlphaGuard(
            broker=self.broker, journal=journal or self.journal, mandate_path=self.mandate,
            approved_mandate_sha256=self.approved, kill_flag_path=self.flag, held_epoch=7,
            current_epoch=lambda: self.epoch["current"], account_state=lambda: self.state,
            expected_schema_sha256=SCHEMA, current_schema_sha256=lambda: self.schema,
            audit_path=self.audit, alert=self.alerts.append, clock=lambda: NOW)

    def buy(self, symbol="AAA", qty="10", sig="s1", caller="alpha-bot", side="buy"):
        return self.guard.place(caller=caller, symbol=symbol, side=side, quantity=qty,
                                signal_version=sig)

    def refused(self, **kw):
        with self.assertRaises(Refused) as ctx:
            self.buy(**kw)
        return str(ctx.exception)


class Guard(Harness):
    def test_positive_control_market_order_with_ref_id(self):
        self.buy()
        (args,) = self.broker.placements()
        self.assertEqual(args["type"], "market")
        self.assertEqual(args["ref_id"],
                         ref_id_for(decision_id("2026-11-04", "AAA", "buy", "s1")))

    def test_rt_main_agent_order_refused(self):
        self.refused(caller="main-agent")
        self.assertEqual(self.broker.calls, [])

    def test_rt_off_list_ticker_refused(self):
        self.refused(symbol="ZZZ")

    def test_rt_stale_epoch_refused(self):
        self.epoch["current"] = 8
        self.refused()
        self.epoch["current"] = None
        self.refused()
        self.assertEqual(self.broker.calls, [])

    def test_unapproved_or_unreadable_mandate_refused(self):
        self.mandate.write_bytes(MANDATE.replace(b'"250"', b'"2500"', 1))
        self.assertIn("approved", self.refused())
        self.mandate.unlink()
        self.refused()

    def test_state_must_be_read_within_60s(self):
        for read_at in (NOW - timedelta(seconds=61), NOW + timedelta(seconds=30),
                        NOW.replace(tzinfo=None)):
            self.state = AccountState(read_at, {}, self.state.prices, D("250"))
            with self.subTest(read_at=read_at):
                self.refused()

    def test_ceiling_includes_price_buffer(self):
        self.buy(qty="24")            # 24 * 10 * 1.02 = 244.8
        self.assertIn("ceiling", self.refused(symbol="BBB", qty="1", sig="s2"))

    def test_insufficient_buying_power(self):
        self.state = AccountState(self.state.read_at, {"AAA": D("5")}, self.state.prices,
                                  D("50"))
        self.refused(symbol="BBB", qty="5")   # 51 > 50 cash

    def test_loss_stop_blocks_all_orders(self):
        self.state = AccountState(self.state.read_at, {"AAA": D("10")}, self.state.prices,
                                  D("100"))           # value 200 == floor
        self.refused(side="sell", qty="1")
        self.refused(symbol="BBB", qty="1")

    def test_sells_cannot_exceed_holdings_net_of_open_sells(self):
        self.state = AccountState(self.state.read_at, {"AAA": D("5")}, self.state.prices,
                                  D("200"), open_orders=(OpenOrder(None, "AAA", "sell", D("3")),))
        self.refused(side="sell", qty="3")
        self.buy(side="sell", qty="2")

    def test_unfilled_orders_reserved_across_restart(self):
        self.buy(qty="12", sig="s1")                     # 122.4 reserved, broker unseen
        self.journal.close()
        self.journal = DecisionJournal(self.journal_path)
        self.guard = self.make_guard()
        self.buy(symbol="BBB", qty="12", sig="s1")       # 244.8
        self.assertIn("ceiling", self.refused(symbol="CCC", qty="1", sig="s1"))
        self.assertEqual(len(self.broker.placements()), 2)

    def test_rt_timeout_after_remote_fill_not_duplicated(self):
        self.broker.fail_after_submit = True
        with self.assertRaises(TimeoutError):
            self.buy()
        self.broker.fail_after_submit = False
        self.assertIn("unresolved", self.refused())
        ref = ref_id_for(decision_id("2026-11-04", "AAA", "buy", "s1"))
        self.state = AccountState(self.state.read_at, {"AAA": D("10")}, self.state.prices,
                                  D("150"), known_ref_ids=frozenset({ref}))
        self.assertIn("already placed", self.refused())
        self.assertEqual(len(self.broker._by_ref), 1)

    def test_rt_retried_order_carries_same_ref_id(self):
        self.broker.fail_before_submit = True
        with self.assertRaises(ConnectionError):
            self.buy()
        did = decision_id("2026-11-04", "AAA", "buy", "s1")
        self.refused()                                   # unknown: no blind retry
        self.journal.mark_failed(did, NOW)               # broker confirmed no order
        self.broker.fail_before_submit = False
        self.buy()
        refs = [a["ref_id"] for a in self.broker.placements()]
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0], refs[1])

    def test_kill_flag_on_or_garbage_refuses(self):
        kill_switch.write_flag(self.flag, "on")
        self.refused()
        self.flag.write_text("maybe")
        self.refused()

    def test_rt_unreadable_flag_starts_paused(self):
        self.flag.unlink()
        guard = self.make_guard()
        self.assertTrue(self.alerts and "paused" in self.alerts[0])
        kill_switch.write_flag(self.flag, "off")
        with self.assertRaises(Refused):
            guard.place(caller="alpha-bot", symbol="AAA", side="buy", quantity="1",
                        signal_version="s1")

    def test_rt_schema_drift_pauses(self):
        self.schema = "schema-v2"
        guard = self.make_guard()
        self.assertEqual(guard.paused_reason,
                         "broker tool schema changed or unreadable at startup")
        with self.assertRaises(Refused):
            guard.place(caller="alpha-bot", symbol="AAA", side="buy", quantity="1",
                        signal_version="s1")

    def test_audit_failure_means_no_order(self):
        self.audit.mkdir()
        with self.assertRaises(OSError):
            self.buy()
        self.assertEqual(self.broker.calls, [])

    def test_malformed_inputs_refused(self):
        for kw in (dict(qty="0"), dict(qty="NaN"), dict(qty="-1"), dict(side="short"),
                   dict(sig=""), dict(sig="a:b")):
            with self.subTest(kw=kw):
                self.refused(**kw)


class KillSwitch(Harness):
    def test_rt_kill_cancels_only_bot_orders_then_blocks(self):
        self.buy()
        self.broker._open.append({"order_id": "manual-1", "ref_id": "josh-manual"})
        cancelled = kill_switch.engage(self.guard, self.broker, self.journal)
        self.assertEqual(cancelled, ["o1"])
        self.assertEqual([o["order_id"] for o in self.broker.open_orders()], ["manual-1"])
        self.assertIn("kill flag on", self.refused(symbol="BBB", sig="s2"))

    def test_kill_attempts_every_cancel(self):
        self.buy(sig="s1")
        self.buy(symbol="BBB", sig="s1")
        self.broker.fail_cancel = {"o1"}
        with self.assertRaises(kill_switch.KillSwitchIncomplete) as ctx:
            kill_switch.engage(self.guard, self.broker, self.journal)
        self.assertEqual((ctx.exception.cancelled, ctx.exception.failed), (["o2"], ["o1"]))

    def test_rt_kill_mid_run_waits_for_in_flight_order_then_cancels_it(self):
        entered, release = threading.Event(), threading.Event()

        def hold():
            entered.set()
            release.wait(5)
        self.broker.on_place = hold
        placer = threading.Thread(target=self.buy)
        placer.start()
        self.assertTrue(entered.wait(5))
        result = {}
        killer = threading.Thread(
            target=lambda: result.setdefault(
                "c", kill_switch.engage(self.guard, self.broker, self.journal)))
        killer.start()
        time.sleep(0.1)
        self.assertTrue(killer.is_alive())          # blocked behind the in-flight order
        self.assertEqual(self.flag.read_text().strip(), "off")
        release.set()
        placer.join(5)
        killer.join(5)
        self.assertEqual(result["c"], ["o1"])       # the in-flight order was swept
        self.assertEqual(self.broker.open_orders(), [])


if __name__ == "__main__":
    unittest.main()
