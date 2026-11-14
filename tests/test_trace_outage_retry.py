"""Real offline E2/C1/C2/C6 trace outage retry; fake provider only."""
import tempfile
import unittest
from test_real_c1_c6_hook import ActualHookIntegration
from tracing.sqlite_sink import SqliteTraceSink


class TraceOutageRetry(unittest.TestCase):
    def test_transient_missing_sink_recovers_without_effect_before_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, sink = ActualHookIntegration().make_world(tmp)
            task = w.runner.tick()[0]
            sink.path.unlink()
            first = w.runner.run_once()
            self.assertEqual(first.outcome, "retry")
            self.assertEqual(w.ext.calls(), 0)
            self.assertEqual(w.runner.tasks.get(task.id).state, "queued")
            SqliteTraceSink.bootstrap(sink.path, environment="synthetic")
            second = w.runner.run_once()
            self.assertEqual(second.outcome, "done")
            self.assertEqual(w.ext.calls(), 1)
            self.assertEqual(len(sink.records()), 1)

    def test_repeated_outage_escalates_without_provider_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, sink = ActualHookIntegration().make_world(tmp)
            w.runner.tick()
            sink.path.unlink()
            outcomes = [w.runner.run_once().outcome for _ in range(3)]
            self.assertEqual(outcomes, ["retry", "retry", "escalated"])
            self.assertEqual(w.ext.calls(), 0)
