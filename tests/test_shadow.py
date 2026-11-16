"""Shadow-MVP runtime: effect gate, brokered model calls, cost log, turn loop."""
import os
import tempfile
import unittest

from model_broker.broker import Broker, Candidate, Caps
from shadow.costlog import CostLog
from shadow.gate import EffectRefused, MODEL, READ, REPLY_OWNER, ToolGate
from shadow.models import BrokeredModel, ModelRefused, ModelUnavailable, RATIFIED_CAPS
from shadow.turn import ContextPlan, Inbound, PassthroughGovernor, ShadowRuntime

OWNER = "1001"
DAY = "2026-11-27"


def cand(model="m-small", expected=1_000, worst=5_000, **kw):
    base = dict(model=model, provider="p", approved=True,
                validated_tasks=frozenset({"chat"}), privacy_classes=frozenset({"personal"}),
                expected_usd_micros=expected, worst_usd_micros=worst)
    base.update(kw)
    return Candidate(**base)


class World:
    """A wired shadow runtime with fake transports."""

    def __init__(self, tmp, *, caps=RATIFIED_CAPS, candidates=None, provider=None,
                 policy=lambda m: True, governor=None):
        self.audit, self.sent, self.provider_calls = [], [], []
        self.audit_down = False
        self.day = DAY
        self.gate = ToolGate(owner_chat_id=OWNER, audit=self._audit)
        self.provider = provider or (lambda **kw: {"text": "hi", "input_tokens": 10,
                                                   "output_tokens": 2, "cost_usd_micros": 1_200})
        self.gate.register("model.complete", MODEL, self._provider)
        self.gate.register("telegram.reply", REPLY_OWNER,
                           lambda chat_id, text: self.sent.append((chat_id, text)))
        self.gate.seal()
        self.broker = Broker.bootstrap(os.path.join(tmp, "broker.db"), caps)
        self.costlog = CostLog(os.path.join(tmp, "cost.jsonl"))
        self.model = BrokeredModel(broker=self.broker, candidates=candidates or [cand()],
                                   gate=self.gate, costlog=self.costlog,
                                   today=lambda: self.day, now=lambda: "2026-11-27T12:00:00Z")
        self.rt = ShadowRuntime(owner_chat_id=OWNER, gate=self.gate, model=self.model,
                                governor=governor or PassthroughGovernor(), policy=policy)

    def _audit(self, rec):
        if self.audit_down:
            raise OSError("sink down")
        self.audit.append(rec)

    def _provider(self, **kw):
        self.provider_calls.append(kw)
        return self.provider(**kw)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name


class Gate(Base):
    def gate(self):
        self.audit = []
        return ToolGate(owner_chat_id=OWNER, audit=self.audit.append)

    def test_effect_classes_other_than_the_three_cannot_register(self):
        g = self.gate()
        for bad in ("write", "send", "trade", "purchase", "", None, "READ", 1):
            with self.assertRaises(EffectRefused) as cm:
                g.register("t", bad, lambda: None)
            self.assertEqual(cm.exception.reason, "effect_class_not_allowed")
        self.assertEqual(g.tools(), ())

    def test_unknown_tool_is_refused_and_audited(self):
        g = self.gate(); g.seal()
        with self.assertRaises(EffectRefused) as cm:
            g.call("brokerage.place_order", symbol="X")
        self.assertEqual(cm.exception.reason, "unknown_tool")
        self.assertEqual(self.audit[-1]["outcome"], "refused:unknown_tool")
        self.assertNotIn("X", repr(self.audit))

    def test_no_registration_after_seal_and_no_calls_before_seal(self):
        g = self.gate()
        g.register("ha.state", READ, lambda: "ok")
        with self.assertRaises(EffectRefused) as cm:
            g.call("ha.state")
        self.assertEqual(cm.exception.reason, "gate_not_sealed")
        g.seal()
        self.assertEqual(g.call("ha.state"), "ok")
        with self.assertRaises(EffectRefused) as cm:
            g.register("late", READ, lambda: None)
        self.assertEqual(cm.exception.reason, "gate_sealed")

    def test_duplicate_and_malformed_names_refused(self):
        g = self.gate()
        g.register("a.b", READ, lambda: 1)
        for name, reason in (("a.b", "duplicate_tool"), ("", "invalid_tool_name"),
                             ("a b", "invalid_tool_name"), ("x" * 65, "invalid_tool_name"),
                             (None, "invalid_tool_name")):
            with self.assertRaises(EffectRefused) as cm:
                g.register(name, READ, lambda: 1)
            self.assertEqual(cm.exception.reason, reason)

    def test_reply_goes_only_to_the_owner_chat(self):
        sent = []
        g = self.gate()
        g.register("telegram.reply", REPLY_OWNER, lambda chat_id, text: sent.append(chat_id))
        g.seal()
        for other in ("2002", "", None, 1001, OWNER + " "):
            with self.assertRaises(EffectRefused) as cm:
                g.call("telegram.reply", chat_id=other, text="x")
            self.assertEqual(cm.exception.reason, "not_owner_chat")
        with self.assertRaises(EffectRefused):
            g.call("telegram.reply", text="x")
        self.assertEqual(sent, [])
        g.call("telegram.reply", chat_id=OWNER, text="x")
        self.assertEqual(sent, [OWNER])

    def test_audit_outage_means_the_tool_does_not_run(self):
        ran = []
        def down(rec):
            raise OSError("down")
        g = ToolGate(owner_chat_id=OWNER, audit=down)
        g.register("ha.state", READ, lambda: ran.append(1))
        g.seal()
        with self.assertRaises(EffectRefused) as cm:
            g.call("ha.state")
        self.assertEqual(cm.exception.reason, "audit_unavailable")
        self.assertEqual(ran, [])

    def test_audit_never_carries_arguments(self):
        g = self.gate()
        g.register("ha.state", READ, lambda entity: "on")
        g.seal()
        g.call("ha.state", entity="secret-entity")
        self.assertEqual(self.audit, [{"tool": "ha.state", "effect_class": "read",
                                       "outcome": "attempt"}])


class Costs(Base):
    def test_rejects_extra_text_fields_and_bad_values(self):
        log = CostLog(os.path.join(self.tmp, "c.jsonl"))
        good = {"ts": "t", "call_id": "c", "task_id": "k", "model": "m", "provider": "p",
                "state": "settled", "reserved_usd_micros": 1, "input_tokens": None,
                "output_tokens": 2, "actual_usd_micros": 1}
        log.append(good)
        for change in ({"prompt": "hello"}, {"state": "fine"}, {"reserved_usd_micros": -1},
                       {"input_tokens": True}, {"model": ""}):
            with self.assertRaises(ValueError):
                log.append({**good, **change})
        self.assertEqual(log.read(), [good])


class Model(Base):
    def test_success_settles_and_logs_actual_cost(self):
        w = World(self.tmp)
        r = w.model.complete(task_id="11111111-1111-4111-8111-111111111111", task_class="chat",
                             privacy_class="personal", messages=[])
        self.assertEqual((r.state, r.actual_usd_micros, r.text), ("settled", 1_200, "hi"))
        self.assertEqual(w.broker.status(r.call_id)[3:], (1_200, "settled"))
        rec = w.costlog.read()[-1]
        self.assertEqual((rec["state"], rec["reserved_usd_micros"], rec["actual_usd_micros"]),
                         ("settled", 5_000, 1_200))

    def test_request_ceiling_refuses_before_any_provider_call(self):
        w = World(self.tmp, candidates=[cand(worst=RATIFIED_CAPS.request + 1)])
        with self.assertRaises(ModelRefused) as cm:
            w.model.complete(task_id="11111111-1111-4111-8111-111111111111", task_class="chat",
                             privacy_class="personal", messages=[])
        self.assertEqual(cm.exception.reason, "budget_ceiling")
        self.assertEqual(w.provider_calls, [])
        self.assertEqual(w.costlog.read()[-1]["state"], "refused")

    def test_day_ceiling_holds_across_a_restart(self):
        caps = Caps(request=5_000, task=5_000, day=10_000, month=1_000_000)
        w = World(self.tmp, caps=caps, provider=lambda **kw: {"text": "x", "cost_usd_micros": 5_000})
        ids = ["11111111-1111-4111-8111-11111111111%d" % i for i in range(3)]
        for t in ids[:2]:
            w.model.complete(task_id=t, task_class="chat", privacy_class="personal", messages=[])
        # New process: reopen the same ledger.
        w.model = BrokeredModel(broker=Broker(os.path.join(self.tmp, "broker.db"), caps),
                                candidates=[cand()], gate=w.gate, costlog=w.costlog,
                                today=lambda: DAY, now=lambda: "t")
        with self.assertRaises(ModelRefused) as cm:
            w.model.complete(task_id=ids[2], task_class="chat", privacy_class="personal", messages=[])
        self.assertEqual(cm.exception.reason, "budget_ceiling")
        self.assertEqual(len(w.provider_calls), 2)

    def test_provider_failure_keeps_the_worst_case_reserved(self):
        def boom(**kw):
            raise RuntimeError("timeout")
        caps = Caps(request=5_000, task=5_000, day=5_000, month=1_000_000)
        w = World(self.tmp, caps=caps, provider=boom)
        with self.assertRaises(ModelUnavailable):
            w.model.complete(task_id="11111111-1111-4111-8111-111111111111", task_class="chat",
                             privacy_class="personal", messages=[])
        self.assertEqual(w.costlog.read()[-1]["state"], "unknown")
        with self.assertRaises(ModelRefused) as cm:   # the day is now fully reserved
            w.model.complete(task_id="11111111-1111-4111-8111-111111111112", task_class="chat",
                             privacy_class="personal", messages=[])
        self.assertEqual(cm.exception.reason, "budget_ceiling")

    def test_missing_or_invalid_cost_is_unknown_not_zero(self):
        for cost in (None, -1, 1.5, True, "12"):
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as tmp:
                w = World(tmp, provider=lambda **kw: {"text": "x", "cost_usd_micros": cost})
                r = w.model.complete(task_id="11111111-1111-4111-8111-111111111111",
                                     task_class="chat", privacy_class="personal", messages=[])
                self.assertEqual((r.state, r.actual_usd_micros), ("unknown", None))
                self.assertEqual(w.broker.status(r.call_id)[4], "dispatched")

    def test_overrun_returns_the_text_then_halts_all_further_calls(self):
        w = World(self.tmp, provider=lambda **kw: {"text": "x", "cost_usd_micros": 9_999})
        r = w.model.complete(task_id="11111111-1111-4111-8111-111111111111", task_class="chat",
                             privacy_class="personal", messages=[])
        self.assertEqual(r.state, "overrun")
        with self.assertRaises(ModelRefused) as cm:
            w.model.complete(task_id="11111111-1111-4111-8111-111111111112", task_class="chat",
                             privacy_class="personal", messages=[])
        self.assertEqual(cm.exception.reason, "unresolved_cost_variance")
        self.assertEqual(len(w.provider_calls), 1)

    def test_non_string_provider_text_is_a_failed_call(self):
        w = World(self.tmp, provider=lambda **kw: {"text": None, "cost_usd_micros": 1})
        with self.assertRaises(ModelUnavailable):
            w.model.complete(task_id="11111111-1111-4111-8111-111111111111", task_class="chat",
                             privacy_class="personal", messages=[])

    def test_day_change_between_reserve_and_dispatch_sends_nothing(self):
        w = World(self.tmp)
        days = iter([DAY, "2026-11-28"])
        w.model = BrokeredModel(broker=w.broker, candidates=[cand()], gate=w.gate,
                                costlog=w.costlog, today=lambda: next(days), now=lambda: "t")
        with self.assertRaises(ModelRefused) as cm:
            w.model.complete(task_id="11111111-1111-4111-8111-111111111111", task_class="chat",
                             privacy_class="personal", messages=[])
        self.assertEqual(cm.exception.reason, "reservation_period_changed")
        self.assertEqual(w.provider_calls, [])


class Turn(Base):
    def test_owner_gets_a_reply(self):
        w = World(self.tmp)
        self.assertEqual(w.rt.handle(Inbound(OWNER, "hello")).status, "replied")
        self.assertEqual(w.sent, [(OWNER, "hi")])
        self.assertEqual(w.provider_calls[0]["messages"], [{"role": "user", "content": "hello"}])

    def test_non_owner_gets_nothing_at_all(self):
        policy_calls = []
        w = World(self.tmp, policy=lambda m: policy_calls.append(m) or True)
        for chat in ("2002", "", None, 1001):
            self.assertEqual(w.rt.handle(Inbound(chat, "hello")).status, "ignored")
        self.assertEqual((w.sent, w.provider_calls, policy_calls, w.audit), ([], [], [], []))

    def test_policy_deny_error_or_truthy_non_true_blocks_the_model(self):
        def raises(m):
            raise RuntimeError("engine down")
        for policy in (lambda m: False, raises, lambda m: "yes", lambda m: 1, lambda m: None):
            with tempfile.TemporaryDirectory() as tmp:
                w = World(tmp, policy=policy)
                self.assertEqual(w.rt.handle(Inbound(OWNER, "hello")).status, "denied")
                self.assertEqual((w.sent, w.provider_calls), ([], []))

    def test_empty_and_oversized_text_ignored(self):
        w = World(self.tmp)
        for text in ("", "   ", "x" * 8001, None):
            self.assertEqual(w.rt.handle(Inbound(OWNER, text)).status, "ignored")
        self.assertEqual(w.provider_calls, [])

    def test_governor_plan_reaches_the_model_and_why_shows_labels_not_content(self):
        class Gov:
            def plan(self, text):
                return ContextPlan("chat", "personal", ("PRIVATE NOTE BODY",),
                                   ("note:sauna",), "mentions the sauna")
        w = World(self.tmp, governor=Gov())
        w.rt.handle(Inbound(OWNER, "sauna plans?"))
        self.assertEqual(w.provider_calls[0]["messages"][0],
                         {"role": "system", "content": "PRIVATE NOTE BODY"})
        self.assertEqual(w.rt.handle(Inbound(OWNER, "/why")).status, "replied")
        why = w.sent[-1][1]
        self.assertIn("note:sauna", why)
        self.assertIn("mentions the sauna", why)
        self.assertIn("$0.0012 (settled)", why)
        self.assertNotIn("PRIVATE NOTE BODY", why)
        self.assertEqual(len(w.provider_calls), 1)   # /why made no model call

    def test_governor_failure_degrades_to_no_context_and_says_so(self):
        class Broken:
            def plan(self, text):
                raise RuntimeError("jev down")
        w = World(self.tmp, governor=Broken())
        self.assertEqual(w.rt.handle(Inbound(OWNER, "hello")).status, "replied")
        w.rt.handle(Inbound(OWNER, "/why"))
        self.assertIn("Context Governor failed", w.sent[-1][1])

    def test_why_before_any_turn(self):
        w = World(self.tmp)
        w.rt.handle(Inbound(OWNER, "/why"))
        self.assertEqual(w.sent, [(OWNER, "No previous turn in this session.")])

    def test_budget_refusal_is_reported_to_the_owner(self):
        w = World(self.tmp, candidates=[cand(worst=RATIFIED_CAPS.request + 1)])
        out = w.rt.handle(Inbound(OWNER, "hello"))
        self.assertEqual((out.status, out.detail), ("refused", "budget_ceiling"))
        self.assertIn("budget_ceiling", w.sent[-1][1])

    def test_provider_failure_is_reported_and_why_explains(self):
        def boom(**kw):
            raise RuntimeError("x")
        w = World(self.tmp, provider=boom)
        self.assertEqual(w.rt.handle(Inbound(OWNER, "hello")).status, "failed")
        w.rt.handle(Inbound(OWNER, "/why"))
        self.assertIn("provider_call_failed", w.sent[-1][1])

    def test_audit_outage_stops_the_model_call_and_the_reply(self):
        w = World(self.tmp)
        w.audit_down = True
        out = w.rt.handle(Inbound(OWNER, "hello"))
        self.assertEqual(out.status, "failed")
        self.assertEqual((w.provider_calls, w.sent), ([], []))

    def test_wired_runtime_exposes_only_model_and_owner_reply(self):
        w = World(self.tmp)
        self.assertEqual({(t.name, t.effect_class) for t in w.gate.tools()},
                         {("model.complete", "model"), ("telegram.reply", "reply_owner")})


if __name__ == "__main__":
    unittest.main()
