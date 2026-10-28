"""Forwarded Graycliff recaps: evidence, comparison and a separate billing sheet.

Never changes billed revenue, work quantities, payment status or email recipients.
Historical jobs are deliberately outside missing-recap tracking.
"""
from __future__ import annotations
import hashlib, io, json, re
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from pypdf import PdfReader

START_DAY = '2026-10-27'
SHEET_NAME = 'Graycliff Billing Recap Reconciliation'
COLS = ['Record Key','Job / location','Area','Week ending','Status','Our billed amount',
        'Recap amount','Difference','Code / quantity comparison','Source file','Received at','Job key']
RECAP = re.compile(r'\b(?:payroll|billing)\s+recaps?\b|\bcontractor invoices?\s*(?:&|and)\s*(?:payroll\s*)?recaps?\b', re.I)
LINE = re.compile(r'^((?:[0-9]{2,3}-)?[A-Z]{2}\d{2}[A-Z]?|257)\+?\s+.*?(\(?\$[\d,]+\.\d{2}\)?)\s*(-?[\d.]+)\s*100\.00\s*%\s*(.+)$')


def now(): return datetime.now(timezone.utc).isoformat()


def day(value):
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(value)): return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
    except (ValueError,TypeError): return ''


def initialize(db):
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS billing_recap_documents (digest TEXT PRIMARY KEY, message_key TEXT, snapshot TEXT NOT NULL, imported_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS billing_recap_messages (message_key TEXT PRIMARY KEY, snapshot TEXT NOT NULL, imported_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS billing_recap_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        c.execute('INSERT OR IGNORE INTO billing_recap_settings VALUES (?,?)',('tracking_start',START_DAY))


def candidate(message, attachments):
    return bool(RECAP.search(str(message.get('subject',''))) or any(RECAP.search(str(a.get('name',''))) for a in attachments))


def parse_pdf(data, filename):
    if len(data)>15_000_000: raise ValueError('Recap exceeds 15 MB. Original retained; no amounts assumed.')
    reader=PdfReader(io.BytesIO(data))
    if len(reader.pages)>100: raise ValueError('Recap exceeds 100 pages. Original retained; no amounts assumed.')
    text='\n'.join(p.extract_text() or '' for p in reader.pages)
    return parse_text(text,filename)


def parse_text(text,filename):
    if not re.search(r'\bFirst\s+Digital\b',text,re.I): raise ValueError('Recap does not identify First Digital. No other contractor amounts imported.')
    vendors=re.findall(r'Emp\s*#\s*(\d+)',text,re.I)
    if len(set(vendors))>1: raise ValueError('Recap contains multiple contractors. No amounts imported.')
    weeks=set()
    for m,d,y in re.findall(r'Week\s+Ending:\s*(\d{1,2})/(\d{1,2})/(\d{4})',text,re.I):
        weeks.add(date(int(y),int(m),int(d)).isoformat())
    areas=set(re.findall(r'\b(FLORENCE|COLUMBIA),\s*SC(?:\b|_)',text,re.I))
    areas.update(re.findall(r'(?mi)^\s*\d{3}\s*-\s*(FLORENCE|COLUMBIA)\s*$',text))
    if len(weeks)!=1 or len(areas)!=1: raise ValueError('Recap week or area is missing or ambiguous. Original retained for review.')
    totals=re.findall(r'(?m)^\s*Sub\s+\$([\d,]+\.\d{2})',text)
    if len(totals)!=1: raise ValueError('Recap total could not be verified. No amounts assumed.')
    expected=int(Decimal(totals[0].replace(',',''))*100)
    blocks=defaultdict(list);daily='';missed=[]
    for raw in text.splitlines():
        line=raw.strip()
        dm=re.match(r'Daily\s+No\s+(\S+)',line,re.I)
        if dm: daily=dm[1];continue
        match=LINE.match(line)
        if not match:
            if re.match(r'^(?:\d{2,3}-)?[A-Z]{2}\d{2}[A-Z]?\+?\s+',line): missed.append(line[:100])
            continue
        if not daily: raise ValueError('Recap line has no daily number. Original retained for review.')
        code,amount,quantity,location=match.groups()
        q=Decimal(quantity)
        if not q.is_finite(): raise ValueError('Invalid recap quantity.')
        value=int(Decimal(re.sub(r'[,$()]','',amount))*100)*(-1 if '(' in amount else 1)
        blocks[(daily,location.strip())].append({'code':re.sub(r'^\d+-','',code),'quantity':float(q),'amountCents':value})
    if missed or not blocks or sum(x['amountCents'] for lines in blocks.values() for x in lines)!=expected:
        raise ValueError('Recap lines do not reconcile to its printed total. Original retained; no partial total imported.')
    return {'week':next(iter(weeks)),'area':next(iter(areas)).title(),'totalCents':expected,'filename':filename,
            'records':[{'daily':daily,'location':location,'lines':lines,'amountCents':sum(x['amountCents'] for x in lines)} for (daily,location),lines in blocks.items()]}


def address(text):
    text=re.sub(r'\b(?:road|street|drive|highway|avenue|lane|court|extension|boulevard)\b',lambda m:{'road':'rd','street':'st','drive':'dr','highway':'hwy','avenue':'ave','lane':'ln','court':'ct','extension':'ext','boulevard':'blvd'}[m[0]],str(text).lower())
    text=re.sub(r'[^a-z0-9 ]',' ',text)
    # Match full numbered street, never a node or street number alone.
    found=re.search(r'\b\d{1,6}\s+(?:[a-z0-9]+\s+){0,7}(?:rd|st|dr|hwy|ave|ln|ct|blvd)(?:\s+ext)?\b',text)
    return re.sub(r'\s+',' ',found[0]).strip() if found else ''


def match_record(record, jobs):
    location=record['location'];pids=set(re.findall(r'\b(?:[FP]|PID\s*[:#-]?\s*)(\d{6,12})\b',location,re.I))
    target=address(location);matches=[]
    for job in jobs:
        title=job.get('title','');ids=set(re.findall(r'\b\d{6,12}\b',title))
        exact_pid=bool(pids & ids)
        if (exact_pid or (target and target==address(title))) and (not job.get('area') or job['area']==record['area']): matches.append(job)
    return matches[0] if len(matches)==1 else None


def quantities(lines):
    result=defaultdict(Decimal)
    for line in lines:
        code=str(line.get('code','')).upper();q=Decimal(str(line.get('quantity',0)))
        if not q.is_finite(): raise ValueError('Invalid billed quantity.')
        result[code]+=q
    return {k:v for k,v in result.items() if v}


def compare(our, recap):
    oq=quantities(our.get('lines') or []);rq=quantities(recap.get('lines') or [])
    changes=[f'{code}: billed {oq.get(code,0)}, recap {rq.get(code,0)}' for code in sorted(set(oq)|set(rq)) if oq.get(code,0)!=rq.get(code,0)]
    amount=our.get('amountCents');difference=recap['amountCents']-amount if isinstance(amount,int) else None
    if not oq: return 'Billed code detail unavailable',difference,'No sent code quantities available for comparison.'
    status='Matched' if difference==0 and not changes else 'Difference to review'
    return status,difference,'; '.join(changes) or 'Codes and quantities match.'


def current_jobs(db):
    from financial_report import report
    with db() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='billing_package_outbox'").fetchone(): return []
    # Only actual portal sends on/after activation can become missing-recap items.
    jobs=report(db)['jobs']
    return [{**j,'billedDay':day(j.get('sentAt')),'area':{'2515846899388292':'Florence','8067039099834244':'Columbia'}.get(str(j.get('sheetId')),'')}
            for j in jobs if j.get('packageId') and day(j.get('sentAt'))>=START_DAY]


def report(db, jobs=None):
    initialize(db);jobs=current_jobs(db) if jobs is None else [j for j in jobs if day(j.get('sentAt',j.get('billedDay','')))>=START_DAY]
    with db() as c:
        docs=[json.loads(r['snapshot']) for r in c.execute('SELECT snapshot FROM billing_recap_documents ORDER BY imported_at')]
        messages=[json.loads(r['snapshot']) for r in c.execute('SELECT snapshot FROM billing_recap_messages ORDER BY imported_at DESC LIMIT 40')]
        setting=c.execute('SELECT value FROM billing_recap_settings WHERE name=?',('sheet',)).fetchone()
    rows=[];matched=set();versions=defaultdict(set)
    for doc in docs:
        if doc['week']<START_DAY: continue
        groups={}
        for record in doc['records']:
            record={**record,'area':doc['area']};job=match_record(record,jobs)
            key=job['sourceKey'] if job else record['daily']+'|'+record['location']
            grouped=groups.setdefault(key,{'job':job,'location':record['location'],'lines':[],'amountCents':0,'daily':[]})
            grouped['lines']+=record['lines'];grouped['amountCents']+=record['amountCents'];grouped['daily'].append(record['daily'])
        for key,recap in groups.items():
            job=recap['job'];status,difference,detail=compare(job,recap) if job else ('Not matched to current billing',None,'No unique job billed since October 27 matches this recap location.')
            if job: matched.add(key);versions[(key,doc['week'])].add(doc['digest'])
            rows.append({'recordKey':doc['digest']+':'+hashlib.sha256(key.encode()).hexdigest()[:20],
                         'sourceKey':job['sourceKey'] if job else '', 'title':job['title'] if job else recap['location'],
                         'area':doc['area'],'week':doc['week'],'status':status,'ourAmountCents':job.get('amountCents') if job else None,
                         'recapAmountCents':recap['amountCents'],'differenceCents':difference,'detail':detail,
                         'sourceFile':doc['filename'],'receivedAt':doc['receivedAt'],'lines':recap['lines'],'daily':recap['daily']})
    for row in rows:
        if len(versions[(row['sourceKey'],row['week'])])>1:
            row['status']='Multiple recap versions';row['detail']='Different recap files cover this job and week. Review versions; amounts are not added together.'
    for job in jobs:
        if job['sourceKey'] in matched: continue
        covered=any(d['week']>=job.get('billedDay',day(job.get('sentAt'))) and d['area']==job.get('area') for d in docs if d['week']>=START_DAY)
        rows.append({'recordKey':'awaiting:'+job['sourceKey'],'sourceKey':job['sourceKey'],'title':job['title'],'area':job.get('area',''),
                     'week':'','status':'Not on received recap' if covered else 'Awaiting recap','ourAmountCents':job.get('amountCents'),
                     'recapAmountCents':None,'differenceCents':None,'detail':'Check this current bill against the received area recap.' if covered else 'No covering recap received yet.',
                     'sourceFile':'','receivedAt':''})
    # Unmatched locations can be old jobs on a mixed-period recap. Passive only.
    return {'startDay':START_DAY,'rows':rows,'messages':messages,'sheetUrl':json.loads(setting['value']).get('url','') if setting else ''}


def sheet_id(store, db):
    with db() as c: saved=c.execute('SELECT value FROM billing_recap_settings WHERE name=?',('sheet',)).fetchone()
    if saved: return json.loads(saved['value'])['id']
    sheets=[s for s in store.list_sheets() if s['name']==SHEET_NAME]
    if len(sheets)>1: raise ValueError('Duplicate recap reconciliation sheets require review.')
    if sheets: sheet=sheets[0]
    else:
        columns=[{'title':n,'type':'TEXT_NUMBER',**({'primary':True} if i==0 else {})} for i,n in enumerate(COLS)]
        sheet=store._request('POST','/sheets',body={'name':SHEET_NAME,'columns':columns},retries=1)['result']
    with db() as c: c.execute('INSERT OR REPLACE INTO billing_recap_settings VALUES (?,?)',('sheet',json.dumps({'id':sheet['id'],'url':sheet.get('permalink','')})))
    return sheet['id']


def sync_sheet(store, db):
    from operations_intake import vals, write
    result=report(db)
    if not result['rows']: return
    sid=sheet_id(store,db);sheet=store.get_sheet(sid,force=True)
    existing={str(v.get('Record Key','')):(r['id'],v) for r in sheet.get('rows',[]) if (v:=vals(sheet,r))}
    live_keys={r['recordKey'] for r in result['rows']}
    for row in result['rows']:
        value={'Record Key':row['recordKey'],'Job / location':row['title'],'Area':row['area'],'Week ending':row['week'],
               'Status':row['status'],'Our billed amount':row['ourAmountCents']/100 if row['ourAmountCents'] is not None else '',
               'Recap amount':row['recapAmountCents']/100 if row['recapAmountCents'] is not None else '',
               'Difference':row['differenceCents']/100 if row['differenceCents'] is not None else '',
               'Code / quantity comparison':row['detail'],'Source file':row['sourceFile'],'Received at':row['receivedAt'],'Job key':row['sourceKey']}
        prior=existing.get(row['recordKey'])
        if not prior or any(prior[1].get(k,'')!=v for k,v in value.items()): write(store,sid,value,prior[0] if prior else None)
    for key,(rid,value) in existing.items():
        if key.startswith('awaiting:') and key not in live_keys and value.get('Status')!='Reconciled':
            write(store,sid,{'Status':'Reconciled','Code / quantity comparison':'See the matched source recap record for this job.'},rid)


def ingest(store, db, message, attachments, message_key, token, ledger_sid):
    import graph_import as graph
    from operations_intake import capture_email_files, vals
    initialize(db)
    sender=str((message.get('from') or {}).get('emailAddress',{}).get('address','')).lower()
    if not re.fullmatch(r'[^@]+@(?:firstdigitalsc\.com|graycliffent\.com)',sender):
        raise ValueError('Forward billing recaps from your First Digital account. Original retained for review.')
    # Original email and attachments remain downloadable from its intake receipt.
    ledger=store.get_sheet(ledger_sid,force=True)
    receipts=[r for r in ledger.get('rows',[]) if str(vals(ledger,r).get('Message Key',''))==message_key]
    if len(receipts)!=1: raise RuntimeError('Recap intake receipt unavailable.')
    capture_email_files(store,token,message,ledger_sid,receipts[0]['id'],attachments,message_key)
    if day(message.get('receivedDateTime'))<START_DAY:
        return {'state':'Before recap tracking','detail':'Received before October 27. Original retained; no historical missing-recap checks.'}
    found=0;errors=[];skipped=0
    for attachment in attachments:
        file=graph.attachment_bytes(attachment)
        if not file: continue
        filename,mime,data=file
        if not filename.lower().endswith('.pdf') or not RECAP.search(filename+' '+message.get('subject','')): continue
        # A forwarded recap email can also contain another contractor's invoice.
        if not re.search(r'first[ _-]*digital|payroll[ _-]*recap|billing[ _-]*recap',filename,re.I): continue
        digest=hashlib.sha256(data).hexdigest()
        with db() as c: prior=c.execute('SELECT 1 FROM billing_recap_documents WHERE digest=?',(digest,)).fetchone()
        if prior: found+=1;continue
        try: doc=parse_pdf(data,filename)
        except Exception as exc:
            errors.append(filename+': '+(str(exc) if isinstance(exc,ValueError) else 'Document could not be read. Original retained.'));continue
        if doc['week']<START_DAY: skipped+=1;continue
        doc.update(digest=digest,receivedAt=message.get('receivedDateTime',''),subject=message.get('subject',''))
        with db() as c: c.execute('INSERT OR IGNORE INTO billing_recap_documents VALUES (?,?,?,?)',(digest,message_key,json.dumps(doc),now()))
        found+=1
    if not found and not skipped and not errors: errors=['No supported First Digital payroll recap PDF attached. Forward the original recap attachment.']
    state='Needs review' if errors else 'Recap reconciled'
    detail='; '.join(errors) if errors else f'{found} recap file(s) reconciled. {skipped} pre-October-27 file(s) retained without historical alerts.'
    with db() as c: c.execute('INSERT OR REPLACE INTO billing_recap_messages VALUES (?,?,?)',(message_key,json.dumps({'subject':message.get('subject',''),'receivedAt':message.get('receivedDateTime',''),'state':state,'detail':detail}),now()))
    sync_sheet(store,db)
    return {'state':state,'detail':detail}
