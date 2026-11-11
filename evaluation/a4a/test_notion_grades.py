import unittest
from evaluation.a4a.notion_grades import convert

def row():
    x={'Task':'synthetic-1','Scenario type':'One-shot'}
    for label in ('A','B'):
        x.update({f'{label} · Done':'Yes', f'{label} · Re-explaining':'None',
                  f'{label} · Fast enough':'Slow', f'{label} · Trust':'Somewhat'})
    return x

class NotionGradeTests(unittest.TestCase):
    def test_valid_selects_convert(self):
        r=convert(row())
        self.assertEqual(r['outputs']['A'],{'done':'yes','reexplaining':'none','speed':'slow','trust':'somewhat'})
        self.assertFalse(r['responsibility'])
    def test_responsibility_requires_both_follow_through(self):
        r=row();r['Scenario type']='Responsibility'
        with self.assertRaises(ValueError): convert(r)
        for label in ('A','B'): r[f'{label} · Follow-through']='Partly'
        self.assertEqual(convert(r)['outputs']['B']['follow_through'],'partly')
    def test_incomplete_invalid_and_sensitive_extra_fields_are_rejected(self):
        for key,value in [('A · Done',None),('B · Trust','Maybe'),('Scenario type','unknown'),('Output A','private')]:
            r=row();r[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError): convert(r)
        r=row();del r['B · Done']
        with self.assertRaises(ValueError): convert(r)

if __name__ == '__main__': unittest.main()
