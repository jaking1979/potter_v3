"""Context Governor v0 and its place in the turn loop (synthetic transports)."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shadow.governor import MAX_PERSONA_CHARS, PERSONA, ContextGovernorV0  # noqa: E402
from shadow.turn import Inbound  # noqa: E402
from test_shadow import OWNER, World  # noqa: E402


def blocks(plan):
    return plan.context[1].split("\n\n")[1:] if len(plan.context) > 1 else []


class Plan(unittest.TestCase):
    def test_no_history_gives_only_the_persona(self):
        plan = ContextGovernorV0().plan("hi")
        self.assertEqual((plan.context, plan.sources, plan.task_class, plan.privacy_class),
                         ((PERSONA,), ("persona:v0", "recent_exchanges:0"), "chat", "personal"))

    def test_only_the_last_n_exchanges_are_kept_oldest_first(self):
        g = ContextGovernorV0(max_exchanges=3)
        for i in range(6):
            g.observe(f"q{i}", f"a{i}")
        plan = g.plan("next")
        self.assertEqual(blocks(plan), ["User: q3\nPotter: a3", "User: q4\nPotter: a4", "User: q5\nPotter: a5"])
        self.assertEqual(plan.sources[1], "recent_exchanges:3")

    def test_each_message_is_clipped(self):
        g = ContextGovernorV0(per_message_chars=20)
        g.observe("x" * 100, "y" * 100)
        [b] = blocks(g.plan("n"))
        user, reply = b.split("\n")
        self.assertEqual((len(user), len(reply)), (len("User: ") + 20, len("Potter: ") + 20))
        self.assertTrue(user.endswith("…") and reply.endswith("…"))

    def test_the_budget_drops_the_oldest_exchanges_first_and_is_never_exceeded(self):
        g = ContextGovernorV0(max_exchanges=10, per_message_chars=50, max_context_chars=len(PERSONA) + 200)
        for i in range(10):
            g.observe(f"question number {i} " * 3, f"answer number {i} " * 3)
        plan = g.plan("n")
        self.assertLessEqual(sum(len(c) for c in plan.context), len(PERSONA) + 200)
        kept = blocks(plan)
        self.assertEqual(plan.sources[1], f"recent_exchanges:{len(kept)}")
        self.assertLess(len(kept), 10)
        self.assertTrue(kept and "number 9" in kept[-1])
        self.assertNotIn("number 0", "".join(kept))

    def test_a_budget_that_exactly_fits_one_exchange_keeps_it_and_one_less_drops_it(self):
        g = ContextGovernorV0(max_context_chars=len(PERSONA))
        g.observe("q", "a")
        block = "User: q\nPotter: a"
        header = "Earlier in this conversation (oldest first):"
        exact = len(PERSONA) + len(header) + 2 + len(block) + 2
        for budget, expected in ((exact, 1), (exact - 1, 0)):
            with self.subTest(budget=budget):
                g2 = ContextGovernorV0(max_context_chars=budget)
                g2.observe("q", "a")
                self.assertEqual(g2.plan("n").sources[1], f"recent_exchanges:{expected}")

    def test_a_budget_that_fits_only_the_persona_drops_all_history(self):
        g = ContextGovernorV0(max_context_chars=len(PERSONA))
        g.observe("q", "a")
        self.assertEqual(g.plan("n").context, (PERSONA,))

    def test_zero_exchanges_remembers_nothing(self):
        g = ContextGovernorV0(max_exchanges=0)
        g.observe("q", "a")
        self.assertEqual(g.plan("n").context, (PERSONA,))

    def test_labels_and_reason_never_contain_conversation_text(self):
        g = ContextGovernorV0()
        g.observe("my sauna code is 4821", "noted")
        plan = g.plan("n")
        self.assertNotIn("4821", " ".join(plan.sources) + plan.reason)

    def test_bad_observations_are_ignored(self):
        g = ContextGovernorV0()
        for pair in ((None, "a"), ("q", None), ("", "a"), ("q", "  "), (1, 2)):
            g.observe(*pair)
        self.assertEqual(g.plan("n").sources[1], "recent_exchanges:0")

    def test_construction_bounds(self):
        for kw in ({"persona": ""}, {"persona": "x" * (MAX_PERSONA_CHARS + 1)}, {"persona": 5},
                   {"max_exchanges": -1}, {"per_message_chars": "a"}, {"max_context_chars": 10}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                ContextGovernorV0(**kw)

    def test_the_persona_forbids_actions_and_makes_no_claims_about_accounts(self):
        self.assertIn("cannot send email, buy, trade", PERSONA)
        self.assertLessEqual(len(PERSONA), MAX_PERSONA_CHARS)

    def test_concurrent_observation_is_safe(self):
        g = ContextGovernorV0(max_exchanges=4)
        threads = [threading.Thread(target=lambda i=i: g.observe(f"q{i}", f"a{i}")) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(blocks(g.plan("n"))), 4)


class InTheTurnLoop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.seen = []

        def provider(**kw):
            self.seen.append(kw["messages"])
            return {"text": "reply-%d" % len(self.seen), "input_tokens": 10, "output_tokens": 2, "cost_usd_micros": 1_200}

        self.gov = ContextGovernorV0()
        self.w = World(self._tmp.name, provider=provider, governor=self.gov)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_second_turn_sees_the_first_exchange(self):
        self.w.rt.handle(Inbound(OWNER, "first question"))
        self.w.rt.handle(Inbound(OWNER, "second question"))
        second = " ".join(m["content"] for m in self.seen[1])
        self.assertIn("first question", second)
        self.assertIn("reply-1", second)
        self.assertEqual(self.seen[1][-1], {"role": "user", "content": "second question"})
        self.assertNotIn("Earlier in this conversation", " ".join(m["content"] for m in self.seen[0]))

    def test_a_failed_model_call_is_not_remembered(self):
        def failing(**kw):
            raise RuntimeError("down")
        w = World(self._tmp.name + "/f" if os.makedirs(self._tmp.name + "/f") is None else "", provider=failing, governor=self.gov)
        w.rt.handle(Inbound(OWNER, "lost question"))
        self.assertEqual(self.gov.plan("n").sources[1], "recent_exchanges:0")

    def test_a_failed_reply_is_not_remembered(self):
        self.w.sent = None                                     # the reply transport now raises
        self.assertEqual(self.w.rt.handle(Inbound(OWNER, "hello")).status, "failed")
        self.assertEqual(self.gov.plan("n").sources[1], "recent_exchanges:0")

    def test_a_failing_observe_never_blocks_the_reply(self):
        self.gov.observe = lambda u, r: (_ for _ in ()).throw(RuntimeError("memory down"))
        self.assertEqual(self.w.rt.handle(Inbound(OWNER, "hello")).status, "replied")

    def test_why_reports_labels_and_counts_not_content(self):
        self.w.rt.handle(Inbound(OWNER, "the-secret-topic"))
        self.w.rt.handle(Inbound(OWNER, "again"))
        self.w.rt.handle(Inbound(OWNER, "/why"))
        text = self.w.sent[-1][1]
        self.assertIn("persona:v0, recent_exchanges:1", text)
        self.assertNotIn("the-secret-topic", text)

    def test_non_owner_turns_are_neither_answered_nor_remembered(self):
        self.w.rt.handle(Inbound("999", "intruder"))
        self.assertEqual(self.gov.plan("n").sources[1], "recent_exchanges:0")


if __name__ == "__main__":
    unittest.main()
