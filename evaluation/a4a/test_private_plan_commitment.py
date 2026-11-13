import unittest

from evaluation.a4a.private_plan_commitment import private_plan_tag, verify_private_plan


class PrivatePlan(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case-{i}': bool(i % 2) for i in range(20)}
        self.key = bytes(range(32))  # deterministic synthetic test key only

    def test_order_and_valid_verification(self):
        tag = private_plan_tag(self.plan, self.key)
        self.assertEqual(tag, private_plan_tag(dict(reversed(list(self.plan.items()))), self.key))
        self.assertTrue(verify_private_plan(self.plan, self.key, tag))

    def test_changed_flag_id_or_key_is_refused(self):
        tag = private_plan_tag(self.plan, self.key)
        for plan, key in (({**self.plan, 'case-0': True}, self.key),
                          ({**{k: v for k, v in self.plan.items() if k != 'case-0'},
                            'different': False}, self.key), (self.plan, b'x' * 32)):
            with self.assertRaises(ValueError):
                verify_private_plan(plan, key, tag)

    def test_reject_invalid_key_plan_flag_and_tag(self):
        for plan, key in ((self.plan, b'bad'), ({'x': True}, self.key),
                          ({**self.plan, 'case-0': 0}, self.key),
                          ({**self.plan, ' ': True}, self.key)):
            with self.assertRaises(ValueError):
                private_plan_tag(plan, key)
        with self.assertRaises(ValueError):
            verify_private_plan(self.plan, self.key, 'A' * 64)

    def test_key_sensitivity(self):
        self.assertNotEqual(private_plan_tag(self.plan, self.key),
                            private_plan_tag(self.plan, b'y' * 32))


if __name__ == '__main__':
    unittest.main()
