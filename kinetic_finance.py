"""Private commercial intake. No operational rows, customer emails or sent amounts change."""
import base64,hashlib,io,json,re,urllib.parse,os,threading,time
from email import policy
from email.parser import BytesParser
from datetime import date,datetime,timezone
from decimal import Decimal,ROUND_HALF_UP
from pathlib import Path
from pypdf import PdfReader

RATE_PATH=Path(os.getenv('KINETIC_RATES_PATH',str(Path(os.getenv('DATA_PATH','/var/data/graycliff.db')).parent/'kinetic_rates.json')))
RATES=json.loads(RATE_PATH.read_text()) if RATE_PATH.exists() else {'revenue':{},'costs':{}}
def norm(code):return re.sub(r'\s+','',str(code).upper()).replace('"','IN')
REV={norm(k):v for k,v in RATES['revenue'].items()}
COST={norm(k):v for k,v in RATES['costs'].items()}
PROCESSING=threading.Lock()
def money(v):return int((Decimal(str(v).replace(',',''))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def now():return datetime.now(timezone.utc).isoformat()
def initialize(db):
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS private_commercial_documents (digest TEXT PRIMARY KEY, kind TEXT, snapshot TEXT NOT NULL, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS private_commercial_messages (message_key TEXT PRIMARY KEY, mime BLOB NOT NULL, mime_hash TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS private_commercial_settings (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS private_commercial_jobs (message_key TEXT PRIMARY KEY,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,last_attempt REAL NOT NULL DEFAULT 0)')
def candidate(message,attachments):
    subject=message.get('subject') or ''
    if re.search(r'remittance\s*(?:-\s*)?advice',subject,re.I):return True
    # Identify financial packets before text extraction: scans have no text
    # layer and must never fall through into shared operational attachments.
    for a in attachments:
        name=str(a.get('name') or '')
        label=subject+' '+name
        if name.lower().endswith('.pdf') and (
            re.search(r'remittance|payroll.*recap|subcon.*invoice',label,re.I)
            or (re.search(r'first\s*digi(?:tal|tial)|kinetic',label,re.I)
                and re.search(r'invoice|billing|recap|week[ ._-]*ending|w[ ./_-]*e\b',label,re.I))
        ):return True
    import graph_import as graph
    for name,text in graph._candidate_document_texts(attachments):
        if re.search(r'Week Ending:',text,re.I) and any(norm(line.split()[0]) in REV for line in text.splitlines() if line.split()):return True
    return False
def invoice(text):
    if re.search(r'Payroll Recaps',text,re.I):return recap(text)
    if not re.search(r'First\s+Digital',text,re.I) or not re.search(r'Graycliff',text,re.I):raise ValueError('Contractor or payer not verified')
    week=re.search(r'Week Ending:\s*(\d+)/(\d+)/(\d+)',text,re.I)
    project=re.search(r'Job[ \t]*#[ \t]*:[ \t]*([A-Z0-9]+)',text,re.I)
    if not project:
        project=re.search(r'(?m)^[ \t]*(\d{3}[A-Z][A-Z0-9]{3,})[ \t]*$',text)
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
def recap(text):
    if not re.search(r'First\s+Digital',text,re.I):raise ValueError('Contractor not verified')
    weeks=set(re.findall(r'Week Ending:\s*(\d+)/(\d+)/(\d+)',text,re.I))
    codes=set(re.findall(r'(?m)^\s*(\d{3}-[A-Z0-9]+)\s*$',text))
    totals=re.findall(r'(?mi)^\s*Sub\s+\$([\d,]+\.\d{2})',text)
    if len(weeks)!=1 or len(codes)!=1 or len(totals)!=1:raise ValueError('Recap identity or total needs review')
    m,d,y=map(int,next(iter(weeks)));lines=[]
    text=re.sub(r'\n[ \t]*DEPTH[ \t]*\n',' DEPTH ',text)
    for prefix,amount,qty,location in re.findall(r'(?m)^(.+?)\s+(\(?\$[\d,]+\.\d{2}\)?)(-?[\d.]+)100\.00%(.+)$',text):
        compact=norm(prefix);matches=[key for key in REV if compact.startswith(key)]
        if not matches:raise ValueError('Recap includes an unrecognized work code')
        longest=max(map(len,matches));matches=[key for key in matches if len(key)==longest]
        if len(matches)!=1:raise ValueError('Recap code is ambiguous')
        key=matches[0];q=Decimal(qty);actual=money(re.sub(r'[\$()]','',amount))*(-1 if '(' in amount else 1);cost=COST.get(key)
        lines.append({'code':key,'codeCandidates':[key],'quantity':str(q),'amountCents':actual,'rateCardAmountCents':money(q*Decimal(REV[key]['rate'])),'subCostEstimateCents':money(q*Decimal(cost['rate'])) if cost else None,'description':prefix,'location':location})
    if not lines or sum(l['amountCents'] for l in lines)!=money(totals[0]):raise ValueError('Recap signed detail total does not reconcile')
    return {'kind':'invoice','sourceType':'recap','business':'Kinetic','projectCode':next(iter(codes)).replace('-',''),'week':date(y,m,d).isoformat(),'amountCents':money(totals[0]),'lines':lines}
def remittance(text):
    if not re.search(r'First\s+Digital',text,re.I) or not re.search(r'Graycliff',text,re.I):raise ValueError('Payee or payer not verified')
    payment=re.search(r'(?m)^\s*(E\d+)\s*$',text)
    page=text.split('Electronic Payment')[0]
    triples=list(re.finditer(r'\b0\.00[ \t]+(-?[\d,]+\.\d{2})[ \t]+0\.00\b',page))
    projects=re.findall(r'\b(\d{3}[A-Z0-9]{4,})\b',page[payment.end():triples[0].start()]) if payment and triples else []
    amounts=[m.group(1) for m in triples]
    refs=[line.strip() for line in page[triples[-1].end():].splitlines() if line.strip()] if triples else []
    if any(not re.fullmatch(r'(?:CB\s+)?[A-Z0-9]+(?:[- /]\d+)?',r,re.I) for r in refs):raise ValueError('Remittance references need review')
    dates=re.findall(r'(?m)^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*$',text)
    total=re.search(r'Payment Totals\s+0\.00\s+\d{1,2}/\d{1,2}/\d{2,4}\s+([\s\S]+?)Inv\. Date',text)
    if not payment or not projects or len(projects)!=len(amounts) or len(refs)!=len(projects) or not dates or not total:raise ValueError('Remittance row mapping needs review')
    totals=re.findall(r'-?[\d,]+\.\d{2}',total.group(1)); expected=money(totals[-1]) if totals else None
    if sum(money(v) for v in amounts)!=expected:raise ValueError('Remittance does not reconcile')
    return {'kind':'remittance','paymentNumber':payment.group(1),'paymentDate':dates[-1],'amountCents':expected,'lines':[{'projectCode':p,'invoiceRef':r,'amountCents':money(a)} for p,r,a in zip(projects,refs,amounts)]}
def save_text(db,text,filename,digest,source=None):
    initialize(db)
    with db() as c:
        prior=c.execute('SELECT kind FROM private_commercial_documents WHERE digest=?',(digest,)).fetchone()
        if prior and prior['kind']!='review':return True
    try:doc=remittance(text) if re.search(r'Remittance Advice|Remittance - Advice',text,re.I) else invoice(text)
    except ValueError as exc:
        scan=not text.strip()
        doc={'kind':'review','reason':'Scanned PDF: visual review required; excluded from totals until verified' if scan else str(exc),'text':text,'classificationPending':scan}
    doc.update(filename=filename,source=source or {},digest=digest)
    with db() as c:c.execute('INSERT INTO private_commercial_documents VALUES (?,?,?,?) ON CONFLICT(digest) DO UPDATE SET kind=excluded.kind,snapshot=excluded.snapshot WHERE private_commercial_documents.kind=\'review\'',(digest,doc['kind'],json.dumps(doc),now()))
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
    # The durable complete MIME is the source of truth. OCR runs independently,
    # so a slow scan never blocks archiving the next shared forwarded copy.
    with db() as c:
        c.execute("INSERT OR IGNORE INTO private_commercial_jobs (message_key,state) VALUES (?,'Queued')",(key,))
        saved=c.execute('SELECT mime,mime_hash FROM private_commercial_messages WHERE message_key=?',(key,)).fetchone()
    if not saved or hashlib.sha256(saved['mime']).hexdigest()!=saved['mime_hash']:raise RuntimeError('Private retention verification failed')
    retained=BytesParser(policy=policy.default).parsebytes(saved['mime'])
    pdfs={str(p.get_filename() or '').casefold():p.get_payload(decode=True) for p in retained.walk() if str(p.get_filename() or '').lower().endswith('.pdf')}
    expected=[str(a.get('name')).casefold() for a in attachments if str(a.get('name') or '').lower().endswith('.pdf')]
    if not pdfs or any(not pdfs.get(name) for name in expected):raise RuntimeError('Complete PDF retention could not be verified')
    state='Archived; automatic processing queued'
    with db() as c:c.execute('UPDATE private_commercial_messages SET state=?, detail=? WHERE message_key=?',(state,'Complete original email and attachments verified privately; automatic reading queued',key))
    with db() as c:cleanup=c.execute("SELECT value FROM private_commercial_settings WHERE name='cleanup_enabled'").fetchone()
    if sender=='thomas.bramlette@firstdigitalsc.com' and cleanup and cleanup['value']=='yes':
        # Verify durable private bytes before removing the shared forwarded copy.
        with db() as c:saved=c.execute('SELECT mime,mime_hash FROM private_commercial_messages WHERE message_key=?',(key,)).fetchone()
        if not saved or hashlib.sha256(saved['mime']).hexdigest()!=saved['mime_hash']:raise RuntimeError('Private retention verification failed')
        if graph.mailbox()!='graycliffjobs@firstdigitalsc.com':raise RuntimeError('Shared mailbox safety check failed')
        root=graph.GRAPH_ROOT+'/users/'+urllib.parse.quote(graph.mailbox(),safe='')+'/messages/'+urllib.parse.quote(message['id'],safe='')
        try:
            graph._request('POST',root+'/permanentDelete',token=token)
            with db() as c:c.execute("UPDATE private_commercial_messages SET state='Private processing; shared copy removed' WHERE message_key=?",(key,))
        except Exception:
            with db() as c:c.execute("UPDATE private_commercial_messages SET detail='Import verified; shared copy removal requires attention' WHERE message_key=?",(key,))
            kick(db)
            return {'state':'Removal needs attention','detail':'Private source verified; shared copy removal will retry.'}
    kick(db)
    return {'state':state,'detail':'Commercial document retained privately.'}

def process_private_message(db,key):
    with db() as c:row=c.execute('SELECT mime,mime_hash,state FROM private_commercial_messages WHERE message_key=?',(key,)).fetchone()
    if not row or hashlib.sha256(row['mime']).hexdigest()!=row['mime_hash']:raise ValueError('Private original integrity check failed')
    message=BytesParser(policy=policy.default).parsebytes(row['mime']);count=0;verified=True
    for part in message.walk():
        filename=str(part.get_filename() or '')
        if not filename.lower().endswith('.pdf'):continue
        count+=1;data=part.get_payload(decode=True)
        if not data:raise ValueError('Retained PDF attachment is empty')
        digest=hashlib.sha256(data).hexdigest()
        with db() as c:prior=c.execute('SELECT kind FROM private_commercial_documents WHERE digest=?',(digest,)).fetchone()
        if prior and prior['kind']!='review':continue
        try:
            reader=PdfReader(io.BytesIO(data))
            if len(reader.pages)>100:raise ValueError('PDF page limit exceeded')
            text='\n'.join(p.extract_text() or '' for p in reader.pages)
        except Exception:text=''
        source={'privateMessageKey':key}
        if text.strip() and save_text(db,text,filename,digest,source):continue
        try:
            from commercial_scan import read_pdf
            doc,pages=read_pdf(data)
            doc.update(filename=filename,digest=digest,source=source)
            doc['source']['verification']='Automatic local OCR; recognized quantities, extended amounts, billing codes and printed total reconcile'
            with db() as c:c.execute('INSERT INTO private_commercial_documents VALUES (?,?,?,?) ON CONFLICT(digest) DO UPDATE SET kind=excluded.kind,snapshot=excluded.snapshot WHERE private_commercial_documents.kind=\'review\'',(digest,doc['kind'],json.dumps(doc),now()))
        except Exception:
            verified=False
            doc={'kind':'review','classificationPending':True,'filename':filename,'digest':digest,'source':source,'reason':'Automatic reading is retrying privately; original email and attachment are safely archived','text':text}
            with db() as c:c.execute('INSERT INTO private_commercial_documents VALUES (?,?,?,?) ON CONFLICT(digest) DO UPDATE SET snapshot=excluded.snapshot WHERE private_commercial_documents.kind=\'review\'',(digest,'review',json.dumps(doc),now()))
    state='Processed privately' if count and verified else 'Private reading retry'
    if 'shared copy removed' in row['state']:state+='; shared copy removed'
    with db() as c:c.execute('UPDATE private_commercial_messages SET state=?,detail=? WHERE message_key=?',(state,'Complete original preserved privately; '+('all PDFs reconciled' if count and verified else 'automatic reading requires another private attempt'),key))
    return bool(count and verified)

def process_pending(db):
    initialize(db)
    with db() as c:keys=[r['message_key'] for r in c.execute("SELECT message_key FROM private_commercial_jobs WHERE state IN ('Queued','Retry') AND attempts<3 AND last_attempt<? ORDER BY last_attempt",(time.time()-300,))]
    for key in keys:
        with db() as c:c.execute("UPDATE private_commercial_jobs SET state='Reading',attempts=attempts+1,last_attempt=? WHERE message_key=?",(time.time(),key))
        try:ok=process_private_message(db,key)
        except Exception:ok=False
        with db() as c:
            c.execute('UPDATE private_commercial_jobs SET state=? WHERE message_key=?',('Complete' if ok else 'Retry',key))
            job=c.execute('SELECT attempts FROM private_commercial_jobs WHERE message_key=?',(key,)).fetchone()
            if not ok and job['attempts']>=3:
                c.execute("UPDATE private_commercial_messages SET detail='Original safely archived privately; automatic reading could not reconcile after three attempts' WHERE message_key=?",(key,))

def kick(db):
    if not PROCESSING.acquire(blocking=False):return
    def worker():
        try:
            # Recover a job interrupted by a service restart without replaying
            # any shared-mailbox mutation.
            initialize(db)
            with db() as c:c.execute("UPDATE private_commercial_jobs SET state='Retry' WHERE state='Reading' AND last_attempt<?",(time.time()-900,))
            process_pending(db)
        finally:PROCESSING.release()
    threading.Thread(target=worker,name='private-commercial-ocr',daemon=True).start()
def report(db):
    initialize(db)
    with db() as c:
        docs=[json.loads(r['snapshot']) for r in c.execute('SELECT snapshot FROM private_commercial_documents')]
        archive=[dict(r) for r in c.execute('SELECT message_key,state,detail,created_at FROM private_commercial_messages ORDER BY created_at DESC')]
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
    unique=[]
    for key,versions in grouped.items():
        if len({v['amountCents'] for v in versions})==1:
            unique.append(sorted(versions,key=lambda v:(v.get('sourceType')=='recap',len(v['lines'])),reverse=True)[0])
    review=[{'filename':d['filename'],'reason':d['reason']} for d in docs if d['kind']=='review' and (d.get('classificationPending') or any(code in d.get('text','') for code in known) or any(norm(line.split()[0]) in REV for line in d.get('text','').splitlines() if line.split()))]
    review += [{'filename':v[0]['filename'],'reason':'Conflicting invoice versions for '+str(key)} for key,v in grouped.items() if len({d['amountCents'] for d in v})>1]
    review += [{'filename':str(key),'reason':'Conflicting remittance amounts; payment excluded'} for key in conflicts]
    return {'archive':archive,'invoices':unique,'receipts':receipts,'review':review, 'unallocatedReceipts':unallocated,'currency':'USD','revenueCents':sum(d['amountCents'] for d in unique),'receiptsCents':sum(d['amountCents'] for d in receipts),'subCostEstimateCents':sum(l['subCostEstimateCents'] or 0 for d in unique for l in d['lines']),'unpricedCostLines':sum(l['subCostEstimateCents'] is None for d in unique for l in d['lines']),'note':'Kinetic only. Customer invoice amounts and cash receipts are separate. Subcontractor costs are rate-card estimates, not paid costs. Revenue excludes conflicting invoice versions. Remittance allocation uses project codes established by invoices whose work codes match the Kinetic rate card; location is never a classifier.'}

def source(db,key):
    if not re.fullmatch(r'[a-f0-9]{64}',str(key)):raise ValueError('Invalid private source reference')
    initialize(db)
    with db() as c:row=c.execute('SELECT mime,mime_hash FROM private_commercial_messages WHERE message_key=?',(key,)).fetchone()
    if not row or hashlib.sha256(row['mime']).hexdigest()!=row['mime_hash']:raise ValueError('Private source could not be verified')
    return {'contentBase64':base64.b64encode(row['mime']).decode(),'filename':'remittance-'+key[:12]+'.eml'}
