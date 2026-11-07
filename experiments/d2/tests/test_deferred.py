import unittest
from registry.deferred import generate, serialize, verify_generated, authorize, RegistryError

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'name':'notes.read', 'class':'read', 'enabled':True},
                     {'name':'voice.call', 'class':'voice', 'enabled':False}]

    def test_deferred_and_unknown_denied(self):
        r = generate(self.rows, ['voice'])
        self.assertTrue(authorize(r, 'notes.read'))
        self.assertFalse(authorize(r, 'voice.call'))
        self.assertFalse(authorize(r, 'notes.write'))

    def test_source_cannot_enable_deferred(self):
        self.rows[1]['enabled'] = True
        with self.assertRaises(RegistryError): generate(self.rows, ['voice'])

    def test_drift_and_determinism(self):
        r = generate(self.rows, ['voice'])
        self.assertEqual(serialize(r), serialize(generate(list(reversed(self.rows)), ['voice'])))
        verify_generated(r, serialize(r))
        with self.assertRaises(RegistryError): verify_generated(r, b'[]')

    def test_bad_rows_fail_closed(self):
        for rows in (self.rows + [self.rows[0]], [{'name':'x','class':'voice','enabled':1}],
                     [{'name':'x','class':'voice','enabled':False,'extra':True}]):
            with self.subTest(rows=rows), self.assertRaises(RegistryError): generate(rows, ['voice'])

if __name__ == '__main__': unittest.main()
