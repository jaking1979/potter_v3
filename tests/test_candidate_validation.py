import tempfile
import unittest
import inspect
import sys
import threading
from pathlib import Path
from dataclasses import replace
from model_broker.broker import Broker, Candidate, Caps, Refused, Request

class InvalidCandidateTests(unittest.TestCase):
    def test_candidate_field_bounds_refuse_without_reservation(self):
        with tempfile.TemporaryDirectory() as d:
            b = Broker.bootstrap(Path(d)/'ledger.db', Caps(50,50,50,50))
            req = Request('x','12345678-1234-4234-8234-123456789abc','research','private','2026-11-06','2026-11')
            good = Candidate('g'*200,'p'*200,True,frozenset({'research'}),frozenset({'private'}),1,2)
            over = frozenset(str(i) for i in range(65))
            bad = (replace(good,model='x'*201), replace(good,provider='x'*201),
                   replace(good,validated_tasks=over), replace(good,privacy_classes=over),
                   replace(good,validated_tasks=frozenset({'x'*201})),
                   replace(good,privacy_classes=frozenset({'x'*201})))
            for item in bad:
                with self.subTest(item=item):
                    with self.assertRaisesRegex(Refused,'malformed_candidate'):
                        b.reserve(req,[good,item])
            bounded = replace(good, validated_tasks=frozenset({'research', *(str(i) for i in range(63))}))
            self.assertEqual(b.reserve(req,[bounded]),bounded)

    def test_list_growth_between_precheck_and_snapshot_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            b = Broker.bootstrap(Path(d)/'ledger.db', Caps(50,50,50,50))
            request = Request('x','12345678-1234-4234-8234-123456789abc','research','private','2026-11-06','2026-11')
            good = Candidate('good','provider',True,frozenset({'research'}),frozenset({'private'}),1,2)
            candidates = [good]
            reached, resume = threading.Event(), threading.Event()
            source, first = inspect.getsourcelines(Broker.reserve)
            target = first + next(i for i, line in enumerate(source) if line.strip() == 'candidates = tuple(candidates)')
            results = []
            def trace(frame, event, arg):
                if event == 'line' and frame.f_code is Broker.reserve.__code__ and frame.f_lineno == target:
                    reached.set()
                    if not resume.wait(5):
                        raise TimeoutError('test did not resume')
                return trace
            def worker():
                sys.settrace(trace)
                try:
                    b.reserve(request, candidates)
                except Exception as exc:
                    results.append(exc)
                finally:
                    sys.settrace(None)
            t = threading.Thread(target=worker)
            t.start()
            try:
                self.assertTrue(reached.wait(5))
                candidates.extend([good] * 1024)
            finally:
                resume.set()
                t.join(5)
            self.assertFalse(t.is_alive())
            self.assertEqual(len(results), 1)
            self.assertIsInstance(results[0], Refused)
            self.assertIn('malformed_candidate', str(results[0]))
            # The refused call wrote no reservation.
            self.assertEqual(b.reserve(request, [good]), good)

    def test_invalid_containers_are_refused_before_iteration_and_write(self):
        with tempfile.TemporaryDirectory() as d:
            b = Broker.bootstrap(Path(d)/'ledger.db', Caps(50,50,50,50))
            request = Request('x','12345678-1234-4234-8234-123456789abc','research','private','2026-11-06','2026-11')
            good = Candidate('good','provider',True,frozenset({'research'}),frozenset({'private'}),1,2)
            seen = []
            def stream():
                seen.append(True)
                yield good
            class HostileList(list):
                def __iter__(self):
                    raise AssertionError('must not iterate')
            for items in (None, 'x', {'x': good}, stream(), HostileList([good]), [good] * 1025):
                with self.subTest(kind=type(items)):
                    with self.assertRaisesRegex(Refused, 'malformed_candidate'):
                        b.reserve(request,items)
            self.assertEqual(seen, [])
            self.assertEqual(b.reserve(request,(good,)),good)

    def test_malformed_candidate_refuses_whole_set_without_reservation(self):
        with tempfile.TemporaryDirectory() as d:
            b = Broker.bootstrap(Path(d)/'ledger.db', Caps(50,50,50,50))
            request = Request('x','12345678-1234-4234-8234-123456789abc','research','private','2026-11-06','2026-11')
            good = Candidate('good','provider',True,frozenset({'research'}),frozenset({'private'}),1,2)
            for invalid in (None, replace(good,validated_tasks=None), replace(good,privacy_classes=None),
                            replace(good,model=7), replace(good,provider=7),
                            replace(good,validated_tasks='research'),replace(good,privacy_classes='private'),
                            replace(good,approved=1), replace(good,available=1),
                            replace(good,expected_usd_micros=-1)):
                with self.subTest(invalid=invalid):
                    for items in ([invalid], [invalid, good], [good, invalid]):
                        with self.assertRaisesRegex(Refused,'malformed_candidate'):
                            b.reserve(request,items)
            # The refused attempts made no reservation; a later valid call works.
            self.assertEqual(b.reserve(request,[good]),good)

    def test_well_formed_disabled_candidate_is_not_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            b = Broker.bootstrap(Path(d)/'ledger.db', Caps(50,50,50,50))
            request = Request('x','12345678-1234-4234-8234-123456789abc','research','private','2026-11-06','2026-11')
            good = Candidate('good','provider',True,frozenset({'research'}),frozenset({'private'}),1,2)
            self.assertEqual(b.reserve(request,[replace(good,approved=False),good]),good)


