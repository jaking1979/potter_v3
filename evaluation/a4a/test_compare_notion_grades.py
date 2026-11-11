import unittest
from evaluation.a4a.compare_notion_grades import compare

def row():
    r={'Task':'synthetic','Scenario type':'One-shot'}
    for label in ('A','B'):
        r.update({f'{label} · Done':'Yes',f'{label} · Re-explaining':'None',
                  f'{label} · Fast enough':'Yes',f'{label} · Trust':'Somewhat'})
    return r

class BoundNotionComparisonTests(unittest.TestCase):
    def test_agreement_and_disagreement(self):
        a=row();b=row();b['B · Done']='Partly'
        r=compare(a,'a'*64,b,'a'*64)
        self.assertEqual((r['comparisons'],r['agreements']),(8,7))
        self.assertEqual(r['disagreements'],[{'label':'B','dimension':'done','first':'yes','second':'partly'}])
    def test_changed_pair_case_or_unknown_data_refused(self):
        a=row();b=row()
        for pair in (None,'b'*64,'A'*64):
            with self.subTest(pair=pair),self.assertRaises(ValueError): compare(a,'a'*64,b,pair)
        b['Task']='another'
        with self.assertRaises(ValueError): compare(a,'a'*64,b,'a'*64)
        b=row();b['Output A']='private'
        with self.assertRaises(ValueError): compare(a,'a'*64,b,'a'*64)
    def test_responsibility_includes_follow_through(self):
        a=row();b=row()
        for r in (a,b):
            r['Scenario type']='Responsibility'
            r['A · Follow-through']=r['B · Follow-through']='Yes'
        self.assertEqual(compare(a,'a'*64,b,'a'*64)['comparisons'],10)

if __name__ == '__main__': unittest.main()
