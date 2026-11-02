import unittest
from claims import attestation_bytes

def fixture():
    e={'commit':'a'*40,'tree':'b'*40,'manifest':'c'*64,'evidence':'d'*64}
    return {**e,'role':'reviewer','verdict':'approve','nonce':'e'*64,'expires_at':101},e

class AttestationEncodingTests(unittest.TestCase):
    def test_encoding_is_order_independent(self):
        c,e=fixture()
        self.assertEqual(attestation_bytes(c,e,100),attestation_bytes(dict(reversed(list(c.items()))),e,100))
    def test_each_candidate_change_is_refused(self):
        c,e=fixture()
        for key in e:
            wrong=dict(e);wrong[key]='f'*len(e[key])
            with self.subTest(key=key), self.assertRaises(ValueError): attestation_bytes(c,wrong,100)
    def test_expired_rejected_and_boolean_time_rejected(self):
        c,e=fixture()
        for exp in (100,99,True,'101'):
            with self.subTest(exp=exp), self.assertRaises(ValueError): attestation_bytes({**c,'expires_at':exp},e,100)
    def test_wrong_shapes_roles_and_values_fail(self):
        c,e=fixture()
        for update in ({'role':'other'},{'verdict':'reject'},{'nonce':'xyz'},{'commit':'A'*40},{'extra':'x'}):
            with self.subTest(update=update), self.assertRaises(ValueError): attestation_bytes({**c,**update},e,100)
        with self.assertRaises(ValueError): attestation_bytes(c,{},100)

if __name__ == '__main__': unittest.main()
