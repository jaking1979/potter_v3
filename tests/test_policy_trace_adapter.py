import tempfile
import unittest
import resp_world as rw
from tracing.policy_adapter import PolicyTraceAdapter

class PolicyAdapterIntegration(unittest.TestCase):
    def test_real_runner_emits_linked_trace(self):
        with tempfile.TemporaryDirectory() as path:
            w=rw.World(path,definitions=[rw.batch(n=1)])
            emitted=[]; adapter=PolicyTraceAdapter(emitted.append)
            w.engine._trace=adapter
            task=w.runner.tick()[0]
            self.assertEqual(w.runner.run_once().outcome,'done')
            self.assertEqual(len(emitted),1)
            self.assertEqual(emitted[0].task_id,task.id)
            self.assertEqual(emitted[0].outcome,'allow')
    def test_real_malformed_request_is_unlinked_and_does_not_crash(self):
        with tempfile.TemporaryDirectory() as path:
            w=rw.World(path,definitions=[rw.batch(n=1)])
            emitted=[]; adapter=PolicyTraceAdapter(emitted.append)
            w.engine._trace=adapter
            self.assertEqual(w.engine.decide(None).reason_code,'malformed_request')
            self.assertEqual(adapter.unlinked_denials,1)
            self.assertEqual(emitted,[])
    def test_sink_failure_prevents_fake_provider_call(self):
        with tempfile.TemporaryDirectory() as path:
            w=rw.World(path,definitions=[rw.batch(n=1)])
            def fail(_): raise OSError('synthetic')
            w.engine._trace=PolicyTraceAdapter(fail)
            w.runner.tick(); w.runner.run_once()
            self.assertEqual(w.ext.calls(),0)
    def test_unlinked_allow_fails_closed(self):
        adapter=PolicyTraceAdapter(lambda _:None)
        with self.assertRaises(ValueError): adapter({'outcome':'allow'})

if __name__=='__main__': unittest.main()
