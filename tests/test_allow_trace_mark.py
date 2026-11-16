"""Dispatch mark: an approval-bound effect cannot be dispatched until its allow decision is durably traced."""
import itertools
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_policy_engine as pe  # noqa: E402
from policy import ALLOW, DENY, RECONCILE  # noqa: E402
from runtime.approval_ledger import JournalApprovalLedger  # noqa: E402
from runtime.db import Database, SCHEMA_VERSION  # noqa: E402
from runtime.effects import EffectJournal, EffectNotAudited, idempotency_key  # noqa: E402

CONN = "test.conn"


class Sink:
    def __init__(self):
        self.rows, self.fail, self.fail_reasons = [], False, set()

    def __call__(self, record):
        if self.fail or record.get("reason_code") in self.fail_reasons:
            raise OSError("trace store unavailable")
        self.rows.append(dict(record))


class External:
    def __init__(self):
        self.calls = 0

    def perform(self, effect):
        self.calls += 1
        return {"ok": self.calls}

    def reconcile(self, effect):
        return "failed"


class World:
    def __init__(self, tmp, **journal_kw):
        self.db = Database(os.path.join(tmp, "j.sqlite"))
        self.ledger = JournalApprovalLedger(self.db)
        self.journal = EffectJournal(self.db, **journal_kw)
        self.sink = Sink()
        ids = itertools.count(1)
        self.engine = pe.engine(ledger=self.ledger, trace_hook=self.sink,
                                new_id=lambda prefix: f"{prefix}-{next(ids):032x}")
        self.ext = External()

    def decide(self, retry_intent=None):
        return self.engine.decide(pe.req(pe.SEND, approval=pe.record(pe.SEND), retry_intent=retry_intent))

    def effect(self):
        [eff] = self.journal.unresolved()
        return eff

    def dispatch(self, eff=None):
        eff = eff or self.effect()
        return self.journal.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                    owner="w", connector=CONN, now=pe.NOW)


class DispatchMark(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_traced_allow_marks_the_effect_and_dispatch_proceeds(self):
        d = self.w.decide()
        self.assertEqual((d.outcome, d.reason_code), (ALLOW, "ok_approved"))
        self.assertIsNotNone(self.w.effect().allow_traced_at)
        self.w.dispatch()
        self.assertEqual(self.w.ext.calls, 1)

    def test_trace_outage_leaves_the_intent_unmarked_and_undispatchable(self):
        self.w.sink.fail = True
        d = self.w.decide()
        self.assertEqual((d.outcome, d.reason_code), (DENY, "trace_error"))
        eff = self.w.effect()
        self.assertIsNone(eff.allow_traced_at)
        with self.assertRaises(EffectNotAudited):
            self.w.dispatch(eff)
        self.assertEqual(self.w.ext.calls, 0)

    def test_reconcile_after_recovery_writes_a_catch_up_allow_then_marks(self):
        self.w.sink.fail = True
        self.w.decide()
        self.w.sink.fail = False
        eff = self.w.effect()
        d = self.w.decide(retry_intent=eff.intent())
        self.assertEqual((d.outcome, d.reason_code), (RECONCILE, "reconcile_pending_intent"))
        self.assertEqual([(r["outcome"], r["reason_code"]) for r in self.w.sink.rows],
                         [("allow", "ok_approved_catch_up"), ("reconcile", "reconcile_pending_intent")])
        catch = self.w.sink.rows[0]
        self.assertEqual((catch["action_hash"], catch["effect_id"], catch["capability"]),
                         (pe.SEND.hash(), eff.effect_id, "gmail.send"))
        self.assertNotEqual(catch["decision_id"], self.w.sink.rows[1]["decision_id"])
        self.assertIsNotNone(self.w.effect().allow_traced_at)
        self.w.dispatch()
        self.assertEqual(self.w.ext.calls, 1)

    def test_catch_up_trace_failure_fails_closed_and_stays_unmarked(self):
        self.w.sink.fail = True
        self.w.decide()
        eff = self.w.effect()
        d = self.w.decide(retry_intent=eff.intent())          # sink still down
        self.assertEqual((d.outcome, d.reason_code), (DENY, "trace_error"))
        self.assertIsNone(self.w.effect().allow_traced_at)
        with self.assertRaises(EffectNotAudited):
            self.w.dispatch()
        self.assertEqual(self.w.sink.rows, [])

    def test_a_failing_catch_up_row_alone_blocks_the_reconcile(self):
        self.w.sink.fail = True
        self.w.decide()
        self.w.sink.fail = False
        self.w.sink.fail_reasons = {"ok_approved_catch_up"}      # only the catch-up row fails
        d = self.w.decide(retry_intent=self.w.effect().intent())
        self.assertEqual((d.outcome, d.reason_code), (DENY, "trace_error"))
        self.assertEqual(self.w.sink.rows, [])                    # no reconcile row either
        self.assertIsNone(self.w.effect().allow_traced_at)
        with self.assertRaises(EffectNotAudited):
            self.w.dispatch()

    def test_mark_refuses_an_effect_that_is_not_approval_bound(self):
        eff = self.w.journal.record_intent("3f0c8a52-6b1e-5c7d-9a2e-1f4b6c8d0e21", "step-1", pe.action_for("notes.write"))
        self.assertFalse(self.w.ledger.mark_allow_traced(eff.intent()))
        self.assertFalse(self.w.ledger.allow_traced(eff.intent()))

    def test_mark_failure_after_a_traced_allow_fails_closed_then_recovers(self):
        real = self.w.ledger.mark_allow_traced
        self.w.ledger.mark_allow_traced = lambda intent: False
        d = self.w.decide()
        self.assertEqual((d.outcome, d.reason_code), (DENY, "trace_error"))
        self.assertIsNone(self.w.effect().allow_traced_at)
        self.w.ledger.mark_allow_traced = real
        d = self.w.decide(retry_intent=self.w.effect().intent())
        self.assertEqual(d.outcome, RECONCILE)
        self.assertIsNotNone(self.w.effect().allow_traced_at)

    def test_a_marked_effect_gets_no_second_catch_up(self):
        self.w.decide()
        rows = len(self.w.sink.rows)
        d = self.w.decide(retry_intent=self.w.effect().intent())
        self.assertEqual(d.outcome, RECONCILE)
        self.assertEqual([r["reason_code"] for r in self.w.sink.rows[rows:]], ["reconcile_pending_intent"])

    def test_mark_is_idempotent_and_first_time_wins(self):
        self.w.decide()
        first = self.w.effect().allow_traced_at
        self.assertTrue(self.w.ledger.mark_allow_traced(self.w.effect().intent()))
        self.assertEqual(self.w.effect().allow_traced_at, first)

    def test_unapproved_effects_are_not_gated(self):
        eff = self.w.journal.record_intent("3f0c8a52-6b1e-5c7d-9a2e-1f4b6c8d0e21", "step-1", pe.action_for("notes.write"))
        self.assertIsNone(eff.approval_id)
        self.w.dispatch(eff)
        self.assertEqual(self.w.ext.calls, 1)

    def test_the_gate_is_on_by_default_and_the_bypass_is_explicit(self):
        self.assertTrue(EffectJournal(self.w.db)._require_allow_trace)
        with self.assertRaises(TypeError):
            EffectJournal(self.w.db, require_allow_trace="no")
        open_journal = EffectJournal(self.w.db, require_allow_trace=False)
        self.w.sink.fail = True
        self.w.decide()
        eff = self.w.effect()
        open_journal.execute(eff.idempotency_key, self.w.ext.perform, self.w.ext.reconcile,
                             owner="w", connector=CONN, now=pe.NOW)
        self.assertEqual(self.w.ext.calls, 1)

    def test_a_ledger_without_the_mark_keeps_the_reference_behaviour(self):
        from policy._contracts import approval as c0
        eng = pe.engine(trace_hook=Sink(), ledger=c0.MemoryApprovalLedger())
        self.assertEqual(eng.decide(pe.req(pe.SEND, approval=pe.record(pe.SEND))).outcome, ALLOW)


class Migration(unittest.TestCase):
    def test_schema_version_and_upgrade_from_v3(self):
        self.assertEqual(SCHEMA_VERSION, "c2.v4")
        with tempfile.TemporaryDirectory() as tmp:
            w = World(tmp)
            w.decide()
            path = w.db.path
            c = sqlite3.connect(path)
            c.execute("UPDATE meta SET value='c2.v3' WHERE key='schema'")
            c.execute("ALTER TABLE effects DROP COLUMN allow_traced_at")
            c.commit()
            c.close()
            w2 = World(tmp)                                        # reopening migrates
            with w2.db.read() as c:
                self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], "c2.v4")
                self.assertIn("allow_traced_at", {r[1] for r in c.execute("PRAGMA table_info(effects)")})
            eff = w2.effect()
            self.assertIsNone(eff.allow_traced_at)                 # upgraded rows are unmarked
            with self.assertRaises(EffectNotAudited):
                w2.dispatch(eff)
            d = w2.decide(retry_intent=eff.intent())               # reconciliation repairs it
            self.assertEqual(d.outcome, RECONCILE)
            w2.dispatch()
            self.assertEqual(w2.ext.calls, 1)


if __name__ == "__main__":
    unittest.main()
