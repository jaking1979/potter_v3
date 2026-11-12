import json
import subprocess
import unittest
from pathlib import Path

from evaluation.a4a.pair_identity import pair_identity, require_same_pair

class PairIdentityParity(unittest.TestCase):
    def test_node_python_parity_and_boundaries(self):
        path = Path(__file__).with_name('pair_identity.mjs').as_uri()
        script = """import {pairIdentity} from %s;
const cases = [['case-1','v1','',''], ['café','v2','00ff10','f09f8c9e'], ['a','bc','004142','ff']];
for (const [c,v,a,b] of cases) console.log(pairIdentity(c,v,Buffer.from(a,'hex'),Buffer.from(b,'hex')));""" % json.dumps(path)
        actual = subprocess.check_output(['node','--input-type=module','-e',script], text=True).splitlines()
        expected = [pair_identity(c,v,bytes.fromhex(a),bytes.fromhex(b)) for c,v,a,b in [
            ('case-1','v1','',''), ('café','v2','00ff10','f09f8c9e'), ('a','bc','004142','ff')]]
        self.assertEqual(actual,expected)
        self.assertNotEqual(pair_identity('a','bc',b'AB',b'C'), pair_identity('ab','c',b'A',b'BC'))
        self.assertNotEqual(pair_identity('x','1',b'A',b'B'), pair_identity('x','1',b'B',b'A'))

    def test_refuse_invalid_identity(self):
        for first,second in [('', ''), ('A'*64, 'A'*64), ('a'*64, 'b'*64), (None, None)]:
            with self.assertRaises(ValueError): require_same_pair(first, second)
        require_same_pair('a'*64, 'a'*64)
        for args in [('', 'v1', b'a', b'b'), ('x','v1','a',b'b'), ('x','v1',b'a',bytearray(b'b'))]:
            with self.assertRaises((TypeError,ValueError)): pair_identity(*args)

if __name__ == '__main__': unittest.main()
