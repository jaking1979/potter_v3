import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from model_broker.broker import Broker, Candidate, Caps, Refused, Request

class InvalidCandidateTests(unittest.TestCase):
    def test_malformed_candidates_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            b = Broker.bootstrap(Path(d)/'ledger.db', Caps(50,50,50,50))
            request = Request('x','12345678-1234-4234-8234-123456789abc','research','private','2026-11-06','2026-11')
            good = Candidate('good','provider',True,frozenset({'research'}),frozenset({'private'}),1,2)
            for invalid in (None, replace(good,validated_tasks=None), replace(good,privacy_classes=None),
                            replace(good,model=7), replace(good,provider=7),
                            replace(good,validated_tasks='research'),replace(good,privacy_classes='private')):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(Refused,'no_eligible_candidate'):
                        b.reserve(request,[invalid])
            self.assertEqual(b.reserve(request,[replace(good,validated_tasks=None),good]),good)
