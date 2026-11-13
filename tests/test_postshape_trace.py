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

    def test_trace_failure_blocks_fake_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=rw.World(tmp,definitions=[rw.batch(n=1)])
            def fail(_): raise OSError('fixture')
            w.engine._trace=fail; w.runner.tick(); w.runner.run_once()
            self.assertEqual(w.ext.calls(),0)
