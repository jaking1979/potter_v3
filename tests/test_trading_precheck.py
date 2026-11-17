"""Tests for overlay/tools/trading_precheck.py. Synthetic mandates only; no brokerage, no network."""
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay/tools"))
import trading_precheck as tp  # noqa: E402

TZ = ZoneInfo("America/New_York")
TUE = date(2030, 3, 5)                                             # a Tuesday
NOW = datetime(2030, 3, 5, 22, 0, tzinfo=timezone.utc)             # 17:00 that Tuesday in New York
FIELDS = {"status": "approved", "approved_by": "the owner", "approved_on": "2030-03-01",
          "window_start": "2030-03-04", "window_end": "2030-03-29", "account": "test-cash",
          "capital_limit": "100", "max_order_value": "40", "max_orders_per_run": "3", "instruments": "etf, equity",
          "universe": "AAA, BBB, CCC", "order_approval": "autonomous", "strategy": "trading/loop/INSTRUCTIONS.md"}


def mandate_text(**changes):
    f = {**FIELDS, **changes}
    return "---\n# synthetic\n" + "".join(f"{k}: {v}\n" for k, v in f.items() if v is not None) + "---\n# Prose\nwords\n"


def ev(event, ts="2030-03-04T22:00:00+00:00", **kw):
    return {"ts": ts, "event": event, **kw}


class Mandate(unittest.TestCase):
    def load(self, **changes):
        return tp.validate_mandate(tp.parse_mandate(mandate_text(**changes)))

    def refused(self, needle, **changes):
        with self.assertRaises(tp.Refuse) as cm:
            m = self.load(**changes)
            tp.check_authority(m, TUE)
        self.assertIn(needle, str(cm.exception))

    def test_a_complete_mandate_parses_into_typed_values(self):
        m = self.load()
        self.assertEqual(m["window_end"], date(2030, 3, 29))
        self.assertEqual((m["capital_limit"], m["max_order_value"], m["max_orders_per_run"]), (100.0, 40.0, 3))
        self.assertEqual((m["universe"], m["instruments"]), (["AAA", "BBB", "CCC"], ["etf", "equity"]))
        tp.check_authority(m, TUE)

    def test_every_required_field_is_required(self):
        for k in tp.REQUIRED:
            with self.subTest(field=k):
                self.refused(k, **{k: None})
                self.refused(k, **{k: ""})

    def test_malformed_files_are_refused(self):
        cases = {"": "no field block", "status: approved\n": "no field block",
                 "---\nstatus: approved\n": "not closed", "---\njust words\n---\n": "not 'key: value'",
                 "---\nstatus: approved\nstatus: draft\n---\n": "appears twice"}
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(tp.Refuse) as cm:
                    tp.parse_mandate(text)
                self.assertIn(needle, str(cm.exception))

    def test_bad_values_are_refused(self):
        self.refused("not approved", status="draft")
        self.refused("not approved", status="suspended")
        self.refused("not a date", window_end="end of March")
        self.refused("ends before it starts", window_end="2030-03-01")
        self.refused("plain number", capital_limit="USD 100")
        self.refused("plain number", max_order_value="1e3")
        self.refused("greater than zero", capital_limit="0")
        self.refused("larger than capital_limit", max_order_value="500")
        self.refused("whole number", max_orders_per_run="0")
        self.refused("whole number", max_orders_per_run="2.5")
        self.refused("unknown instrument", instruments="etf, futures")
        self.refused("not a ticker symbol", universe="AAA, all of them")
        self.refused("per_order or autonomous", order_approval="whatever")
        self.refused("not an account number", account="acct 12345")
        self.refused("is empty", universe=" , ")

    def test_the_date_window_is_enforced(self):
        m = self.load()
        tp.check_authority(m, date(2030, 3, 4))                     # first day
        tp.check_authority(m, date(2030, 3, 29))                    # last day
        for day, needle in ((date(2030, 3, 3), "has not started"), (date(2030, 3, 30), "expired on 2030-03-29")):
            with self.assertRaises(tp.Refuse) as cm:
                tp.check_authority(m, day)
            self.assertIn(needle, str(cm.exception))
        self.refused("approval date is in the future", approved_on="2030-03-06", window_start="2030-03-01")

    def test_the_shipped_template_can_never_authorise_a_run(self):
        text = (ROOT / "overlay/workspace/skills/trading-run/MANDATE.template.md").read_text()
        fields = tp.parse_mandate(text)
        self.assertEqual(set(tp.REQUIRED) - set(fields), set())    # the template shows every field...
        with self.assertRaises(tp.Refuse):                         # ...and is refused as it stands
            tp.validate_mandate(fields)
        fields.update({k: v for k, v in FIELDS.items() if k not in ("status", "order_approval")})
        with self.assertRaises(tp.Refuse) as cm:                   # filled in but still a draft
            tp.check_authority(tp.validate_mandate(fields), TUE)
        self.assertIn("not approved", str(cm.exception))
        self.assertEqual(fields["order_approval"], "per_order")    # the safe default
        self.assertNotRegex(text, r"\d{3,}")                       # no amounts, no numbers of any account


class MarketDay(unittest.TestCase):
    def test_weekdays_pass_weekends_and_listed_holidays_do_not(self):
        for d in range(4, 9):
            tp.check_market_day(date(2030, 3, d))
        for d in (date(2030, 3, 9), date(2030, 3, 10)):
            with self.assertRaises(tp.Refuse):
                tp.check_market_day(d)
        with self.assertRaises(tp.Refuse) as cm:
            tp.check_market_day(TUE, {TUE})
        self.assertIn("holiday", str(cm.exception))


class RunLog(unittest.TestCase):
    def problem(self, events, today=TUE, run=None):
        return tp.log_problem(events, today, TZ, run)

    def test_clean_histories_allow_a_run(self):
        ok_run = [ev("run_start", run_id="r1"), ev("order_intent", run_id="r1", ref="r1-1"),
                  ev("order_result", ref="r1-1", status="placed"), ev("run_end", run_id="r1", status="ok")]
        for events in ([], ok_run, [ev("precheck_failed", reason="weekend")] * 3):
            self.assertIsNone(self.problem(events))

    def test_each_unresolved_condition_blocks_the_next_run(self):
        cases = {
            "unresolved error": [ev("error", detail="auth failed")],
            "unresolved error in": [ev("run_start", run_id="r1"), ev("run_end", run_id="r1", status="error")],
            "no recorded result": [ev("run_start", run_id="r1"), ev("order_intent", run_id="r1", ref="r1-1"),
                                   ev("run_end", run_id="r1", status="ok")],
            "unknown state": [ev("run_start", run_id="r1"), ev("order_intent", run_id="r1", ref="r1-1"),
                              ev("order_result", ref="r1-1", status="unknown"), ev("run_end", run_id="r1", status="ok")],
            "never finished": [ev("run_start", run_id="r1")],
            "unreadable line": [ev("run_start", run_id="r1"), None],
        }
        for needle, events in cases.items():
            with self.subTest(case=needle):
                self.assertIn(needle, self.problem(events))
                self.assertIsNone(self.problem(events + [ev("resolved", note="owner checked the account")]))

    def test_a_later_result_settles_an_unknown_order(self):
        events = [ev("run_start", run_id="r1"), ev("order_intent", run_id="r1", ref="r1-1"),
                  ev("order_result", ref="r1-1", status="unknown"), ev("order_result", ref="r1-1", status="filled"),
                  ev("run_end", run_id="r1", status="ok")]
        self.assertIsNone(self.problem(events))

    def test_only_one_completed_run_per_local_day(self):
        done = [ev("run_start", ts="2030-03-05T21:00:00+00:00", run_id="r1"),
                ev("run_end", ts="2030-03-05T21:05:00+00:00", run_id="r1", status="ok")]
        self.assertIn("already completed today", self.problem(done))
        self.assertIsNone(self.problem(done, today=date(2030, 3, 6)))
        late = [ev("run_start", ts="2030-03-05T03:00:00+00:00", run_id="r0"),      # 22:00 on the 4th, local
                ev("run_end", ts="2030-03-05T03:01:00Z", run_id="r0", status="ok")]
        self.assertIsNone(self.problem(late))

    def test_mid_run_the_open_run_must_be_this_one_and_its_orders_settled(self):
        base = [ev("run_start", run_id="r2")]
        self.assertIsNone(self.problem(base, run="r2"))
        self.assertIn("not the one open", self.problem(base, run="r9"))
        self.assertIn("not the one open", self.problem([], run="r2"))
        self.assertIn("no recorded result", self.problem(base + [ev("order_intent", run_id="r2", ref="r2-1")], run="r2"))


class Orders(unittest.TestCase):
    M = tp.validate_mandate(tp.parse_mandate(mandate_text()))

    def refused(self, needle, orders, already=(), m=None):
        with self.assertRaises(tp.Refuse) as cm:
            tp.check_orders(m or self.M, orders, list(already))
        self.assertIn(needle, str(cm.exception))

    @staticmethod
    def o(symbol="AAA", side="buy", instrument="etf", value=30, **kw):
        return {"symbol": symbol, "side": side, "instrument": instrument, "value": value, **kw}

    def test_orders_inside_the_mandate_pass(self):
        tp.check_orders(self.M, [self.o(), self.o("BBB", value=40), self.o("CCC", "sell", value=None)], [])

    def test_anything_outside_the_mandate_is_refused(self):
        self.refused("no proposed orders", [])
        self.refused("no proposed orders", {"symbol": "AAA"})
        self.refused("not an object", ["AAA"])
        self.refused("symbol outside the mandate", [self.o("ZZZ")])
        self.refused("symbol outside the mandate", [self.o("aaa")])
        self.refused("instrument type outside the mandate", [self.o(instrument="option")])
        self.refused("no valid side", [self.o(side="short")])
        self.refused("larger than the mandate's limit", [self.o(value=40.01)])
        self.refused("buy without a value", [self.o(value=None)])
        self.refused("not a positive number", [self.o(value=-5)])
        self.refused("not a positive number", [self.o(value="30")])
        self.refused("not a positive number", [self.o(value=True)])
        self.refused("more orders than the mandate allows", [self.o("AAA"), self.o("BBB"), self.o("CCC"), self.o("AAA", "sell")])
        self.refused("capital limit", [self.o("AAA", value=40), self.o("BBB", value=40), self.o("CCC", value=21)])
        self.refused("repeats an order", [self.o(), self.o()])

    def test_earlier_orders_in_the_same_run_count(self):
        earlier = [{"symbol": "AAA", "side": "buy", "value": 40}, {"symbol": "BBB", "side": "buy", "value": 40}]
        tp.check_orders(self.M, [self.o("CCC", value=20)], earlier)
        self.refused("capital limit", [self.o("CCC", value=21)], earlier)
        self.refused("repeats an order", [self.o("AAA", value=5)], earlier)
        self.refused("more orders", [self.o("CCC", value=1), self.o("CCC", "sell")], earlier)

    def test_per_order_mandates_need_the_owner_on_each_order(self):
        m = tp.validate_mandate(tp.parse_mandate(mandate_text(order_approval="per_order")))
        self.refused("owner's approval", [self.o()], m=m)
        self.refused("owner's approval", [self.o(owner_approved="yes")], m=m)
        tp.check_orders(m, [self.o(owner_approved=True)], [])


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.mandate, self.stop, self.log, self.orders = d / "MANDATE.md", d / "STOP", d / "logs/run.jsonl", d / "orders.json"
        self.mandate.write_text(mandate_text())

    def run_cli(self, cmd, *extra, now=NOW):
        out = io.StringIO()
        args = [cmd]
        if cmd in ("check", "orders"):
            args += ["--mandate", str(self.mandate), "--kill-switch", str(self.stop)]
        rc = tp.main(args + ["--log", str(self.log), *extra], now=now, out=out)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1, "exactly one line of output")
        return rc, text.strip()

    def events(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_all_clear_exits_0_and_writes_nothing_unless_asked(self):
        rc, text = self.run_cli("check")
        self.assertEqual(rc, 0)
        self.assertTrue(text.startswith("OK: may proceed run_id=20300305T220000Z order_approval=autonomous"), text)
        self.assertFalse(self.log.exists())

    def test_each_failed_check_exits_1_with_one_line(self):
        def missing():
            self.mandate.unlink()

        def expired():
            self.mandate.write_text(mandate_text(window_end="2030-03-04"))

        def unparsable():
            self.mandate.write_text("# Mandate\nTrade whatever looks good.\n")

        def binary():
            self.mandate.write_bytes(b"\xff\xfe\x00junk")

        def stopped():
            self.stop.write_text("owner said stop")

        def errored():
            self.log.parent.mkdir()
            self.log.write_text(json.dumps(ev("error", detail="auth failed")) + "\n")

        def garbled():
            self.log.parent.mkdir()
            self.log.write_text('{"event": "run_start", "run_id": "r1"\n')

        cases = [(missing, [], "mandate file is missing"), (expired, [], "mandate expired on 2030-03-04"),
                 (unparsable, [], "no field block"), (binary, [], "cannot be read"),
                 (stopped, [], "kill switch is on"), (errored, [], "unresolved error"),
                 (garbled, [], "unreadable line"), (lambda: None, ["--today", "2030-03-09"], "not a market weekday"),
                 (lambda: None, ["--tz", "Mars/Olympus"], "not valid"),
                 (lambda: None, ["--holidays", str(self.mandate)], "holiday file")]
        for arrange, extra, needle in cases:
            with self.subTest(case=needle):
                self.setUp()
                arrange()
                rc, text = self.run_cli("check", *extra)
                self.assertEqual(rc, 1)
                self.assertTrue(text.startswith("REFUSE: "), text)
                self.assertIn(needle, text)

    def test_weekend_is_judged_in_the_market_time_zone(self):
        fri_evening_utc_sat = datetime(2030, 3, 9, 1, 0, tzinfo=timezone.utc)      # still Friday in New York
        self.assertEqual(self.run_cli("check", now=fri_evening_utc_sat)[0], 0)
        self.assertEqual(self.run_cli("check", now=datetime(2030, 3, 9, 15, 0, tzinfo=timezone.utc))[0], 1)

    def test_holiday_file(self):
        h = self.mandate.with_name("holidays.txt")
        h.write_text("# closed\n2030-03-05  # test day\n")
        rc, text = self.run_cli("check", "--holidays", str(h))
        self.assertEqual(rc, 1)
        self.assertIn("holiday", text)
        self.assertEqual(self.run_cli("check", "--holidays", str(h), "--today", "2030-03-06")[0], 0)

    def test_a_dangling_stop_link_still_stops(self):
        self.stop.symlink_to(self.stop.with_name("nowhere"))
        self.assertEqual(self.run_cli("check")[0], 1)

    def test_recorded_refusal_leaves_a_log_entry_and_does_not_block_tomorrow(self):
        rc, _ = self.run_cli("check", "--record", "--today", "2030-03-09")
        self.assertEqual(rc, 1)
        self.assertEqual([(e["event"], e["reason"]) for e in self.events()],
                         [("precheck_failed", "2030-03-09 is not a market weekday")])
        self.assertEqual(self.run_cli("check")[0], 0)

    def test_a_whole_run_is_logged_before_and_after_each_order(self):
        rc, text = self.run_cli("check", "--record")
        run_id = text.split("run_id=")[1].split()[0]
        self.orders.write_text(json.dumps([{"symbol": "AAA", "side": "sell", "instrument": "etf", "quantity": 1.5},
                                           {"symbol": "BBB", "side": "buy", "instrument": "etf", "value": 25}]))
        rc, text = self.run_cli("orders", "--run-id", run_id, "--orders", str(self.orders), "--record")
        self.assertEqual((rc, text), (0, f"OK: 2 order(s) inside the mandate; refs {run_id}-1 {run_id}-2"))
        # the intents are on disk before any result exists, and a second batch is refused until they are settled
        self.assertEqual([e["event"] for e in self.events()], ["run_start", "order_intent", "order_intent"])
        self.orders.write_text(json.dumps([{"symbol": "CCC", "side": "buy", "instrument": "etf", "value": 5}]))
        rc, text = self.run_cli("orders", "--run-id", run_id, "--orders", str(self.orders), "--record")
        self.assertEqual(rc, 1)
        self.assertIn("no recorded result", text)
        self.assertEqual(self.run_cli("result", "--ref", f"{run_id}-1", "--status", "placed", "--broker-order-id", "x1")[0], 0)
        self.assertEqual(self.run_cli("result", "--ref", f"{run_id}-2", "--status", "rejected")[0], 0)
        self.assertEqual(self.run_cli("result", "--ref", "made-up", "--status", "placed")[0], 1)
        rc, text = self.run_cli("orders", "--run-id", run_id, "--orders", str(self.orders), "--record")
        self.assertEqual((rc, text), (0, f"OK: 1 order(s) inside the mandate; refs {run_id}-3"))
        self.run_cli("result", "--ref", f"{run_id}-3", "--status", "filled")
        self.assertEqual(self.run_cli("end", "--run-id", run_id, "--status", "ok")[0], 0)
        self.assertEqual([e["event"] for e in self.events()],
                         ["run_start", "order_intent", "order_intent", "order_result", "order_result",
                          "order_intent", "order_result", "run_end"])
        self.assertTrue(all(e["ts"].endswith("+00:00") for e in self.events()))
        # same day: no second run. Next market day: fine.
        rc, text = self.run_cli("check", "--record")
        self.assertIn("already completed today", text)
        self.assertEqual(self.run_cli("check", now=datetime(2030, 3, 6, 22, 0, tzinfo=timezone.utc))[0], 0)

    def test_orders_gate_refuses_without_logging_anything(self):
        _, text = self.run_cli("check", "--record")
        run_id = text.split("run_id=")[1].split()[0]
        for content, needle in (('[{"symbol": "ZZZ", "side": "buy", "instrument": "etf", "value": 5}]', "outside the mandate"),
                                ("not json", "not JSON")):
            self.orders.write_text(content)
            rc, text = self.run_cli("orders", "--run-id", run_id, "--orders", str(self.orders), "--record")
            self.assertEqual(rc, 1)
            self.assertIn(needle, text)
        self.assertEqual(self.run_cli("orders", "--run-id", "other", "--orders", str(self.orders))[0], 1)
        self.stop.write_text("stop")                                # the switch also stops a run in progress
        self.orders.write_text('[{"symbol": "AAA", "side": "buy", "instrument": "etf", "value": 5}]')
        rc, text = self.run_cli("orders", "--run-id", run_id, "--orders", str(self.orders), "--record")
        self.assertIn("kill switch", text)
        self.assertEqual([e["event"] for e in self.events()], ["run_start"])

    def test_a_crashed_or_failed_run_blocks_until_the_owner_resolves_it(self):
        _, text = self.run_cli("check", "--record")
        run_id = text.split("run_id=")[1].split()[0]
        tomorrow = datetime(2030, 3, 6, 22, 0, tzinfo=timezone.utc)
        rc, text = self.run_cli("check", "--record", now=tomorrow)
        self.assertEqual(rc, 1)
        self.assertIn("never finished", text)
        self.run_cli("end", "--run-id", run_id, "--status", "error", "--note", "brokerage unreachable")
        self.assertIn("unresolved error", self.run_cli("check", now=tomorrow)[1])
        self.assertEqual(self.run_cli("resolve", "--note", "owner checked: no open orders")[0], 0)
        self.assertEqual(self.run_cli("check", now=tomorrow)[0], 0)

    def test_if_the_log_cannot_be_written_nothing_may_proceed(self):
        self.log.parent.write_text("a file where the log directory should be")
        rc, text = self.run_cli("check", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("run log", text)

    def test_bad_arguments_exit_2(self):
        for argv in ([], ["check"], ["check", "--mandate", "x"], ["result", "--log", "x", "--ref", "r", "--status", "great"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm:
                import contextlib
                with contextlib.redirect_stderr(io.StringIO()):
                    tp.main(argv)
            self.assertEqual(cm.exception.code, 2)


class Hygiene(unittest.TestCase):
    FILES = [ROOT / "overlay/tools/trading_precheck.py", ROOT / "tests/test_trading_precheck.py",
             ROOT / "overlay/workspace/skills/trading-run/SKILL.md",
             ROOT / "overlay/workspace/skills/trading-run/MANDATE.template.md"]

    def test_nothing_personal_or_named_is_shipped(self):
        banned = ["/ho" + "me/", "expand" + "user"] + [a + b for a, b in (("alpha", " bot"), ("alpha", "bot"), ("alpha", "_bot"),
                                                                ("robin", "hood"), ("opti", "plex"), ("brook", "lyn"))]
        patterns = [re.escape(w) for w in banned] + [r"\d{9,}", r"\$\s?\d"]     # words, long ids, dollar amounts
        for p in self.FILES:
            low = p.read_text().lower()
            for pat in patterns:
                hit = re.search(pat, low)
                with self.subTest(file=p.name, pattern=pat):
                    self.assertIsNone(hit and hit.group(0))

    def test_the_tool_has_no_way_to_reach_a_network_or_a_brokerage(self):
        import ast
        allowed = {"__future__", "argparse", "json", "os", "re", "sys", "datetime", "pathlib", "zoneinfo"}
        tree = ast.parse(self.FILES[0].read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                self.assertIn(n.split(".")[0], allowed)


if __name__ == "__main__":
    unittest.main()
