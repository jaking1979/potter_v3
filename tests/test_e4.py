import copy
from datetime import datetime,timezone,timedelta
from html.parser import HTMLParser
import json
import unittest
from experiments.e4.home import project,render,respond,ENUMS,SCHEMA
NOW=datetime(2026,11,7,0,0,tzinfo=timezone.utc)
ID='00000000-0000-0000-0000-000000000001'
def stamp(age=0): return (NOW-timedelta(seconds=age)).strftime('%Y-%m-%dT%H:%M:%SZ')
def fixture(age=0):
    return {'schema':SCHEMA,**{name:{'observed_at':stamp(age),'items':[{'id':ID,'status':sorted(values)[0],'updated_at':stamp(age)}]} for name,values in ENUMS.items()}}
class Tests(unittest.TestCase):
    def test_harness_stopped_independent_sections(self):
        s=fixture();s['health']['items'][0]['status']='stopped';v=project(s,now=NOW)
        self.assertEqual(v['sections']['health']['items'][0]['status'],'stopped')
        self.assertTrue(all(x['availability']=='available' for x in v['sections'].values()))
    def test_stale_boundary_item_age(self):
        for age,stale in ((300,False),(301,True)):
            self.assertEqual(project(fixture(age),now=NOW)['sections']['tasks']['stale'],stale)
        s=fixture();s['tasks']['items'][0]['updated_at']=stamp(301);v=project(s,now=NOW)['sections']['tasks']
        self.assertFalse(v['stale']);self.assertTrue(v['items'][0]['stale'])
    def test_malformed_missing_fail_closed(self):
        changes=(lambda s:s.pop('tasks'),lambda s:s['tasks'].update(observed_at=stamp(-1)),
          lambda s:s['tasks']['items'][0].update(status='<script>secret</script>'),
          lambda s:s['tasks']['items'][0].update(id='clinical-name'),
          lambda s:s['tasks']['items'].append(copy.deepcopy(s['tasks']['items'][0])),
          lambda s:s['tasks'].update(items=[{}]*101))
        for change in changes:
            s=fixture();change(s);v=project(s,now=NOW)
            self.assertEqual(v['sections']['tasks']['availability'],'unavailable')
            self.assertEqual(v['sections']['approvals']['availability'],'available')
    def test_payload_redaction(self):
        s=fixture();s['secret']='CANARY_SECRET';s['tasks']['items'][0].update(title='CANARY_SECRET',payload={'token':'CANARY_SECRET'},reason='CANARY_SECRET')
        self.assertNotIn('CANARY_SECRET',json.dumps(project(s,now=NOW)))
        self.assertNotIn('CANARY_SECRET',respond('GET','/',s,now=NOW)[2])
    def test_no_actions_or_unknown_paths(self):
        for method in ('POST','PUT','PATCH','DELETE','HEAD','OPTIONS'):
            self.assertEqual(respond(method,'/api/approve',None,now=None)[0],405)
        for path in ('/approve','/stop','/recover','/?approve=1','/api/status?secret=x','https://example.com'):
            self.assertEqual(respond('GET',path,None,now=None)[0],404)
    def test_response_headers(self):
        code,h,b=respond('GET','/api/status',fixture(),now=NOW)
        self.assertEqual(code,200);self.assertEqual(h['Cache-Control'],'no-store')
        self.assertIn("form-action 'none'",h['Content-Security-Policy']);self.assertEqual(json.loads(b)['mode'],'offline_read_only')
    def test_accessible_semantics_no_actions(self):
        class Parser(HTMLParser):
            def __init__(self): super().__init__();self.tags=[];self.attrs=[]
            def handle_starttag(self,t,a):self.tags.append(t);self.attrs.extend(a)
        p=Parser();p.feed(render(project(fixture(),now=NOW)))
        self.assertEqual(p.tags.count('main'),1);self.assertEqual(p.tags.count('h1'),1)
        self.assertEqual(p.tags.count('caption'),6);self.assertEqual(p.tags.count('h2'),6)
        self.assertFalse(set(p.tags)&{'button','input','form','script','iframe'})
        self.assertIn(('lang','en'),p.attrs);self.assertIn(('href','#main'),p.attrs)
    def test_invalid_clock_and_envelope(self):
        with self.assertRaises(ValueError):project(fixture(),now=NOW.replace(tzinfo=None))
        for s in (None,{}, {'schema':'wrong'}, {'schema':SCHEMA}):
            self.assertTrue(all(x['availability']=='unavailable' for x in project(s,now=NOW)['sections'].values()))
    def test_forged_projection_revalidated(self):
        v=project(fixture(),now=NOW);v['sections']['tasks']['items'][0]['status']='<script>CANARY_SECRET</script>'
        self.assertNotIn('CANARY_SECRET',render(v))
if __name__=='__main__':unittest.main()
