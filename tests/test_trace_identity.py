import unittest
from datetime import datetime, timezone
from tracing.policy_adapter import PolicyTraceAdapter

TASK = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DECISION = "dec-" + "a" * 32
FIXED_TIME = datetime(2026, 1, 2, tzinfo=timezone.utc)

def record(**changes):
    d = dict(task_id=TASK, decision_id=DECISION, outcome="allow", reason_code="ok",
             capability="notes.append", risk="low", action_hash="b"*64, effect_id=None)
    d.update(changes)
    return d

class PolicyTraceIdentity(unittest.TestCase):
    def test_same_decision_id_has_stable_trace_id_across_adapter_recreation(self):
        outputs=[]
        for _ in range(2):
            PolicyTraceAdapter(outputs.append, clock=lambda: FIXED_TIME)(record())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0].subject_id, DECISION)
        self.assertEqual(outputs[0].timestamp, FIXED_TIME.isoformat())

    def test_digest_changes_when_decision_changes_but_id_remains_stable(self):
        out=[]
        a=PolicyTraceAdapter(out.append, clock=lambda: FIXED_TIME)
        a(record())
        a(record(outcome="deny", reason_code="not_granted"))
        self.assertEqual(out[0].trace_id, out[1].trace_id)
        self.assertNotEqual(out[0].decision_digest, out[1].decision_digest)

    def test_unlinked_denial_and_unlinked_allow(self):
        a=PolicyTraceAdapter(lambda _: self.fail("unexpected emit"))
        a({"outcome":"deny"})
        self.assertEqual(a.unlinked_denials,1)
        with self.assertRaises(ValueError): a({"outcome":"allow"})

    def test_malformed_metadata_fails_before_emission(self):
        out=[]
        a=PolicyTraceAdapter(out.append)
        for bad in (record(decision_id="é"), record(reason_code={"bad":"value"})):
            with self.assertRaises(ValueError): a(bad)
        missing=record(); del missing["action_hash"]
        with self.assertRaises(ValueError): a(missing)
        self.assertEqual(out,[])

    def test_bad_environment_and_naive_clock_fail_closed(self):
        with self.assertRaises(ValueError): PolicyTraceAdapter(lambda _:None, environment="bad")
        a=PolicyTraceAdapter(lambda _:self.fail("unexpected emit"), clock=lambda:datetime(2026,1,1))
        with self.assertRaises(ValueError): a(record())

if __name__ == "__main__":
    unittest.main()
