"""Import retained work-completed totals, never reprice or send historical work."""
import base64, hashlib, io, json, re, sqlite3, time, urllib.parse, zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
TABLE='financial_spreadsheet_imports'

def initialize(db):
    with db() as c:c.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        source_key TEXT PRIMARY KEY, snapshot TEXT NOT NULL, imported_at REAL NOT NULL)''')

def money(value):
    try:
        n=Decimal(str(value))
        if not n.is_finite() or n<0 or n>9999999999:raise ValueError('Invalid amount')
        return int((n*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    except (InvalidOperation,TypeError):raise ValueError('Amount is not numeric')

def extract_total(raw):
    """Known unit-code template only. Check the saved total against its saved lines.

    This reads the billed spreadsheet's amounts, including its actual total range;
    it does not apply today's rates or execute workbook code/external references.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(i.file_size for i in z.infolist())>30000000:raise ValueError('Workbook exceeds inspection limit')
        if any(n.startswith('xl/externalLinks/') for n in z.namelist()):raise ValueError('External workbook references need review')
        ss=[]
        if 'xl/sharedStrings.xml' in z.namelist():ss=[''.join(e.itertext()) for e in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        def text(c):
            if c is None:return ''
            if c.get('t')=='inlineStr':return ''.join(c.find('s:is',NS).itertext())
            v=c.find('s:v',NS);v=v.text if v is not None else ''
            return ss[int(v)] if c.get('t')=='s' and v else v
        found=[]
        for path in z.namelist():
            if not re.fullmatch(r'xl/worksheets/sheet\d+\.xml',path):continue
            cells={c.get('r'):c for c in ET.fromstring(z.read(path)).findall('.//s:c',NS)}
            codes={col:text(cells.get(col+'1')).strip() for col in 'BCDEFGHIJKLMNOPQR'}
            if sum(bool(re.fullmatch(r'[A-Z]{2}\d{2}[A-Z]?',v)) for v in codes.values())<8:continue
            totals=[int(re.search(r'\d+',ref)[0]) for ref,c in cells.items() if ref.startswith('S') and text(c).strip().lower()=='totals']
            for row in totals:
                n=row+1;ref='S'+str(n);c=cells.get(ref)
                if c is None or text(c)=='':continue
                f=c.find('s:f',NS);formula=(f.text or '').upper().replace('$','').replace(' ','') if f is not None else ''
                match=re.fullmatch(r'SUM\(([B-R])'+str(n)+r':([B-R])'+str(n)+r'\)',formula)
                if not match:raise ValueError('Unrecognized grand-total formula at '+ref)
                start,end=match.groups();lines=[];exact=Decimal(0)
                for col in map(chr,range(ord(start),ord(end)+1)):
                    v=text(cells.get(col+str(n)))
                    if v=='':raise ValueError('A priced total line is missing')
                    amount=money(v);exact+=Decimal(v)
                    qty=text(cells.get(col+str(row))) or '0'
                    try:qty=float(Decimal(qty))
                    except InvalidOperation:raise ValueError('A quantity total is missing')
                    lines.append({'code':codes[col],'quantity':qty,'amountCents':amount})
                total=money(text(c))
                if abs(total-money(exact))>1:raise ValueError('Saved total does not reconcile to its included dollar lines')
                found.append({'amountCents':total,'lines':lines,'sourceCell':path+'!'+ref,
                    'formula':formula,'fileSha256':hashlib.sha256(raw).hexdigest()})
        if len(found)!=1:raise ValueError('No unique priced work-completed total was found')
        return found[0]

def normalized(value):
    value=str(value).lower().replace('’',"'")
    for a,b in [('street','st'),('road','rd'),('drive','dr'),('highway','hwy'),('court','ct'),('lane','ln'),('boulevard','blvd'),('avenue','ave'),('mount','mt')]:
        value=re.sub(r'\b'+a+r'\b',b,value)
    return re.sub(r'[^a-z0-9]+',' ',value).strip()

def match_job(filename,jobs):
    name=re.sub(r'\.xlsx$','',filename,flags=re.I)
    name=re.sub(r'^work[ _-]*completed(?:[ _-]*spreadsheet)?[ _-]*','',name,flags=re.I)
    name=normalized(name.strip(' ()'))
    candidates=[]
    for j in jobs:
        title=normalized(j['title']);parts=[normalized(x) for x in re.split(r'[·|]',j['title'])]
        if name==title or name in parts:candidates.append(j)
    return candidates[0] if len(candidates)==1 else None

def save_candidates(db,candidates):
    initialize(db);saved=0;conflicts=0
    for key,versions in candidates.items():
        versions.sort(key=lambda x:x['sentAt'],reverse=True)
        amounts={x['amountCents'] for x in versions}
        if len(amounts)>1 and not re.search(r'\b(revised|corrected|updated|revision)\b',versions[0]['subject'],re.I):conflicts+=1;continue
        record=versions[0];record['versionsFound']=len(versions)
        with db() as c:c.execute(f'INSERT OR REPLACE INTO {TABLE} VALUES(?,?,?)',(key,json.dumps(record),time.time()))
        saved+=1
    return {'imported':saved,'conflicts':conflicts}

def scan(db,store,mailbox,progress=print):
    import graph_import as graph
    initialize(db);jobs=[]
    for sh in store.list_sheets():
        if sh['name'] not in ('Completed Small Projects Graycliff','Small Projects Graycliff - Florence','Small Projects Graycliff - Columbia'):continue
        sheet=store.get_sheet(sh['id']);cols={c['id']:c['title'] for c in sheet['columns']}
        for row in sheet.get('rows',[]):
            v={cols[c['columnId']]:c.get('value','') for c in row['cells']};title=v.get('Task Name','')
            if not title or v.get('Status')=='Canceled':continue
            if sh['name']!='Completed Small Projects Graycliff' and v.get('Status')!='Billed':continue
            sid=str(v.get('Portal Billing Source Sheet') or sh['id']);rid=str(v.get('Portal Billing Source Row') or row['id'])
            jobs.append({'title':title,'sourceKey':sid+':'+rid,'sheetId':sid,'rowId':rid})
    user=urllib.parse.quote(mailbox,safe='');root=graph.GRAPH_ROOT+'/users/'+user+'/messages'
    query=urllib.parse.urlencode({'$search':'"work completed"','$top':100,'$select':'id,subject,from,sentDateTime,receivedDateTime,hasAttachments'})
    token=graph.access_token();url=root+'?'+query;messages=[]
    while url:
        if not url.startswith(root+'?'):raise ValueError('Unexpected message pagination source')
        result=graph._request('GET',url,token=token);messages.extend(result.get('value',[]));url=result.get('@odata.nextLink','')
    candidates={};unmatched=[];errors=[];inspected=0
    for index,m in enumerate(messages):
        sender=m.get('from',{}).get('emailAddress',{}).get('address','').lower()
        if not sender.endswith('@firstdigitalsc.com') or re.search(r'\bTEST ONLY\b',m['subject'],re.I) or not m.get('hasAttachments'):continue
        mid=urllib.parse.quote(m['id'],safe='');attachroot=root+'/'+mid+'/attachments'
        attachments=graph._request('GET',attachroot+'?$top=100&$select=id,name,size,isInline',token=token).get('value',[])
        for a in attachments:
            if not re.search(r'work[ _-]*completed.*\.xlsx$',a['name'],re.I) or a.get('isInline'):continue
            job=match_job(a['name'],jobs)
            if not job:unmatched.append(a['name']);continue
            try:
                if a.get('size',0)>15000000:raise ValueError('Workbook exceeds size limit')
                aid=urllib.parse.quote(a['id'],safe='');item=graph._request('GET',attachroot+'/'+aid,token=token)
                raw=base64.b64decode(item['contentBytes']);record=extract_total(raw)
                record.update(job);record.update({'source':'Work completed spreadsheet','sourceFile':a['name'],
                    'sentAt':m.get('sentDateTime') or m['receivedDateTime'],'messageId':m['id'],'attachmentId':a['id'],'subject':m['subject']})
                candidates.setdefault(job['sourceKey'],[]).append(record);inspected+=1
            except Exception as e:errors.append({'file':a['name'],'reason':str(e)[:180]})
        if index%20==0:progress(json.dumps({'messagesChecked':index+1,'messagesFound':len(messages),'matchedJobs':len(candidates)}),flush=True)
    result=save_candidates(db,candidates);result.update({'messagesFound':len(messages),'inspectedFiles':inspected,'unmatchedFiles':sorted(set(unmatched)),'errors':errors})
    progress(json.dumps(result),flush=True);return result

if __name__=='__main__':
    import argparse,os
    from contextlib import contextmanager
    from smartsheet_api import SmartsheetClient
    p=argparse.ArgumentParser();p.add_argument('--mailbox',required=True);args=p.parse_args()
    @contextmanager
    def db():
        c=sqlite3.connect(os.getenv('DATA_PATH','/tmp/graycliff.db'),timeout=30);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    scan(db,SmartsheetClient(),args.mailbox)
