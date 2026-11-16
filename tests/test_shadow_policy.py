"""C1 PolicyEngine + durable trace wired into the shadow turn loop (synthetic transports)."""
import itertools
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from policy import ALLOW, DENY, Caller, CapabilityRegistry, PolicyEngine, PolicyRequest  # noqa: E402
from policy._contracts import approval as c0  # noqa: E402
from shadow.policy import CAPABILITIES, GRANTS, PRINCIPAL, engine_policy, make_engine  # noqa: E402
from shadow.tracefile import JsonlTraceSink  # noqa: E402
from shadow.turn import Inbound  # noqa: E402
from test_shadow import OWNER, World  # noqa: E402


class Wired(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.trace_path = os.path.join(self.tmp, "policy-trace.jsonl")
        ids = itertools.count(1)
        self.sink = JsonlTraceSink(self.trace_path, clock=lambda: "2026-11-27T12:00:00Z")
        self.engine = make_engine(trace_hook=self.sink, new_id=lambda p: f"{p}-{next(ids):032x}")
        self.w = World(self.tmp, policy=engine_policy(self.engine))

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self):
        if not os.path.exists(self.trace_path):
            return []
        with open(self.trace_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_an_allowed_turn_is_traced_with_join_fields_and_no_message_text(self):
        msg = Inbound(OWNER, "tell me a secret-plan phrase")
        self.assertEqual(self.w.rt.handle(msg).status, "replied")
        [row] = self.rows()
        self.assertEqual((row["potter.outcome"], row["potter.capability"], row["potter.reason"]),
                         ("allow", "chat.turn", "ok"))
        self.assertEqual(row["task.id"], msg.task_id)
        self.assertEqual(row["schema"], "potter.trace.decision/v1")
        self.assertRegex(row["potter.action.hash"], r"^[0-9a-f]{64}$")
        self.assertNotIn("secret-plan", json.dumps(row))
        self.assertEqual(len(self.w.sent), 1)

    def test_the_turn_task_id_is_shared_by_policy_trace_and_cost_log(self):
        task = str(uuid.uuid4())
        self.w.rt.handle(Inbound(OWNER, "hello", task_id=task))
        self.assertEqual([r["task.id"] for r in self.rows()], [task])
        with open(os.path.join(self.tmp, "cost.jsonl"), encoding="utf-8") as f:
            self.assertEqual([json.loads(line)["task_id"] for line in f], [task])

    def test_the_engine_receives_the_ingress_provenance_and_task(self):
        seen = []

        class Spy:
            def decide(self, request):
                seen.append(request)
                return make_engine(trace_hook=lambda r: None).decide(request)

        w = World(os.path.join(self.tmp, "spy") if os.makedirs(os.path.join(self.tmp, "spy")) is None else "", policy=engine_policy(Spy()))
        msg = Inbound(OWNER, "hello", provenance=("email_body", "web_page"))
        w.rt.handle(msg)
        [request] = seen
        self.assertEqual((request.provenance, request.action.task_id, request.capability),
                         (("email_body", "web_page"), msg.task_id, "chat.turn"))

    def test_trace_ids_are_stable_per_decision_and_differ_between_decisions(self):
        self.w.rt.handle(Inbound(OWNER, "one"))
        self.w.rt.handle(Inbound(OWNER, "two"))
        a, b = self.rows()
        self.assertNotEqual(a["trace.id"], b["trace.id"])

    def test_a_failing_write_or_fsync_each_deny_the_turn(self):
        from unittest.mock import patch
        for target in ("os.write", "os.fsync"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as sub:
                ids = itertools.count(1)
                engine = make_engine(trace_hook=JsonlTraceSink(os.path.join(sub, "t.jsonl")),
                                     new_id=lambda p: f"{p}-{next(ids):032x}")
                w = World(sub, policy=engine_policy(engine))
                with patch("shadow.tracefile." + target, side_effect=OSError("io")):
                    self.assertEqual(w.rt.handle(Inbound(OWNER, "hello")).status, "denied")
                self.assertEqual((w.sent, w.provider_calls), ([], []))

    @unittest.skipUnless(os.path.exists("/dev/full"), "needs /dev/full")
    def test_a_failed_write_after_open_also_denies_the_turn(self):
        ids = itertools.count(1)
        engine = make_engine(trace_hook=JsonlTraceSink("/dev/full"), new_id=lambda p: f"{p}-{next(ids):032x}")
        sub = os.path.join(self.tmp, "full")
        os.makedirs(sub)
        w = World(sub, policy=engine_policy(engine))
        self.assertEqual(w.rt.handle(Inbound(OWNER, "hello")).status, "denied")
        self.assertEqual((w.sent, w.provider_calls), ([], []))

    def test_a_trace_outage_denies_the_turn_before_any_model_call_or_reply(self):
        sub = os.path.join(self.tmp, "second")
        os.makedirs(os.path.join(sub, "gone"))
        broken = JsonlTraceSink(os.path.join(sub, "gone"))           # a directory: cannot be appended to
        ids = itertools.count(1)
        engine = make_engine(trace_hook=broken, new_id=lambda p: f"{p}-{next(ids):032x}")
        w = World(sub, policy=engine_policy(engine))
        outcome = w.rt.handle(Inbound(OWNER, "hello"))
        self.assertEqual((outcome.status, outcome.detail), ("denied", "policy"))
        self.assertEqual((w.sent, w.provider_calls), ([], []))

    def test_every_forbidden_capability_is_denied_even_if_granted(self):
        forbidden = [c.name for c in CAPABILITIES if c.name != "chat.turn"]
        self.assertGreaterEqual(len(forbidden), 6)
        eng = PolicyEngine(registry=CapabilityRegistry(CAPABILITIES),
                           grants={PRINCIPAL: frozenset(c.name for c in CAPABILITIES)},
                           ledger=c0.MemoryApprovalLedger(), authenticator=None,
                           clock=lambda: 1_800_000_000, trace_hook=lambda r: None)
        for name in forbidden:
            with self.subTest(capability=name):
                action = c0.BoundAction(str(uuid.uuid4()), name, "op", "t", {"k": "v"})
                d = eng.decide(PolicyRequest(Caller(PRINCIPAL), name, action, None, None, ("principal_direct",), None))
                self.assertEqual(d.outcome, DENY)

    def test_only_chat_turn_is_grantable(self):
        self.assertEqual(GRANTS, {PRINCIPAL: frozenset({"chat.turn"})})

    def test_untrusted_provenance_still_gets_only_the_read_class_turn(self):
        msg = Inbound(OWNER, "hello", provenance=("email_body",))
        self.assertEqual(self.w.rt.handle(msg).status, "replied")
        [row] = self.rows()
        self.assertEqual(row["potter.capability"], "chat.turn")

    def test_a_malformed_task_id_never_reaches_policy_or_the_model(self):
        for bad in ("not-a-uuid", "", "3F0C8A52-6B1E-5C7D-9A2E-1F4B6C8D0E21"):
            with self.subTest(task_id=bad):
                self.assertEqual(self.w.rt.handle(Inbound(OWNER, "hello", task_id=bad)).detail, "bad_task_id")
        self.assertEqual((self.rows(), self.w.sent, self.w.provider_calls), ([], [], []))

    def test_rows_are_append_only_and_private(self):
        self.w.rt.handle(Inbound(OWNER, "one"))
        self.w.rt.handle(Inbound(OWNER, "two"))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(oct(os.stat(self.trace_path).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
