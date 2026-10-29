"""Owner-only processing receipts and collections actions; no bank assertions."""
import json,time,hashlib
from datetime import datetime,timezone
from email.parser import BytesHeaderParser
from email import policy


def initialize(db):
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS financial_processing (message_key TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, received_at TEXT NOT NULL, updated_at REAL NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS financial_monitor (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        c.execute("INSERT OR IGNORE INTO financial_monitor VALUES ('started',?)",(json.dumps(time.time()),))


def record(db,key,kind,state,detail='',subject='',received=''):
    initialize(db)
    with db() as c:
        c.execute('''INSERT INTO financial_processing VALUES (?,?,?,?,?,?,?) ON CONFLICT(message_key) DO UPDATE SET
          updated_at=excluded.updated_at,state=excluded.state,detail=excluded.detail,
          subject=CASE WHEN excluded.subject!='' THEN excluded.subject ELSE financial_processing.subject END,
          received_at=CASE WHEN excluded.received_at!='' THEN excluded.received_at ELSE financial_processing.received_at END''',
          (key,kind,subject,received,time.time(),state,detail))


def monitor(db,ok,detail=''):
    initialize(db)
    with db() as c:c.execute("INSERT OR REPLACE INTO financial_monitor VALUES ('inbox',?)",(json.dumps({'checkedAt':time.time(),'ok':ok,'detail':detail}),))


def processing(db,now=None):
    initialize(db);now=time.time() if now is None else now
    with db() as c:
        events={r['message_key']:dict(r) for r in c.execute('SELECT * FROM financial_processing ORDER BY updated_at DESC LIMIT 100')}
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'private_commercial_messages' in tables:
            # Backfill only MIME headers for older archived messages; never load
            # attachments or run OCR in an owner dashboard request.
            for old in c.execute('''SELECT m.message_key,substr(m.mime,1,16384) headers,m.created_at
                FROM private_commercial_messages m LEFT JOIN financial_processing p ON p.message_key=m.message_key
                WHERE p.message_key IS NULL ORDER BY m.created_at DESC LIMIT 100''').fetchall():
                header=BytesHeaderParser(policy=policy.default).parsebytes(old['headers'])
                item={'message_key':old['message_key'],'kind':'Financial PDF','subject':str(header.get('Subject','Saved financial email')),
                      'received_at':old['created_at'],'updated_at':0,'state':'Received','detail':'Private original retained.'}
                events[old['message_key']]=item
                c.execute('INSERT OR IGNORE INTO financial_processing VALUES (?,?,?,?,?,?,?)',tuple(item[k] for k in ('message_key','kind','subject','received_at','updated_at','state','detail')))
            for r in c.execute('''SELECT m.message_key,m.state,m.detail,m.created_at,j.state job_state,j.attempts,j.last_attempt
              FROM private_commercial_messages m LEFT JOIN private_commercial_jobs j ON j.message_key=m.message_key ORDER BY m.created_at DESC LIMIT 100'''):
                key=r['message_key'];job=r['job_state'];attempts=r['attempts'] or 0
                state=('Completed' if job=='Complete' else 'Needs attention' if job=='Retry' and attempts>=3 else 'Retrying' if job=='Retry' else 'Processing' if job=='Reading' else 'Received' if job=='Queued' else 'Completed')
                if 'removal requires attention' in r['detail'].lower():state='Needs attention'
                item=events.setdefault(key,{'message_key':key,'kind':'Financial PDF','subject':'Saved financial email','received_at':r['created_at'],'updated_at':r['last_attempt'] or 0})
                intake_error=item.get('state')=='Needs attention' and item.get('detail','').startswith('Private document intake')
                intake_detail=item.get('detail','')
                item.update(state=state,detail=r['detail'],attempts=attempts,sourceKey=key)
                if intake_error:item.update(state='Needs attention',detail=intake_detail)
                if job=='Reading' and now-(r['last_attempt'] or now)>900:item.update(state='Needs attention',detail='PDF reader was interrupted; original is retained privately. Automatic recovery is pending.')
        saved=c.execute("SELECT value FROM financial_monitor WHERE name='inbox'").fetchone()
        started=json.loads(c.execute("SELECT value FROM financial_monitor WHERE name='started'").fetchone()[0])
    health=json.loads(saved[0]) if saved else None
    rows=sorted(events.values(),key=lambda r:(r.get('received_at',''),r['updated_at']),reverse=True)[:100]
    for row in rows:
        if row.get('kind')=='Issued billing':row['sourceKey']=row['message_key']
        if row['state']=='Processing' and now-row['updated_at']>900:row.update(state='Needs attention',detail='Processing stopped reporting progress. Original retained; check before retrying.')
    problems=[r for r in rows if r['state']=='Needs attention']
    stale=now-(health['checkedAt'] if health else started)>300
    health_error=stale or bool(health and not health['ok'])
    return {'rows':rows,'attentionCount':len(problems)+int(health_error),'activeCount':sum(r['state'] in ('Received','Processing','Retrying') for r in rows),
            'completedCount':sum(r['state']=='Completed' for r in rows),'monitor':health,'monitorStale':stale,
            'monitorIssue':'Inbox checks have not reported in over five minutes.' if stale else (health['detail'] if health_error else '')}


def collections(report,business,recaps=None):
    tracking=report.get('tracking') or {};items=[]
    invoices={(d['projectCode'],d['week']):d for d in report.get('invoices',[])}
    for w in tracking.get('weeks',[]):
        difference=w['differenceCents'];receipts=w.get('remittances',[])
        if not w.get('flagged'):continue
        if difference>0 and receipts:kind='Short remittance';action='Ask Graycliff to explain the shortage against this billing week.'
        elif difference>0 and w.get('overdueDays'):kind='Missing remittance';action='Request the remittance notice for this billing week.'
        elif difference<0:kind='Remittance exceeds billing';action='Check credits, adjustments and the billed reference.'
        else:continue
        doc=invoices.get((w['projectCode'],w['week']),{});source=doc.get('source') or {}
        items.append({'kind':kind,'business':business,'area':w.get('area',''),'projectCode':w['projectCode'],'week':w['week'],
          'billedCents':w['amountCents'],'reportedCents':w['reportedCents'],'differenceCents':difference,'outstandingCents':max(0,difference),
          'expectedDate':w.get('expectedDate'),'expectedWindowEnd':w.get('expectedEnd'),'overdueDays':w.get('overdueDays',0),
          'sources':list(dict.fromkeys([doc.get('filename','')]+[r.get('sourceFile','') for r in receipts])),
          'sourceKey':source.get('privateMessageKey'),'references':[r.get('paymentNumber','')+' · '+r.get('invoiceRef','') for r in receipts],
          'detail':w['status'],'nextAction':action})
    for review in report.get('review',[]):
        items.append({'kind':'Billing source discrepancy','business':business,'area':'','projectCode':'','week':'','outstandingCents':None,
          'sources':[review['filename']],'detail':review['reason'],'nextAction':'Resolve the conflicting or incomplete source document before using its total.'})
    for r in tracking.get('unmatchedRemittances',[]):
        items.append({'kind':'Remittance needs billing evidence','business':business,'area':'','projectCode':r['projectCode'],'week':'',
          'outstandingCents':None,'reportedCents':r['amountCents'],'sources':[r.get('sourceFile','')],
          'references':[r.get('paymentNumber','')+' · '+r.get('invoiceRef','')],
          'detail':'A remittance exists, but a unique matching customer bill has not been established. This is not an unpaid-bill flag.',
          'nextAction':'Locate the customer billing or recap named on this notice.'})
    for r in (recaps or {}).get('rows',[]):
        if r.get('status') not in ('Difference to review','Multiple recap versions','Billed code detail unavailable','Not on received recap'):continue
        items.append({'kind':'Billing recap discrepancy','business':business,'area':r.get('area',''),'projectCode':'','week':r.get('week',''),
          'billedCents':r.get('ourAmountCents'),'reportedCents':r.get('recapAmountCents'),'differenceCents':r.get('differenceCents'),'outstandingCents':None,
          'sources':[r.get('sourceFile','')],'detail':r.get('title','')+' · '+r.get('detail',''),'nextAction':'Compare the issued billing codes and quantities with the customer recap.'})
    for item in items:
        item['id']=hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest()[:20]
        item['sources']=[s for s in item['sources'] if s]
    items.sort(key=lambda i:(i['outstandingCents'] is None,-(i.get('overdueDays') or 0),i.get('week','')))
    return {'items':items,'actionCount':sum(i['kind']!='Remittance needs billing evidence' for i in items),
            'evidenceGapCount':sum(i['kind']=='Remittance needs billing evidence' for i in items),
            'outstandingCents':sum(i['outstandingCents'] or 0 for i in items),'asOf':tracking.get('asOf')}
