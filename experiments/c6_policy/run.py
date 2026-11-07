"""Explicit synthetic C0/C1/C6 integration, never imported by runtime.

Run in a fresh process with -I and an explicitly staged source root. The
manifest checks content identity, not Git/Notion authority or T39 provenance.
Verified bytes are copied before import; caller paths must be trusted.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4


def stage(root, target, manifest):
    """Snapshot bounded expected bytes, then write only those bytes for import."""
    content = {}
    for name, spec in manifest['files'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid manifest path')
        with (root / relative).open('rb') as stream:
            data = stream.read(131073)
        if len(data) > 131072 or hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise ValueError('component content mismatch: ' + name)
        content[name] = data
    for name, data in content.items():
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def suite():
    from policy import Capability, CapabilityRegistry, RiskClass, PolicyEngine, PolicyRequest, Caller
    from policy._contracts import approval as c0
    from tracing.policy_binding import SyntheticPolicyAudit
    from tracing.sqlite_sink import SqliteTraceSink, TraceStoreRefused

    class Integration(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.path = Path(self.tmp.name) / 'synthetic.db'
            self.store = SqliteTraceSink.bootstrap(self.path, environment='synthetic')
            self.forward = lambda event_id, line: self.store.put(event_id, line)
            self.audit = SyntheticPolicyAudit(lambda event_id, line: self.forward(event_id, line),
                                              environment='synthetic')
            self.ledger = c0.MemoryApprovalLedger()
            self.engine = PolicyEngine(registry=CapabilityRegistry([
                Capability('notes.read', RiskClass.READ, False, False),
                Capability('notes.write', RiskClass.CONSEQUENTIAL, True, False)]),
                grants={'synthetic': frozenset({'notes.read', 'notes.write'})},
                ledger=self.ledger, authenticator=None, clock=lambda: 100,
                trace_hook=self.audit.hook)

        def request(self, task=None, capability='notes.read'):
            return PolicyRequest(Caller('synthetic'), capability,
                c0.BoundAction(task or str(uuid4()), capability, 'read', 'synthetic',
                               parameters={'body': 'CANARY_PRIVATE_TEXT'}))

        def decide(self, request, *, context_task='request'):
            task = request.action.task_id if context_task == 'request' else context_task
            with self.audit.bind(task_id=task, trace_id=str(uuid4()),
                                 timestamp='2026-11-07T07:29:00Z'):
                return self.engine.decide(request)

        def records(self):
            return [json.loads(line) for _, line in self.store.records()]

        def test_concurrent_real_policy_to_local_sink_and_restart(self):
            requests = [self.request() for _ in range(32)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                decisions = list(pool.map(self.decide, requests))
            self.assertTrue(all(d.outcome == 'allow' for d in decisions))
            self.store = SqliteTraceSink.open_existing(self.path, environment='synthetic')
            records = self.records()
            self.assertEqual(len(records), 32)
            actual = {r['potter.subject.id']: r['task.id'] for r in records}
            self.assertEqual(actual, {d.decision_id: q.action.task_id for d, q in zip(decisions, requests)})
            self.assertNotIn('CANARY_PRIVATE_TEXT', json.dumps(records))
            # Context is reset: a later unbound decision fails closed.
            self.assertEqual(self.engine.decide(self.request()).reason_code, 'trace_error')

        def test_policy_denial_is_bounded_and_linked(self):
            request = self.request(capability='notes.write')
            decision = self.decide(request)
            self.assertEqual(decision.outcome, 'deny')
            record, = self.records()
            self.assertEqual((record['task.id'], record['potter.outcome']),
                             (request.action.task_id, 'deny'))

        def test_malformed_denial_never_fabricates_task(self):
            decision = self.decide(None, context_task=None)
            self.assertEqual(decision.outcome, 'deny')
            record, = self.records()
            self.assertEqual(record['schema'], 'potter.trace.uncorrelated-denial/v0')
            self.assertEqual(record['decision.id'], decision.decision_id)
            self.assertNotIn('task.id', record)

        def test_mismatched_context_never_persists(self):
            decision = self.decide(self.request(), context_task=str(uuid4()))
            self.assertEqual((decision.outcome, decision.reason_code), ('deny', 'trace_error'))
            self.assertEqual(self.records(), [])

        def test_missing_context_denies_without_record(self):
            decision = self.engine.decide(self.request())
            self.assertEqual((decision.outcome, decision.reason_code), ('deny', 'trace_error'))
            self.assertEqual(self.records(), [])

        def test_lost_store_denies_and_does_not_recreate(self):
            self.path.unlink()
            decision = self.decide(self.request())
            self.assertEqual((decision.outcome, decision.reason_code), ('deny', 'trace_error'))
            self.assertFalse(self.path.exists())

        def test_sink_failure_before_commit_denies(self):
            def refuse(event_id, line):
                raise OSError('synthetic unavailable sink')
            self.forward = refuse
            decision = self.decide(self.request())
            self.assertEqual((decision.outcome, decision.reason_code), ('deny', 'trace_error'))
            self.assertEqual(self.records(), [])

        def test_committed_hook_failure_is_not_final_authority(self):
            # Important limit: a persisted candidate allow is NOT a final grant.
            def commit_then_raise(event_id, line):
                self.store.put(event_id, line)
                raise OSError('synthetic failure after local commit')
            self.forward = commit_then_raise
            decision = self.decide(self.request())
            self.assertEqual((decision.outcome, decision.reason_code), ('deny', 'trace_error'))
            record, = self.records()
            self.assertEqual(record['potter.outcome'], 'allow')

        def test_identical_uncertain_delivery_is_locally_idempotent(self):
            self.decide(self.request())
            event_id, line = self.store.records()[0]
            self.assertFalse(self.store.put(event_id, line))
            self.assertEqual(len(self.records()), 1)
            with self.assertRaises(TraceStoreRefused):
                self.store.put(event_id, line.replace('"allow"', '"deny"'))

        def test_hook_retry_stable_identity_and_context_restoration(self):
            request = self.request()
            with self.audit.bind(task_id=request.action.task_id, trace_id=str(uuid4()),
                                 timestamp='2026-11-07T07:29:00Z'):
                decision = self.engine.decide(request)
                self.audit.hook(decision.trace_record())
                self.assertEqual(len(self.records()), 1)
                with self.audit.bind(task_id=str(uuid4()), trace_id=str(uuid4()),
                                     timestamp='2026-11-07T07:29:00Z'):
                    with self.assertRaises(ValueError):
                        self.audit.hook(decision.trace_record())
                self.audit.hook(decision.trace_record())
                self.assertEqual(len(self.records()), 1)

        def test_binding_rejects_production_and_invalid_context(self):
            for environment in ('staging', 'production'):
                with self.assertRaises(ValueError):
                    SyntheticPolicyAudit(self.store.put, environment=environment)
            with self.assertRaises(ValueError):
                with self.audit.bind(task_id='CANARY_PRIVATE_TEXT', trace_id=str(uuid4()),
                                     timestamp='2026-11-07T07:29:00Z'):
                    pass

    return unittest.defaultTestLoader.loadTestsFromTestCase(Integration)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('sources.json').read_text())
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        stage(args.source_root.resolve(), staged, manifest)
        sys.path.insert(0, str(staged))
        result = unittest.TextTestRunner(verbosity=2).run(suite())
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
