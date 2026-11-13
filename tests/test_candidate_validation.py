import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from model_broker.broker import Broker, Candidate, Caps, Refused, Request

class InvalidCandidateTests(unittest.TestCase):
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

