"""Extract nonfinancial NTP facts; retain commercial source files for office use."""
from __future__ import annotations
import io,json,re,zipfile,hashlib,subprocess,sys,tempfile,os
from pathlib import PurePath
from email import policy
from email.parser import BytesParser
import graph_import as graph

FACTS_COLUMN='Portal Field Instructions'
FILES_COLUMN='Portal Field Files'
REVIEW_COLUMN='Portal Field Document Review'
POLICY_VERSION='field-v4'
FINANCIAL=re.compile(r'\b(?:PO\s*Amount|P\.?O\.?\s*(?:Amount|Value|Total)|Purchase\s*Order\s*Amount|Unit\s*(?:Rate|Price)|(?:Total|Extended)\s*(?:Price|Cost|Amount)|Sub\s*Payout|Emp\s*Payout|Invoice\s*Total)\b|[$£€]\s*\d',re.I)
NTP=re.compile(r'\b(?:NTP|Notice\s+to\s+Proceed)\b',re.I)
FACT_KEYS=('prism','workOrder','purchaseOrder','constructionType','coordinator','workOrderDate','estimatedCompletion','vendor','issuedBy','jobName','address','cityStateZip','siteContact','sitePhone','comments','permitNotice')
LABELS={'prism':r'PRISM\s+ID','workOrder':r'Work\s+Order\s*#','purchaseOrder':r'PO\s*#','_price':r'PO\s*Amount','constructionType':r'Const\s*Type','coordinator':r'Const\s*Coordinator','workOrderDate':r'Work\s*Order\s*Date','estimatedCompletion':r'Est\.?\s*Completion','vendor':r'Vendor\s*Name','issuedBy':r'Issued\s*By','comments':r'Comments'}
LABEL_RE=re.compile(r'(?P<label>'+'|'.join('(?P<'+k+'>'+v+')' for k,v in LABELS.items())+r')\s*:',re.I)

def pdf_text(raw):
    """Untrusted PDFs run in a disposable, memory-limited process, never the web worker."""
    if len(raw)>8*1024*1024:return ''
    program='''
import io,sys
try:
 import resource
 resource.setrlimit(resource.RLIMIT_AS,(128*1024*1024,128*1024*1024))
 resource.setrlimit(resource.RLIMIT_CPU,(8,8))
 from pypdf import PdfReader
 reader=PdfReader(io.BytesIO(sys.stdin.buffer.read(8*1024*1024+1)))
 if len(reader.pages)>12:sys.exit(0)
 parts=[];size=0
 for page in reader.pages:
  value=page.extract_text(extraction_mode='layout') or '';size+=len(value)
  if size>200000:sys.exit(0)
  parts.append(value)
 sys.stdout.write('\\n'.join(parts))
except (Exception,MemoryError):sys.exit(0)
'''
    try:
        result=subprocess.run([sys.executable,'-c',program],input=raw,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=12)
        return result.stdout.decode('utf-8',errors='replace') if result.returncode==0 and len(result.stdout)<800000 else ''
    except (OSError,subprocess.TimeoutExpired):return ''

def safe_text(value):
    lines=[]
    for line in str(value or '').splitlines():
        if FINANCIAL.search(line):continue
        line=re.sub(r'\s+',' ',line).strip()
        if line:lines.append(line)
    return '\n'.join(lines)[:5000]

def ntp_facts(text, expected_work_order=''):
    if not (re.search(r'Work\s+Order\s*#?\s*:',text,re.I) and re.search(r'PRISM\s+ID\s*:',text,re.I)):return {}
    matches=list(LABEL_RE.finditer(text));out={}
    for i,m in enumerate(matches):
        key=next((k for k in LABELS if m.group(k) is not None),None)
        if key=='_price':continue
        # Stop at the next field label or first line break. Never join the amount row into another field.
        value=text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)]
        value=value.splitlines()[0] if value.splitlines() else ''
        out[key]=safe_text(value)[:500]
    if expected_work_order and out.get('workOrder') and out['workOrder']!=str(expected_work_order):return {}
    lines=text.splitlines()
    for i,line in enumerate(lines):
        if 'Job Address' in line and 'Site Contact' in line and 'Site Phone' in line:
            positions=[line.lower().find('job'),line.find('Job Address'),line.find('Site Contact'),line.find('Site Phone')]
            if positions[0]<positions[1]<positions[2]<positions[3]:
                body=[l for l in lines[i+1:i+8] if l.strip() and not re.search(r'Please do not|Comments\s*:',l,re.I)]
                if body:
                    first=body[0];parts=[first[positions[n]:positions[n+1] if n<3 else None].strip() for n in range(4)]
                    for key,value in zip(('jobName','address','siteContact','sitePhone'),parts):
                        if value:out[key]=safe_text(value)[:250]
                    # PDF layout text can shift a phone left of its column header.
                    phone=re.search(r'(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?:\s*(?:ext\.?|x)\s*\d+)?',first[positions[2]:],re.I)
                    if phone:
                        start=positions[2]+phone.start();out['sitePhone']=phone.group().strip()
                        out['siteContact']=safe_text(first[positions[2]:start])[:250]
                    else:out.pop('sitePhone',None)
                    for l in body[1:]:
                        city=l[positions[1]:positions[2]].strip()
                        if re.search(r'\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b',city,re.I):out['cityStateZip']=safe_text(city);break
            break
    notice=re.search(r'Please\s+do\s+not\s+start\s+work[^\n]+',text,re.I)
    if notice:out['permitNotice']=safe_text(notice.group())[:500]
    for key in ('prism','workOrder','purchaseOrder'):
        if key in out and not re.fullmatch(r'\d{4,18}',out[key]):out.pop(key)
    return {k:v for k,v in out.items() if k in FACT_KEYS and v and not FINANCIAL.search(v)}

def normalize_id(value):
    return str(int(value)) if isinstance(value,(int,float)) else str(value or '')

def documents(raw, name, depth=0):
    if depth>2 or len(raw)>30*1024*1024:return []
    if name.lower().endswith('.pdf'):
        text=pdf_text(raw)
        return [(name,text,raw)] if text else []
    if name.lower().endswith('.zip'):
        result=[]
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if len(z.infolist())>100 or sum(i.file_size for i in z.infolist())>50*1024*1024:return []
                for item in z.infolist():
                    if not item.is_dir() and item.filename.lower().endswith(('.pdf','.zip')):
                        result+=documents(z.read(item),item.filename,depth+1)
        except (zipfile.BadZipFile,RuntimeError):pass
        return result
    return []

def email_text(raw):
    m=BytesParser(policy=policy.default).parsebytes(raw);parts=[str(m.get('Subject',''))]
    for part in m.walk():
        if part.get_content_type() in ('text/plain','text/html') and part.get_content_disposition()!='attachment':
            try:parts.append(graph._plain_text(part.get_content()))
            except (LookupError,UnicodeError,AttributeError):pass
    return '\n'.join(parts)

def scan_text(raw,pdf=False):
    # All rendering is bounded and outside the web worker. Share the existing
    # OCR lock with billing so neither task can start a second OCR worker.
    from pathlib import Path
    import fcntl
    with open(Path(tempfile.gettempdir())/'graycliff-commercial-ocr.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='field-review-') as folder:
            root=Path(folder);source=root/('source.pdf' if pdf else 'source.image');source.write_bytes(raw)
            program='''
import sys,json,resource
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS,(220*1024*1024,220*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU,(20,20))
root=Path(sys.argv[1]);pdf=sys.argv[2]=='pdf';items=[]
if pdf:
 import pypdfium2 as p
 doc=p.PdfDocument(root/'source.pdf')
 if len(doc)>12:raise ValueError('Too many pages')
 pages=range(len(doc))
else:pages=range(1)
for i in pages:
 if pdf:
  page=doc[i];w,h=page.get_size();bitmap=page.render(scale=min(2,1800/max(w,h)));im=bitmap.to_pil()
 else:
  from PIL import Image
  im=Image.open(root/'source.image');im.thumbnail((1800,1800))
 path=root/('page-'+str(i)+'.png');im.convert('RGB').save(path);w,h=im.size;im.close()
 if pdf:bitmap.close();page.close()
 items.append(dict(page=i+1,path=str(path),lowPath=str(path),width=w,height=h,lowWidth=w,lowHeight=h))
(root/'manifest.json').write_text(json.dumps(items))
'''
            subprocess.run([sys.executable,'-c',program,str(root),'pdf' if pdf else 'image'],capture_output=True,timeout=30,check=True)
            result=subprocess.run(['node','--max-old-space-size=128',str(Path(__file__).with_name('commercial_ocr.cjs')),str(root/'manifest.json')],capture_output=True,text=True,timeout=120,check=True)
            return '\n'.join(p['text'] for p in json.loads(result.stdout))

def image_text(raw):return scan_text(raw)

def document_is_private(text):
    return bool(FINANCIAL.search(text) or ntp_facts(text) or re.search(r'Notice\s+to\s+Proceed',text,re.I))

def spreadsheet_text(raw):
    # Read shared strings AND inline strings; never execute workbook macros.
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members=[i for i in z.infolist() if i.filename.startswith('xl/') and i.filename.endswith('.xml')]
        if sum(i.file_size for i in members)>20*1024*1024:raise ValueError('Workbook too large to review')
        return '\n'.join(' '.join(ET.fromstring(z.read(i)).itertext()) for i in members)

def classify(name, raw, ntp_source=False):
    ext=PurePath(name).suffix.lower()
    if NTP.search(name) or re.search(r'(?:rate.?card|billing|invoice|remittance|purchase.?order)',name,re.I):return 'office','NTP or commercial source document'
    if re.search(r'(?:graycliff-logo|logo-tag|facebook|linkedin|twitter)',name,re.I):return 'office','Email signature graphic'
    if ext in ('.zip','.msg','.eml'):
        if ext=='.eml':
            text=email_text(raw)
            if not NTP.search(text) and not FINANCIAL.search(text):return 'preview','Nonfinancial email preview; original NTP attachments stay with office'
        return 'office','Original email or package retained by office'
    if ext=='.pdf':
        text=pdf_text(raw)
        if len(text.strip())<30:
            try:
                text=scan_text(raw,pdf=True)
            except Exception:return 'review','PDF could not be checked for NTP or pricing'
        if document_is_private(text):return 'office','NTP or commercial source document'
        return 'field','Technical PDF checked for NTP and pricing'
    if ext in ('.jpg','.jpeg','.png','.webp','.gif','.heic'):
        if not ntp_source:return 'field','Field photo'
        try:text=image_text(raw)
        except Exception:return 'review','Image could not be checked for NTP or pricing'
        return ('office','NTP or pricing image') if document_is_private(text) else ('field','Nonfinancial image')
    if ext in ('.xlsx','.xlsm','.xls'):
        try:text=spreadsheet_text(raw)
        except Exception:return 'review','Spreadsheet could not be checked for NTP or pricing'
        return ('office','NTP or pricing workbook') if document_is_private(text) else ('field','Nonfinancial workbook')
    if ext in ('.sor',):return 'field','Field test file'
    if ext in ('.txt','.csv'):
        text=raw.decode('utf-8',errors='replace')
        if not document_is_private(text):return 'field','Nonfinancial field text'
    return 'review','Office review required before field release'

def ensure_columns(store,sid):
    sheet=store.get_sheet(sid);known={c['title'] for c in sheet['columns']};missing=[{'title':n,'type':'TEXT_NUMBER','index':len(known)} for i,n in enumerate((n for n in (FACTS_COLUMN,FILES_COLUMN,REVIEW_COLUMN) if n not in known))]
    if missing:store.add_columns(sid,missing)
    return store.get_sheet(sid,force=True)

def sync_row(store,sid,rid,force=False):
    from pathlib import Path
    import fcntl
    with open(Path(tempfile.gettempdir())/'graycliff-field-review.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _sync_row(store,sid,rid,force)

def _sync_row(store,sid,rid,force=False):
    sheet=ensure_columns(store,sid);cols={int(c['id']):c['title'] for c in sheet['columns']};row=next(r for r in sheet['rows'] if int(r['id'])==int(rid));values={cols.get(int(c['columnId'])):c.get('value','') for c in row['cells']}
    try:old=json.loads(values.get(FILES_COLUMN) or '{}')
    except (ValueError,TypeError):old={}
    try:facts=json.loads(values.get(FACTS_COLUMN) or '{}')
    except (ValueError,TypeError):facts={}
    if force or old.get('_version')!=POLICY_VERSION:old={}
    files=sorted(store.list_row_attachments(sid,rid),key=lambda a:(a.get('createdAt',''),str(a['id'])));payloads={};is_ntp=bool(values.get('Portal NTP Due Date') or values.get('Portal Work Order') or facts)
    # NTP source is established before assigning any imported pictures to the field.
    for a in files:
        if a['name'].lower().endswith('.eml') and str(a['id']) not in old:
            raw,_,_=store.download_attachment(sid,a['id']);payloads[str(a['id'])]=raw
            is_ntp=is_ntp or bool(NTP.search(email_text(raw)))
    policy_map={};extracted=[]
    for a in files:
        aid=str(a['id'])
        if aid in old:
            policy_map[aid]=old[aid];continue
        ext=PurePath(a['name']).suffix.lower()
        raw=payloads.get(aid)
        if raw is None and (is_ntp or ext not in ('.jpg','.jpeg','.png','.webp','.gif','.heic','.sor')):
            raw,_,_=store.download_attachment(sid,a['id'])
        prepared=False
        if ext=='.pdf' and not re.search(r'\bNTP\b|rate.?card|billing|invoice|remittance|purchase.?order',a['name'],re.I):
            try:
                from field_pdf import prepare
                clean,redactions=prepare(raw or b'')
                mode,reason=('office','Pricing retained in office original; redacted field copy created') if redactions else ('field','Technical PDF checked for NTP and pricing')
                if redactions:extracted.append((a['name'],clean))
                prepared=True
            except Exception:
                mode,reason='review','PDF needs office review before field release'
                prepared=True
        if not prepared:mode,reason=classify(a['name'],raw or b'',is_ntp)
        policy_map[aid]={'mode':mode,'reason':reason}
        if ext=='.eml':
            instructions=safe_text(email_text(raw or b''))
            if instructions:
                prior=facts.get('comments','')
                facts['comments']=(prior+'\n'+instructions if prior and prior not in instructions else instructions)[:2500]
        for name,text,data in documents(raw or b'',a['name']):
            found=ntp_facts(text,normalize_id(values.get('Portal Work Order')))
            if found:facts.update(found)
            if ext=='.zip' and classify(name,data,is_ntp)[0]=='field':extracted.append((name,data))
    existing_names={a['name'] for a in files}
    for name,data in extracted:
        safe='Field-'+PurePath(name).stem[:80]+'-'+hashlib.sha256(data).hexdigest()[:10]+'.pdf'
        if safe not in existing_names:
            store.attach_file_to_row(sid,rid,filename=safe,mime_type='application/pdf',data=data);existing_names.add(safe)
    if extracted:
        for a in store.list_row_attachments(sid,rid):
            if a['name'].startswith('Field-') and str(a['id']) not in policy_map:policy_map[str(a['id'])]={'mode':'field','reason':'Nonfinancial technical PDF extracted from source package'}
    facts={k:safe_text(v) for k,v in facts.items() if k in FACT_KEYS and v and not FINANCIAL.search(str(v))}
    compact_policy={aid:{'mode':entry['mode']} for aid,entry in policy_map.items()}
    compact_policy['_version']=POLICY_VERSION
    update={FACTS_COLUMN:json.dumps(facts,separators=(',',':')),FILES_COLUMN:json.dumps(compact_policy,separators=(',',':')),REVIEW_COLUMN:''}
    # Smartsheet TEXT_NUMBER cells allow 4,000 characters. No policy is truncated.
    if any(len(v)>3900 for v in update.values()):raise ValueError('Field-document metadata needs office review; no partial policy was saved.')
    store.update_row(sid,rid,update)
    return {'facts':len(facts),'files':len(policy_map),'officeOnly':sum(p['mode'] in ('office','review') for p in policy_map.values())}

def backfill(store,limit=1):
    """Migrate existing operational rows durably in small, retryable batches."""
    done=0
    for sid in (2515846899388292,8067039099834244):
        sheet=store.get_sheet(sid,force=True)
        cols={int(c['id']):c['title'] for c in sheet['columns']}
        def priority(row):
            v={cols.get(int(c['columnId'])):c.get('value','') for c in row['cells']}
            return (str(v.get('Status','')).lower() in ('billed','canceled','cancelled','closed','complete'), '7061750' not in str(v))
        for row in sorted(sheet['rows'],key=priority):
            values={cols.get(int(c['columnId'])):c.get('value','') for c in row['cells']}
            try:policy=json.loads(values.get(FILES_COLUMN) or '{}')
            except (ValueError,TypeError):policy={}
            if policy.get('_version')==POLICY_VERSION:continue
            try:sync_row(store,sid,row['id'])
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Field document migration failed for row %s',row['id'])
            done+=1
            if done>=limit:return done
    return done

def install(app,store):
    # Legacy email intake uses the same PDF reader. Keep it outside the web
    # process and under the same limits, including before field-file extraction.
    graph._pdf_text=pdf_text
    module=sys.modules.get(app.import_name)
    scheduler=getattr(module,'scheduler',None)
    if scheduler:
        from datetime import datetime,timedelta,timezone
        from memory_runtime import background_job
        scheduler.add_job(background_job(lambda:backfill(store),store),'interval',minutes=2,id='field_document_visibility_v2',replace_existing=True,max_instances=1,next_run_time=datetime.now(timezone.utc)+timedelta(seconds=20))
    import os,time,hmac
    from flask import request,jsonify
    path='/api/portal/field-review'
    @app.post(path)
    def portal_field_review():
        raw=request.get_data();ts=request.headers.get('X-FDGC-Time','');signature=request.headers.get('X-FDGC-Signature','')
        secret=os.getenv('GRAYCLIFF_BRIDGE_SECRET','') or store.token
        value='fdgc-mail-bridge-v1\n'+ts+'\nPOST\n'+path+'\n'+hashlib.sha256(raw).hexdigest()
        if not ts.isdigit() or abs(time.time()-int(ts))>300 or not hmac.compare_digest(hmac.new(secret.encode(),value.encode(),hashlib.sha256).hexdigest(),signature):return jsonify({'error':'Unauthorized'}),403
        try:
            p=request.get_json() or {};sid=int(p['sheetId']);rid=int(p['rowId'])
            if sid not in (2515846899388292,8067039099834244):raise ValueError('Only operational jobs support field-document review.')
            result=sync_row(store,sid,rid,force=bool(p.get('refresh')))
            return jsonify(result)
        except Exception:return jsonify({'error':'Field details could not be fully extracted. Source files remain with the office; review the job details before release.'}),400
