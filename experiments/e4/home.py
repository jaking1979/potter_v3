"""E4a offline read model: no server, credentials, stores or action handlers.
A future authenticated C7 adapter supplies detached snapshots. This module is
neither authentication nor a clinical-content detector. Only IDs/enums/times pass.
"""
from datetime import datetime, timezone
from html import escape
import json
from uuid import UUID

SCHEMA='potter.e4a-snapshot/v0'
ENUMS={
 'health': {'running','stopped','degraded','unknown'},
 'tasks': {'queued','running','waiting_approval','blocked','succeeded','failed','cancelled'},
 'activity': {'allow','deny','reconcile','error'},
 'approvals': {'pending','approved','denied','expired','cancelled'},
 'responsibilities': {'healthy','stale','blocked','unknown'},
 'recovery': {'not_required','blocked','available','unknown'}}
SECTIONS=tuple(ENUMS)


def timestamp(value):
    if type(value) is not str or len(value) not in (20,27) or not value.endswith('Z'):
        raise ValueError('time')
    fmt='%Y-%m-%dT%H:%M:%SZ' if len(value)==20 else '%Y-%m-%dT%H:%M:%S.%fZ'
    dt=datetime.strptime(value,fmt).replace(tzinfo=timezone.utc)
    if dt.strftime(fmt)!=value: raise ValueError('time')
    return dt


def section(name, raw, now):
    if type(raw) is not dict or set(raw)!={'observed_at','items'}: raise ValueError('section')
    age=(now-timestamp(raw['observed_at'])).total_seconds()
    if age<0 or type(raw['items']) is not list or len(raw['items'])>100: raise ValueError('items')
    items=[]; seen=set()
    for item in raw['items']:
        if type(item) is not dict: raise ValueError('item')
        ident=item.get('id'); status=item.get('status'); when=timestamp(item.get('updated_at'))
        if (type(ident) is not str or str(UUID(ident))!=ident or ident in seen
            or type(status) is not str or status not in ENUMS[name]
            or when>timestamp(raw['observed_at'])): raise ValueError('invalid item')
        seen.add(ident); item_age=(now-when).total_seconds()
        items.append({'id':ident,'status':status,'updated_at':item['updated_at'],
                      'age_s':int(item_age)})
    return {'availability':'available','observed_at':raw['observed_at'],
            'age_s':int(age),'stale':age>300,'items':items}


def project(snapshot, *, now):
    """Malformed/missing sections become unavailable; unknown payloads vanish.
    Item age is time since change; section age determines data freshness.
    """
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset().total_seconds()!=0:
        raise ValueError('aware UTC clock required')
    result={'schema':'potter.e4a-view/v0','generated_at':now.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'mode':'offline_read_only','sections':{}}
    for name in SECTIONS:
        try:
            if type(snapshot) is not dict or snapshot.get('schema')!=SCHEMA: raise ValueError('schema')
            result['sections'][name]=section(name,snapshot[name],now)
        except (KeyError,ValueError,TypeError,OverflowError,AttributeError):
            result['sections'][name]={'availability':'unavailable','observed_at':None,
                                     'age_s':None,'stale':True,'items':[]}
    return result


def render(view):
    # Re-project: passing a forged view must not introduce arbitrary markup.
    now=timestamp(view['generated_at']); snap={'schema':SCHEMA}
    for name in SECTIONS:
        s=view.get('sections',{}).get(name,{})
        if s.get('availability')=='available':
            snap[name]={'observed_at':s.get('observed_at'),'items':s.get('items')}
    clean=project(snap,now=now)
    out=['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         '<title>Potter Home — offline read-only preview</title>',
         '<style>body{font:1rem system-ui;max-width:60rem;margin:auto;padding:1rem;line-height:1.6}',
         'a{color:#0645ad}table{border-collapse:collapse;width:100%}th,td{text-align:left;border:1px solid #777;padding:.5rem}',
         'a:focus{outline:3px solid #000;outline-offset:3px}</style></head><body>',
         '<a href="#main">Skip to status</a><main id="main"><h1>Potter Home</h1>',
         '<p>Offline read-only preview. Approval and recovery actions are unavailable here. ',
         'Use the established authenticated channel for decisions.</p>',
         '<p>This view does not prove that a service or host is reachable.</p>',
         '<p>Snapshot generated at '+escape(clean['generated_at'])+'. This page does not update automatically; refresh to check current status.</p>']
    for name,s in clean['sections'].items():
        out.append('<section aria-labelledby="'+name+'"><h2 id="'+name+'">'+name.title()+'</h2>')
        if s['availability']=='unavailable':
            out.append('<p>Unavailable. State age unknown. Other available sections remain readable. No action is enabled.</p>')
        else:
            label='Stale at generation' if s['stale'] else 'Within five minutes at generation'
            out.append('<p>'+label+'. Snapshot age: '+str(s['age_s'])+' seconds.</p>')
            if not s['items']: out.append('<p>No items in this snapshot.</p>')
            else:
                out.append('<table><caption>'+name.title()+' status</caption><thead><tr><th scope="col">ID</th><th scope="col">Status</th><th scope="col">Last changed</th></tr></thead><tbody>')
                for item in s['items']:
                    out.append('<tr><td>'+escape(item['id'])+'</td><td>'+escape(item['status'])+'</td><td>'+str(item['age_s'])+' seconds ago</td></tr>')
                out.append('</tbody></table>')
        out.append('</section>')
    return ''.join(out)+'</main></body></html>'


def respond(method,path,snapshot,*,now):
    """Pure response formatter, NOT a network listener or auth boundary.
    All write methods and unknown paths refuse before reading snapshot or clock.
    """
    headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
             'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
    if method!='GET': return 405,{**headers,'Allow':'GET','Content-Type':'text/plain; charset=utf-8'},'Read-only surface'
    if path not in ('/','/api/status'): return 404,{**headers,'Content-Type':'text/plain; charset=utf-8'},'Not found'
    view=project(snapshot,now=now)
    if path=='/api/status': return 200,{**headers,'Content-Type':'application/json'},json.dumps(view,sort_keys=True,allow_nan=False)
    return 200,{**headers,'Content-Type':'text/html; charset=utf-8'},render(view)

