import itertools
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shadow.brief import MAX_ATTEMPTS, MorningBrief  # noqa: E402
from shadow.policy import engine_policy, make_engine  # noqa: E402
from test_shadow import OWNER, World  # noqa: E402

MON_8 = datetime(2026, 11, 23, 8, 0)      # Monday
MON_6 = datetime(2026, 11, 23, 6, 0)
SAT_8 = datetime(2026, 11, 28, 8, 0)


class T(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = self._t.name
        self.rows = []
        ids = itertools.count(1)
        engine = make_engine(trace_hook=self.rows.append, new_id=lambda p: f"{p}-{next(ids):032x}")
        self.w = World(self.tmp, policy=engine_policy(engine))
        self.state = os.path.join(self.tmp, "brief.json")
        self.b = MorningBrief(runtime=self.w.rt, owner_chat_id=OWNER, state_path=self.state)

    def test_weekday_after_send_time_sends_once_to_owner_only(self):
        self.assertEqual(self.b.tick(MON_8).status, "replied")
        self.assertIsNone(self.b.tick(MON_8))
        self.assertEqual([c for c, _ in self.w.sent], [OWNER])

    def test_not_before_send_time_and_not_on_weekends(self):
        self.assertIsNone(self.b.tick(MON_6))
        self.assertIsNone(self.b.tick(SAT_8))
        self.assertEqual(self.w.sent, [])

    def test_a_restart_does_not_repeat_a_delivered_brief(self):
        self.b.tick(MON_8)
        again = MorningBrief(runtime=self.w.rt, owner_chat_id=OWNER, state_path=self.state)
        self.assertIsNone(again.tick(MON_8))
        self.assertEqual(len(self.w.sent), 1)

    def test_failures_retry_up_to_the_cap_then_stop(self):
        self.w.provider = lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        self.w.audit_down = False
        n = 0
        for _ in range(MAX_ATTEMPTS + 3):
            if self.b.tick(MON_8) is not None:
                n += 1
        self.assertEqual(n, MAX_ATTEMPTS)

    def test_the_brief_is_policy_traced_with_a_per_attempt_task_id(self):
        self.b.tick(MON_8)
        [row] = self.rows
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.w.sent[0][0], OWNER)

    def test_retries_use_distinct_task_ids(self):
        seen = []
        orig = self.w.rt.handle
        self.w.rt.handle = lambda m: (seen.append(m.task_id), orig(m))[1]
        self.w.provider = lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        self.b.tick(MON_8)
        self.b.tick(MON_8)
        self.assertEqual(len(set(seen)), 2)

    def test_provenance_is_system_policy_and_never_principal(self):
        seen = []
        orig = self.w.rt.handle
        self.w.rt.handle = lambda m: (seen.append(m), orig(m))[1]
        self.b.tick(MON_8)
        [m] = seen
        self.assertEqual(m.provenance, ("system_policy",))
        self.assertEqual(m.event_id, "brief:2026-11-23")

    def test_policy_denial_sends_nothing_and_is_not_retried(self):
        self.w.rt._policy = lambda m: False
        self.assertEqual(self.b.tick(MON_8).status, "denied")
        self.assertIsNone(self.b.tick(MON_8))
        self.assertEqual(self.w.sent, [])

    def test_state_file_is_bounded_and_bad_hour_rejected(self):
        for d in range(1, 21):
            self.b._save({**self.b._load(), f"2026-10-{d:02d}": {"attempts": 1, "done": True}})
        self.assertLessEqual(len(json.loads(Path(self.state).read_text())), 14)
        with self.assertRaises(ValueError):
            MorningBrief(runtime=self.w.rt, owner_chat_id=OWNER, state_path=self.state, send_hour=24)

    def test_corrupt_state_is_treated_as_empty(self):
        Path(self.state).write_text("{not json")
        self.assertEqual(self.b.tick(MON_8).status, "replied")


if __name__ == "__main__":
    unittest.main()
