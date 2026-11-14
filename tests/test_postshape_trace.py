import tempfile
import unittest
import uuid
import resp_world as rw
from policy import Caller, PolicyRequest
from policy._contracts import approval as c0
from tracing.trace import DecisionTrace

class PostshapeTrace(unittest.TestCase):
    def test_runner_task_id_reaches_c6(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=rw.World(tmp,definitions=[rw.batch(n=1)]); rows=[]; w.engine._trace=rows.append
            task=w.runner.tick()[0]; outcome=w.runner.run_once()
            self.assertEqual(outcome.outcome,'done'); self.assertEqual(rows[0]['task_id'],task.id)
            trace=DecisionTrace(task_id=rows[0]['task_id'], trace_id='abcdefab-cdef-4def-8def-abcdefabcdef', component='policy', outcome=rows[0]['outcome'],decision_digest='a'*64, environment='synthetic',timestamp='2026-11-13T20:00:00Z',subject_id=rows[0]['decision_id'])
            self.assertEqual(trace.task_id,task.id)
    def test_malformed_request_has_no_trusted_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=rw.World(tmp,definitions=[rw.batch(n=1)]); rows=[]; w.engine._trace=rows.append
            result=w.engine.decide(None)
            self.assertEqual(result.reason_code,'malformed_request'); self.assertNotIn('task_id',rows[0])
    def test_noncanonical_id_does_not_change_decision_or_add_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=rw.World(tmp,definitions=[rw.batch(n=1)]); rows=[]; w.engine._trace=rows.append
            action=c0.BoundAction('legacy-task','notes.append','append','test',{})
            w.engine.decide(PolicyRequest(Caller('agent:potter'),'notes.append',action))
            self.assertNotIn('task_id',rows[0])
    def test_after_shape_denial_retains_correlation(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=rw.World(tmp,definitions=[rw.batch(n=1)]); rows=[]; w.engine._trace=rows.append
            task=str(uuid.uuid4()); action=c0.BoundAction(task,'unknown.tool','append','test',{})
            result=w.engine.decide(PolicyRequest(Caller('agent:potter'),'unknown.tool',action))
            self.assertEqual(result.outcome,'deny'); self.assertEqual(rows[0]['task_id'],task)
    def test_runner_rejects_action_for_another_claimed_task(self):
        from unittest.mock import patch
        from responsibilities import Step
        with tempfile.TemporaryDirectory() as tmp:
            w=rw.World(tmp,definitions=[rw.batch(n=1)]); rows=[]; w.engine._trace=rows.append
            task=w.runner.tick()[0]
            wrong=c0.BoundAction(str(uuid.uuid4()),'notes.append','append','test',{})
            with patch.object(Step,'build',return_value=wrong):
                result=w.runner.run_once()
            self.assertNotEqual(wrong.task_id, task.id)
            self.assertEqual(result.outcome, 'failed')
            self.assertEqual(w.ext.calls(), 0)
            self.assertEqual(rows, [])  # rejected by runner before C1 policy trace

    def test_waiting_rebuild_cannot_switch_claimed_task(self):
        from unittest.mock import patch
        from responsibilities import Step
        with tempfile.TemporaryDirectory() as tmp:
            w = rw.World(tmp, definitions=[rw.bill_watch()])
            w.runner.on_event("bill.due", {"id": "bill-test", "amount": 120})
            result = w.runner.run_once()
            self.assertEqual(result.outcome, "waiting_approval")
            task = w.runner.tasks.get(result.task_id)
            self.assertIsNotNone(w.runner._waiting_step(task))
            wrong = c0.BoundAction(str(uuid.uuid4()), "notes.append", "append", "test", {})
            with patch.object(Step, "build", return_value=wrong):
                self.assertIsNone(w.runner._waiting_step(task))
                self.assertEqual(w.runner.poll_approvals(), [])
            self.assertEqual(w.runner.tasks.get(task.id).state, "waiting_approval")
            self.assertEqual(w.ext.calls("pay"), 0)

    def test_missing_request_wrong_rebuild_fails_with_binding_reason(self):
        from unittest.mock import patch
        from responsibilities import Step
        class Crash(BaseException): pass
        with tempfile.TemporaryDirectory() as tmp:
            w = rw.World(tmp, definitions=[rw.bill_watch()])
            w.runner.on_event('bill.due', {'id':'missing-test', 'amount':120})
            with patch.object(w.runner, '_issue', side_effect=Crash()):
                with self.assertRaises(Crash): w.runner.run_once()
            with w.db.read() as c:
                tid = c.execute("SELECT id FROM tasks WHERE state='waiting_approval'").fetchone()[0]
            wrong = c0.BoundAction(str(uuid.uuid4()), 'notes.append', 'append', 'test', {})
            with patch.object(Step, 'build', return_value=wrong):
                outcomes = w.runner.poll_approvals()
            self.assertEqual([x.outcome for x in outcomes], ['failed'])
            self.assertIn('task binding mismatch', w.runner.tasks.get(tid).last_error)
            self.assertEqual(w.ext.calls('pay'), 0)
            with w.db.read() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM approval_requests').fetchone()[0], 0)

    def test_wrong_rebuild_causes_issuer_void(self):
        from unittest.mock import patch
        from responsibilities import Step
        with tempfile.TemporaryDirectory() as tmp:
            w = rw.World(tmp, definitions=[rw.bill_watch()])
            w.runner.on_event('bill.due', {'id':'wrong-live', 'amount':120})
            result = w.runner.run_once()
            wrong = c0.BoundAction(str(uuid.uuid4()), 'notes.append', 'append', 'test', {})
            with patch.object(Step, 'build', return_value=wrong):
                self.assertIsNone(w.runner.live_action(result.detail, result.task_id))
                self.assertEqual(w.say('/approve ' + result.detail).reason, 'state_changed')
            self.assertEqual(w.ext.calls('pay'), 0)

    def test_trace_failure_blocks_fake_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=rw.World(tmp,definitions=[rw.batch(n=1)])
            def fail(_): raise OSError('fixture')
            w.engine._trace=fail; w.runner.tick(); w.runner.run_once()
            self.assertEqual(w.ext.calls(),0)

