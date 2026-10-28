"""Private commercial intake. No operational rows, customer emails or sent amounts change."""
import base64,hashlib,io,json,re,urllib.parse,os
from datetime import date,datetime,timezone
from decimal import Decimal,ROUND_HALF_UP
from pathlib import Path
from pypdf import PdfReader

RATE_PATH=Path(os.getenv('KINETIC_RATES_PATH',str(Path(os.getenv('DATA_PATH','/var/data/graycliff.db')).parent/'kinetic_rates.json')))
RATES=json.loads(RATE_PATH.read_text()) if RATE_PATH.exists() else {'revenue':{},'costs':{}}
def norm(code):return re.sub(r'\s+','',str(code).upper()).replace('"','IN')
REV={norm(k):v for k,v in RATES['revenue'].items()}
COST={norm(k):v for k,v in RATES['costs'].items()}
def money(v):return int((Decimal(str(v).replace(',',''))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def now():return datetime.now(timezone.utc).isoformat()
def initialize(db):
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS private_commercial_documents (digest TEXT PRIMARY KEY, kind TEXT, snapshot TEXT NOT NULL, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS private_commercial_messages (message_key TEXT PRIMARY KEY, mime BLOB NOT NULL, mime_hash TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS private_commercial_settings (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
def candidate(message,attachments):
    if re.search(r'remittance\s+advice',message.get('subject',''),re.I):return True
    import graph_import as graph
    for name,text in graph._candidate_document_texts(attachments):
        if re.search(r'Week Ending:',text,re.I) and any(norm(line.split()[0]) in REV for line in text.splitlines() if line.split()):return True
    return False
def invoice(text):
    if not re.search(r'First\s+Digital',text,re.I) or not re.search(r'Graycliff',text,re.I):raise ValueError('Contractor or payer not verified')
    week=re.search(r'Week Ending:\s*(\d+)/(\d+)/(\d+)',text,re.I)
    project=re.search(r'Job\s*#\s*:\s*([A-Z0-9]+)',text,re.I)
    total=re.findall(r'(?m)^\s*Total:\s*\$([\d,]+\.\d{2})',text)
    if not week or not project or len(total)!=1:raise ValueError('Invoice identity or total needs review')
    m,d,y=map(int,week.groups()); week=date(y,m,d).isoformat(); lines=[]; unmatched=[]
    text=re.sub(r'(?m)^(\S+[^\n]*?)\n([A-Z ]+)\n(?=\d+\s+\$)',r'\1 \2 ',text)
    for raw in text.splitlines():
        match=re.match(r'^(\S+)\s+(.+?)\s+(-?[\d,.]+)\s+\$(-?[\d,]+\.\d{2})\s*\$([\d,]+\.\d+)\s*$',raw.strip())
        if not match:continue
        code,description,qty,amount,rate=match.groups(); key=norm(code); candidates=[k for k in REV if k==key]
        # Some invoice exports visibly truncate the code column. A unique
        # code AND matching customer rate are required to expand it.
        if not candidates:candidates=[k for k,v in REV.items() if k.startswith(key) and money(v['rate'])==money(rate)]
        if not candidates:candidates=[k for k in REV if k.startswith(key)]
        if not candidates:unmatched.append(code);continue
        key=candidates[0] if len(candidates)==1 else code; q=Decimal(qty.replace(',','')); actual=money(amount)
        rates={Decimal(REV[k]['rate']) for k in candidates};expected=money(q*next(iter(rates))) if len(rates)==1 else None
        cost=COST.get(key) if len(candidates)==1 else None; estimate=money(q*Decimal(cost['rate'])) if cost else None
        lines.append({'code':key,'codeCandidates':candidates,'quantity':str(q),'amountCents':actual,'rateCents':money(rate),'rateCardAmountCents':expected,'subCostEstimateCents':estimate,'subRateCents':money(cost['rate']) if cost else None,'description':description})
    if not lines or unmatched or sum(x['amountCents'] for x in lines)!=money(total[0]):raise ValueError('Invoice codes or signed total need review; no totals assumed')
    return {'kind':'invoice','business':'Kinetic','projectCode':project.group(1).upper(),'week':week,'amountCents':money(total[0]),'lines':lines}
def remittance(text):
    if not re.search(r'First\s+Digital',text,re.I) or not re.search(r'Graycliff',text,re.I):raise ValueError('Payee or payer not verified')
    payment=re.search(r'(?m)^\s*(E\d+)\s*$',text)
    projects=re.findall(r'(?m)^\s*(\d{3}[A-Z0-9]{4,})\s*$',text)
    amounts=re.findall(r'(?m)^\s*0\.00\s+(-?[\d,]+\.\d{2})\s+0\.00\s*$',text)
    page=text.split('Electronic Payment')[0]
    refs=re.findall(r'(?m)^\s*((?:CB\s+)?[A-Z]+-\d{6})\s*$',page)
    dates=re.findall(r'(?m)^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*$',text)
    total=re.search(r'Payment Totals\s+0\.00\s+\d{1,2}/\d{1,2}/\d{2,4}\s+([\s\S]+?)Inv\. Date',text)
    if not payment or not projects or len(projects)!=len(amounts) or len(refs)!=len(projects) or not dates or not total:raise ValueError('Remittance row mapping needs review')
    totals=re.findall(r'-?[\d,]+\.\d{2}',total.group(1)); expected=money(totals[-1]) if totals else None
    if sum(money(v) for v in amounts)!=expected:raise ValueError('Remittance does not reconcile')
    return {'kind':'remittance','paymentNumber':payment.group(1),'paymentDate':dates[-1],'amountCents':expected,'lines':[{'projectCode':p,'invoiceRef':r,'amountCents':money(a)} for p,r,a in zip(projects,refs,amounts)]}
def save_text(db,text,filename,digest,source=None):
    initialize(db)
    with db() as c:
        if c.execute('SELECT 1 FROM private_commercial_documents WHERE digest=?',(digest,)).fetchone():return True
    try:doc=remittance(text) if re.search(r'Remittance Advice|Remittance - Advice',text,re.I) else invoice(text)
    except ValueError as exc:doc={'kind':'review','reason':str(exc),'text':text}
    doc.update(filename=filename,source=source or {},digest=digest)
    with db() as c:c.execute('INSERT OR IGNORE INTO private_commercial_documents VALUES (?,?,?,?)',(digest,doc['kind'],json.dumps(doc),now()))
    return doc['kind']!='review'
def ingest(db,message,attachments,key,token):
    import graph_import as graph
    initialize(db)
    sender=(message.get('from') or {}).get('emailAddress',{}).get('address','').lower()
    if sender not in ('thomas.bramlette@firstdigitalsc.com','lwarner@graycliffent.com'):return {'state':'Needs review','detail':'Commercial sender requires owner review.'}
    raw=graph.get_message_mime(token,message['id'])
    if not raw or len(raw)>25_000_000:raise ValueError('Commercial source exceeds retention limit')
    digest=hashlib.sha256(raw).hexdigest()
    with db() as c:
        c.execute('INSERT OR IGNORE INTO private_commercial_messages VALUES (?,?,?,?,?,?)',(key,raw,digest,'Retained','Private original retained; parsing pending',now()))
    count=0;verified=True
    for a in attachments:
        f=graph.attachment_bytes(a)
        if not f:continue
        filename,mime,data=f
        if not filename.lower().endswith('.pdf'):continue
        count+=1
        reader=PdfReader(io.BytesIO(data))
        if len(reader.pages)>100:verified=False;continue
        text='\n'.join(p.extract_text() or '' for p in reader.pages)
        verified=save_text(db,text,filename,hashlib.sha256(data).hexdigest(),{'receivedAt':message.get('receivedDateTime',''),'messageKey':key}) and verified
    state='Imported' if count and verified else 'Needs review'
    with db() as c:c.execute('UPDATE private_commercial_messages SET state=?, detail=? WHERE message_key=?',(state,'Private source retained and commercial analysis updated' if state=='Imported' else 'Private source retained; document requires owner review',key))
    with db() as c:cleanup=c.execute("SELECT value FROM private_commercial_settings WHERE name='cleanup_enabled'").fetchone()
    if state=='Imported' and sender=='thomas.bramlette@firstdigitalsc.com' and cleanup and cleanup['value']=='yes':
        # Verify durable private bytes before removing the shared forwarded copy.
        with db() as c:saved=c.execute('SELECT mime,mime_hash FROM private_commercial_messages WHERE message_key=?',(key,)).fetchone()
        if not saved or hashlib.sha256(saved['mime']).hexdigest()!=saved['mime_hash']:raise RuntimeError('Private retention verification failed')
        if graph.mailbox()!='graycliffjobs@firstdigitalsc.com':raise RuntimeError('Shared mailbox safety check failed')
        root=graph.GRAPH_ROOT+'/users/'+urllib.parse.quote(graph.mailbox(),safe='')+'/messages/'+urllib.parse.quote(message['id'],safe='')
        try:
            graph._request('POST',root+'/permanentDelete',token=token)
            with db() as c:c.execute("UPDATE private_commercial_messages SET state='Imported and removed' WHERE message_key=?",(key,))
        except Exception:
            with db() as c:c.execute("UPDATE private_commercial_messages SET detail='Import verified; shared copy removal requires attention' WHERE message_key=?",(key,))
            return {'state':'Removal needs attention','detail':'Private import verified; shared copy remains.'}
    return {'state':state,'detail':'Commercial document retained privately.'}
def report(db):
    initialize(db)
    with db() as c:docs=[json.loads(r['snapshot']) for r in c.execute('SELECT snapshot FROM private_commercial_documents')]
    invoices=[d for d in docs if d['kind']=='invoice']; known={d['projectCode'] for d in invoices}; receipts=[];unallocated=[]
    for d in docs:
        if d['kind']!='remittance':continue
        for i,line in enumerate(d['lines']):
            item={**line,'paymentNumber':d['paymentNumber'],'paymentDate':d['paymentDate'],'sourceFile':d['filename'],'key':d['digest']+':'+str(i)}
            (receipts if line['projectCode'] in known else unallocated).append(item)
    # Repeated PDF exports with the same payment identity are counted once.
    by_payment={};conflicts=[]
    for r in receipts:
        key=(r['paymentNumber'],r['invoiceRef'],r['projectCode'])
        if key in by_payment and by_payment[key]['amountCents']!=r['amountCents']:conflicts.append(key)
        else:by_payment[key]=r
    receipts=[r for key,r in by_payment.items() if key not in conflicts]
    # Same job/week invoice revisions are review items, never additive revenue.
    grouped={}
    for d in invoices:grouped.setdefault((d['projectCode'],d['week']),[]).append(d)
    unique=[v[0] for v in grouped.values() if len(v)==1]
    return {'invoices':unique,'receipts':receipts,'review':[{'filename':d['filename'],'reason':d['reason']} for d in docs if d['kind']=='review']+[{'filename':v[0]['filename'],'reason':'Multiple invoice versions for '+str(k)} for k,v in grouped.items() if len(v)>1], 'unallocatedReceipts':unallocated,'currency':'USD','revenueCents':sum(d['amountCents'] for d in unique),'receiptsCents':sum(d['amountCents'] for d in receipts),'subCostEstimateCents':sum(l['subCostEstimateCents'] or 0 for d in unique for l in d['lines']),'unpricedCostLines':sum(l['subCostEstimateCents'] is None for d in unique for l in d['lines']),'note':'Kinetic only. Customer invoice amounts and cash receipts are separate. Subcontractor costs are rate-card estimates, not paid costs. Revenue excludes conflicting invoice versions. Remittance allocation uses project codes established by invoices whose work codes match the Kinetic rate card; location is never a classifier.'}
