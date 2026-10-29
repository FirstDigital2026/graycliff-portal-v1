"""Explicitly reviewed billing packages. No automatic or historical sending."""
from __future__ import annotations
import base64, hashlib, hmac, html, json, os, re, time, uuid, zipfile, io
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import PurePath
from email.message import EmailMessage
from email import policy
from email.utils import formatdate
import mail_automation as mail
import billing_transport as transport

SHEETS = {2515846899388292, 8067039099834244}
PATH = '/api/portal/billing'

def billing_route(store, payload):
    """Keep the source thread, but use the private area policy for billing only."""
    sid=int(payload['sheetId'])
    if sid not in SHEETS:raise ValueError('Billing area is not supported. Nothing was sent.')
    route=mail.resolve_thread(store,mail.graph.access_token(),{'sheetId':sid,'rowId':payload['rowId']})
    config=mail.bridge(store,'POST','/fdgc/v1/billing-recipients',{'sheetId':str(sid)})
    return apply_billing_recipients(route,config,sid)

def apply_billing_recipients(route, config, sheet_id):
    if not isinstance(config,dict) or str(config.get('sheetId'))!=str(sheet_id):
        raise ValueError('Billing recipients could not be verified. Nothing was sent.')
    result=dict(route)
    for field in ('to','cc'):
        addresses=config.get(field)
        if not isinstance(addresses,list) or not 1<=len(addresses)<=20:
            raise ValueError('The billing recipient list is incomplete. Nothing was sent.')
        clean=[]
        for address in addresses:
            if not isinstance(address,str) or not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',address):
                raise ValueError('A billing email address is invalid. Nothing was sent.')
            address=address.lower()
            if address==mail.SENDER.lower():raise ValueError('Billing cannot be sent to its own mailbox.')
            if address not in clean:clean.append(address)
        result[field]=clean
    if len(result['cc'])!=2 or set(result['to'])&set(result['cc']):
        raise ValueError('Check the separate billing To and Cc lists. Nothing was sent.')
    return result

def initialize(db):
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS billing_package_outbox (
          id TEXT PRIMARY KEY,job_key TEXT NOT NULL,state TEXT NOT NULL,
          snapshot TEXT NOT NULL,created_at REAL NOT NULL,sent_at TEXT,detail TEXT)''')

def reviewed_recipients(value):
    """One billing preview only; never change market defaults or field routing."""
    if not isinstance(value,dict) or set(value)!={'to','cc'}:
        raise ValueError('Check the billing To and Cc addresses.')
    result={}
    for field in ('to','cc'):
        entries=value[field]
        if not isinstance(entries,list) or len(entries)>20:
            raise ValueError('Use up to 20 billing addresses in each recipient box.')
        clean=[]
        for address in entries:
            if not isinstance(address,str) or not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',address):
                raise ValueError('A billing email address is invalid. Nothing was sent.')
            address=address.lower()
            if address==mail.SENDER.lower():raise ValueError('Billing cannot be sent to its own mailbox.')
            if address not in clean:clean.append(address)
        result[field]=clean
    if not result['to']:raise ValueError('Add at least one To recipient for this billing email.')
    if set(result['to'])&set(result['cc']):raise ValueError('An address is in both To and Cc. Keep it in one box.')
    return result

def authenticated(raw, headers, secret):
    ts=headers.get('X-FDGC-Time','');sig=headers.get('X-FDGC-Signature','')
    if not secret or not ts.isdigit() or abs(time.time()-int(ts))>300:return False
    value='fdgc-mail-bridge-v1\n'+ts+'\nPOST\n'+PATH+'\n'+hashlib.sha256(raw).hexdigest()
    return hmac.compare_digest(hmac.new(secret.encode(),value.encode(),hashlib.sha256).hexdigest(),sig)

def fresh_job(store, payload):
    sid=int(payload['sheetId']);rid=int(payload['rowId'])
    if sid not in SHEETS:raise ValueError('Only active operational jobs can submit a new package.')
    s=store.get_sheet(sid,force=True);r=next((r for r in s['rows'] if int(r['id'])==rid),None)
    if r is None:raise ValueError('Job no longer exists.')
    cols={int(c['id']):c['title'] for c in s['columns']}
    v={cols.get(int(c['columnId'])):c.get('value','') for c in r['cells']}
    if str(v.get('Status','')).lower()!='complete':raise ValueError('Finish field work before submitting billing.')
    if v.get('Portal Billing Status')=='Submitted':raise ValueError('Billing is already recorded as submitted.')
    return v

def collect(store, payload):
    sid=int(payload['sheetId']);rid=int(payload['rowId'])
    attached={str(a['id']):a for a in store.list_row_attachments(sid,rid)}
    selected=payload.get('files',{})
    if not isinstance(selected,dict):raise ValueError('Select the billing documents.')
    selected=dict(selected)
    no_workbook=payload.get('workbookNotNeeded') is True
    if no_workbook:selected.pop('workbook',None)
    roles=['spreadsheet','map']+(['workbook'] if payload.get('workbookRequired') and not no_workbook else [])
    if selected.get('workbook') and not no_workbook and 'workbook' not in roles:roles.append('workbook')
    if not payload.get('workbookRequired') and not payload.get('workbookNotNeeded'):
        raise ValueError('Include the workbook or confirm it is not needed for this job.')
    supporting=selected.pop('supporting',[])
    if not isinstance(supporting,list) or len(supporting)>20:raise ValueError('Choose up to 20 supporting test files.')
    for i,aid in enumerate(supporting):
        role='supporting'+str(i);roles.append(role);selected[role]=str(aid)
    if len({str(selected.get(role,'')) for role in roles})!=len(roles):raise ValueError('Choose a different file for each document.')
    result=[]
    for role in roles:
        aid=str(selected.get(role,''));a=attached.get(aid)
        if not a:raise ValueError('The selected '+role+' is not attached to this job.')
        name=str(a.get('name',''));ext=PurePath(name).suffix.lower()
        if '\r' in name or '\n' in name or len(name)>200:raise ValueError('Invalid attachment name.')
        allowed={'.pdf','.sor','.csv','.txt'} if role.startswith('supporting') else ({'.pdf','.jpg','.jpeg','.png'} if role=='map' else {'.xlsx','.xlsm'})
        if ext not in allowed:
            raise ValueError('The completed-work asbuilt must be PDF, JPEG or PNG; spreadsheets and fiber workbooks must be Excel files.')
        raw,_,_=store.download_attachment(sid,int(aid))
        if len(raw)>transport.MAX_FILE:raise ValueError('Each billing file must be at most 15 MB. Nothing was sent.')
        if ext in {'.pdf','.xlsx','.xlsm'} and not raw.startswith(b'%PDF-' if ext=='.pdf' else b'PK'):
            raise ValueError('The '+role+' does not match its file type.')
        if ext in {'.jpg','.jpeg','.png'}:
            from PIL import Image
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format not in ({'PNG'} if ext=='.png' else {'JPEG'}) or image.width*image.height>40000000:raise ValueError('Invalid asbuilt photo.')
                    image.verify()
            except Exception as exc:raise ValueError('The asbuilt photo is not a readable matching JPEG or PNG.') from exc
        if role=='spreadsheet' and not any(quantity_totals(raw).values()):
            raise ValueError('The spreadsheet has no completed quantities. Fill it out before submitting billing.')
        result.append({'role':role,'id':aid,'name':name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'data':raw})
        transport.validate_size(result)
    return result

def quantity_layout(raw):
    """Read bounded quantity inputs; never trust stale cached totals or run macros."""
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist())>30000000:raise ValueError('Workbook is too large to inspect.')
            if any(n.startswith('xl/externalLinks/') or n.endswith('vbaProject.bin') for n in z.namelist()):
                raise ValueError('Use the quantities-only .xlsx template for the work completed spreadsheet.')
            sheets=[n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n)]
            if len(sheets)!=1:raise ValueError('Use the single-sheet quantities-only template.')
            root=ET.fromstring(z.read(sheets[0]));ss=[]
            if 'xl/sharedStrings.xml' in z.namelist():ss=[''.join(e.itertext()) for e in ET.fromstring(z.read('xl/sharedStrings.xml'))]
            cells={c.attrib['r']:c for c in root.findall('.//s:c',ns)}
            def text(c):
                if c is None:return ''
                v=c.find('s:v',ns)
                if c.attrib.get('t')=='inlineStr':return ''.join(c.find('s:is',ns).itertext())
                return ss[int(v.text)] if c.attrib.get('t')=='s' and v is not None else (v.text if v is not None else '')
            total_rows=[]
            for row in range(3,29):
                if text(cells.get('A'+str(row))).strip().lower()=='totals' or text(cells.get('S'+str(row))).strip().lower()=='totals':total_rows.append(row)
            if len(total_rows)!=1:raise ValueError('The spreadsheet must have one totals row directly below the completed work.')
            total_row=total_rows[0]
            for c in cells.values():
                f=c.find('s:f',ns)
                if f is not None and '*' in (f.text or ''):raise ValueError('This spreadsheet contains dollar multipliers. Use the quantities-only template; pricing stays with the office.')
                if int(re.search(r'\d+',c.attrib['r'])[0])>total_row and text(c):
                    raise ValueError('Remove the dollar row using the quantities-only template before submitting.')
            totals={}
            for col in 'BCDEFGHIJKLMNOPQR':
                code=text(cells.get(col+'1'))
                if not re.fullmatch(r'[A-Z]{2}\d{2}[A-Z]?',code):raise ValueError('The spreadsheet unit-code headers do not match the template.')
                total=Decimal(0)
                for row in range(2,total_row):
                    c=cells.get(col+str(row));value=text(c)
                    if not value:continue
                    if c.find('s:f',ns) is not None:raise ValueError('Enter numeric quantities in the input rows.')
                    try:n=Decimal(value)
                    except (TypeError,InvalidOperation):raise ValueError('Every quantity must be a number.')
                    if not n.is_finite() or not 0<=n<=10000000:raise ValueError('Quantity is outside the supported range.')
                    total+=n
                totals[code]=float(total)
            return totals,total_row
    except (zipfile.BadZipFile,ET.ParseError,KeyError):raise ValueError('The work completed spreadsheet is not a readable .xlsx file.')

def quantity_totals(raw):
    return quantity_layout(raw)[0]
def price_spreadsheet(raw, rates):
    """Add the private rate row to the outgoing copy, keeping the field copy unpriced."""
    quantities,total_row=quantity_layout(raw)
    amount_row=total_row+1
    if not isinstance(rates,dict) or set(rates)!=set(quantities):raise ValueError('The office unit-rate configuration is incomplete. Nothing was sent.')
    lines=[];total=Decimal(0)
    for code,quantity in quantities.items():
        try:rate=Decimal(str(rates[code]))
        except InvalidOperation:raise ValueError('The office rate for '+code+' is invalid.')
        if not rate.is_finite() or not 0<=rate<=1000000:raise ValueError('Invalid office unit rate.')
        amount=(Decimal(str(quantity))*rate).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
        total+=amount;lines.append({'code':code,'quantity':quantity,'rate':float(rate),'total':float(amount)})
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main';q=lambda n:'{'+ns+'}'+n
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name=next(n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n))
        sheet=ET.fromstring(z.read(name));styles=ET.fromstring(z.read('xl/styles.xml'))
        formats=styles.find(q('numFmts'))
        if formats is None:formats=ET.Element(q('numFmts'));styles.insert(0,formats)
        fmt=max([163]+[int(f.attrib['numFmtId']) for f in formats])+1
        ET.SubElement(formats,q('numFmt'),{'numFmtId':str(fmt),'formatCode':'"$"#,##0.00;[Red]("$"#,##0.00)'})
        formats.set('count',str(len(formats)))
        fills=styles.find(q('fills'));fill=len(fills);f=ET.SubElement(fills,q('fill'));pat=ET.SubElement(f,q('patternFill'),{'patternType':'solid'});ET.SubElement(pat,q('fgColor'),{'rgb':'FFFFC000'});ET.SubElement(pat,q('bgColor'),{'indexed':'64'});fills.set('count',str(len(fills)))
        xfs=styles.find(q('cellXfs'));style=len(xfs);money=ET.SubElement(xfs,q('xf'),{'numFmtId':str(fmt),'fontId':'0','fillId':str(fill),'borderId':'0','xfId':'0','applyNumberFormat':'1','applyFill':'1','applyAlignment':'1'});ET.SubElement(money,q('alignment'),{'horizontal':'center','vertical':'center'});xfs.set('count',str(len(xfs)))
        data=sheet.find(q('sheetData'));rows={int(r.attrib['r']):r for r in data.findall(q('row'))}
        quantity_styles={c.attrib['r']:c.attrib.get('s') for c in rows.get(total_row,[]) if c.tag==q('c')}
        # Retain the supplied template's currency-row style, including its borders.
        if quantity_styles.get('B'+str(total_row)):
            source=xfs[int(quantity_styles['B'+str(total_row)])]
            for attr in ('fontId','borderId','xfId'):money.set(attr,source.get(attr,'0'))
        for n in (total_row,amount_row):
            if n not in rows:rows[n]=ET.Element(q('row'),{'r':str(n)});data.append(rows[n])
        for n in (total_row,amount_row):
            for cell in list(rows[n]):
                if re.fullmatch(r'[B-S]'+str(n),cell.attrib.get('r','')):rows[n].remove(cell)
        def cell(ref,formula,value,style_id=None):
            attrs={'r':ref}
            if style_id is not None:attrs['s']=str(style_id)
            c=ET.SubElement(rows[int(re.search(r'\d+',ref)[0])],q('c'),attrs)
            ET.SubElement(c,q('f')).text=formula;ET.SubElement(c,q('v')).text=str(value)
        for col,line in zip('BCDEFGHIJKLMNOPQR',lines):
            ref=col+str(total_row)
            cell(ref,'SUM('+col+'2:'+col+str(total_row-1)+')',line['quantity'],quantity_styles.get(ref))
            cell(col+str(amount_row),'ROUND('+ref+'*'+format(Decimal(str(rates[line['code']])),'f')+',2)',line['total'],style)
        label_ref='S'+str(total_row)
        label=ET.SubElement(rows[total_row],q('c'),{'r':label_ref,'t':'inlineStr',**({'s':quantity_styles[label_ref]} if quantity_styles.get(label_ref) else {})});is_=ET.SubElement(label,q('is'));ET.SubElement(is_,q('t')).text='Totals'
        cell('S'+str(amount_row),'SUM(B'+str(amount_row)+':R'+str(amount_row)+')',str(total),style)
        # Retain all other workbook parts and macro-free source bytes verbatim.
        dim=sheet.find(q('dimension'))
        if dim is not None:dim.set('ref','A1:S'+str(amount_row))
        for row in sorted(list(data),key=lambda e:int(e.attrib['r'])):data.remove(row);data.append(row)
        book=ET.fromstring(z.read('xl/workbook.xml'));calc=book.find(q('calcPr'))
        if calc is None:calc=ET.SubElement(book,q('calcPr'))
        calc.set('calcMode','auto');calc.set('fullCalcOnLoad','1')
        changes={name:ET.tostring(sheet,encoding='utf-8',xml_declaration=True),'xl/styles.xml':ET.tostring(styles,encoding='utf-8',xml_declaration=True),'xl/workbook.xml':ET.tostring(book,encoding='utf-8',xml_declaration=True)}
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as dest:
            for item in z.infolist():dest.writestr(item,changes.get(item.filename,z.read(item.filename)))
    return out.getvalue(),{'amount':float(total),'lines':lines,'basis':'Priced spreadsheet sent to Graycliff; all unit codes included'}

def package_files(files,rates):
    priced=[];pricing=None
    for original in files:
        f=dict(original)
        if f['role']=='spreadsheet':
            f['data'],pricing=price_spreadsheet(f['data'],rates)
            f['name']=PurePath(f['name']).stem+'.xlsx'
            f['size']=len(f['data']);f['sha256']=hashlib.sha256(f['data']).hexdigest()
        priced.append(f)
    return priced,pricing

def delivery_files(files, title):
    """Use a readable outgoing asbuilt name while retaining source bytes."""
    title=re.sub(r'[\\/:*?"<>|\x00-\x1f]','-',str(title)).strip(' .')[:120] or 'Job'
    result=[]
    for original in files:
        f=dict(original)
        if f['role']=='map':f['name']='Splicing Asbuilt ('+title+')'+PurePath(f['name']).suffix.lower()
        result.append(f)
    return result


def draft(v, route, package_id):
    title=str(v.get('Task Name',''));city=str(v.get('City',''))
    opening='Please see the attached completed billing package for '+title+'.'
    closing='We appreciate your business and look forward to our continued partnership.'
    text=(opening+'\n\n'+closing+'\n\nThank you,\nFirst Digital\ngraycliffjobs@firstdigitalsc.com\n\n'
          'This is an automated message from our field processing app.')
    body=('<html><body style="font:15px Arial;color:#153349">'
          '<p>'+html.escape(opening)+'</p><p>'+html.escape(closing)+'</p><p>Thank you,</p>'
          '<hr><img src="'+mail.LOGO+'" width="160" alt="First Digital">'
          '<p><strong>First Digital</strong><br>graycliffjobs@firstdigitalsc.com</p>'
          '<p style="font-size:11px;color:#63798a">This is an automated message from our field processing app.</p></body></html>')
    subject=route['subject']
    if not subject.lower().startswith('re:'):subject='Re: '+subject
    return {'to':route['to'],'cc':route['cc'],'subject':subject,'parent':route['messageId'],
            'references':list(dict.fromkeys(route.get('references',[])+[route['messageId']]))[-30:],
            'text':text,'html':body,'eventId':'billing-'+package_id,'title':title}


def mime_message(d, files):
    m=EmailMessage(policy=policy.SMTP);m['From']='First Digital <'+mail.SENDER+'>';m['To']=', '.join(d['to'])
    if d['cc']:m['Cc']=', '.join(d['cc'])
    m['Subject']=d['subject'];m['Date']=formatdate();m['Message-ID']='<fdgc-'+d['eventId']+'@firstdigitalsc.com>'
    if d.get('parent'):m['In-Reply-To']=d['parent']
    if d.get('references'):m['References']=' '.join(d['references'])
    m['Auto-Submitted']='auto-generated';m['X-Auto-Response-Suppress']='All'
    m.set_content(d['text']);m.add_alternative(d['html'],subtype='html')
    for f in files:
        main,sub=transport.content_type(f['name']).split('/',1)
        mime={'maintype':main,'subtype':sub}
        m.add_attachment(f['data'],filename=f['name'],**mime)
    raw=m.as_bytes()
    if len(base64.b64encode(raw))>3900000:
        raise ValueError('This package exceeds the direct-email size limit. Ask the office to send it manually and record the submission. Nothing was sent.')
    return raw

def receipt(row):
    return {'id':row['id'],'state':row['state'],'sentAt':row['sent_at'],'message':row['detail'] or ''}

def handle(store, db, payload):
    if payload.get('testTo'):raise ValueError('Test sending has been removed. Reload the billing screen before continuing.')
    initialize(db);op=payload.get('op')
    if op=='kinetic_financials':
        from kinetic_finance import report
        from financial_workflow import processing,collections
        result=report(db)
        return {**result,'processing':processing(db),'collections':collections(result,'Kinetic')}
    if op=='spectrum_financials':
        from spectrum_tracking import report
        return report(db)
    if op=='commercial_source':
        from kinetic_finance import source
        return source(db,payload.get('key',''))
    if op=='financials':
        from financial_report import report
        from billing_recaps import report as recap_report
        from spectrum_tracking import report as spectrum_report
        from financial_workflow import processing,collections
        recaps=recap_report(db);spectrum=spectrum_report(db)
        return {**report(db),'recaps':recaps,'spectrumRemittances':spectrum,'processing':processing(db),'collections':collections(spectrum,'Spectrum',recaps)}
    key=str(payload['sheetId'])+':'+str(payload['rowId'])
    if op=='review_pricing':raise ValueError('Billing review shows unpriced files. Prices are added only when sending.')
    with db() as c:
        accepted=c.execute("SELECT * FROM billing_package_outbox WHERE job_key=? AND state IN ('ACCEPTED','UNCERTAIN','DISPATCHING') ORDER BY created_at DESC LIMIT 1",(key,)).fetchone()
    if accepted and op in ('quantities','pricing'):
        if accepted['state']!='ACCEPTED':raise ValueError('Pricing can be calculated after Microsoft accepts the billing email.')
        saved=json.loads(accepted['snapshot'])
        return {'quantities':saved.get('quantities',{}),'pricing':saved.get('pricing',{})}
    if accepted:
        if accepted['state']=='ACCEPTED':return receipt(accepted)
        raise ValueError('A previous send has an uncertain outcome. The office must check Sent Items. It will not be sent again automatically.')
    if op=='prepare':
        v=fresh_job(store,payload);originals=collect(store,payload)
        files=delivery_files(originals,v.get('Task Name',''))
        route=billing_route(store,payload)
        pid=str(uuid.uuid4());d=draft(v,route,pid)
        transport.validate_size(files)
        if sum(f['size'] for f in files)<=transport.DIRECT_BUDGET:mime_message(d,files)
        snap={'input':payload,'draft':d,'files':[{k:v for k,v in f.items() if k!='data'} for f in originals],'deliveryFiles':[{k:v for k,v in f.items() if k!='data'} for f in files],
              'quantities':quantity_totals(next(f['data'] for f in originals if f['role']=='spreadsheet')),'snapshotVersion':5,'sourceRoute':route}
        with db() as c:c.execute('INSERT INTO billing_package_outbox VALUES(?,?,?,?,?,?,?)',(pid,key,'PREPARED',json.dumps(snap),time.time(),None,'Prepared only; no email sent.'))
        return {'id':pid,'state':'PREPARED','pricingOnSend':True,'to':d['to'],'cc':d['cc'],'subject':d['subject'],'text':d['text'],'files':snap['deliveryFiles']}
    if op not in ('send','recipients'):raise ValueError('Unknown billing action.')
    with db() as c:r=c.execute('SELECT * FROM billing_package_outbox WHERE id=? AND job_key=?',(str(payload.get('id','')),key)).fetchone()
    if not r or r['state']!='PREPARED':raise ValueError('Prepare and review this package before sending.')
    if time.time()-r['created_at']>1800:raise ValueError('The review has expired. Prepare the package again.')
    snap=json.loads(r['snapshot'])
    if snap.get('snapshotVersion')!=5:raise ValueError('Review this package again to confirm the updated billing recipients.')
    if op=='recipients':
        recipients=reviewed_recipients(payload.get('recipients'))
        fresh_job(store,snap['input'])
        # Keep the verified source route intact for the final send-time check.
        pid=str(uuid.uuid4())
        snap['draft']={**snap['draft'],**recipients,'eventId':'billing-'+pid}
        snap['reviewedRecipients']={**recipients,'actor':str(payload.get('actor','')),'at':mail.now()}
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute("SELECT id FROM billing_package_outbox WHERE job_key=? AND state IN ('DISPATCHING','UNCERTAIN','ACCEPTED')",(key,)).fetchone()
            if prior:raise ValueError('This billing package is already being processed. Refresh before editing recipients.')
            changed=c.execute("UPDATE billing_package_outbox SET state='REPLACED',detail='Recipient list edited in a newer review.' WHERE id=? AND state='PREPARED' AND snapshot=?",(r['id'],r['snapshot'])).rowcount
            if changed!=1:raise ValueError('The review changed in another tab. Reload before editing recipients.')
            c.execute('INSERT INTO billing_package_outbox VALUES(?,?,?,?,?,?,?)',(pid,key,'PREPARED',json.dumps(snap),r['created_at'],None,'Billing recipients reviewed; no email sent.'))
        return {'id':pid,'state':'PREPARED',**recipients,'message':'Recipients saved for this billing email. Nothing sent.'}
    fresh_job(store,snap['input']);originals=collect(store,snap['input'])
    if [{k:v for k,v in f.items() if k!='data'} for f in originals]!=snap['files']:raise ValueError('An attachment changed. Prepare and review the updated package.')
    files,pricing=package_files(delivery_files(originals,snap['draft']['title']),snap['input'].get('rates'))
    snap['pricing']=pricing;snap['sentFiles']=[{k:v for k,v in f.items() if k!='data'} for f in files]
    # Resolve once more: never silently send to a changed email chain.
    route=billing_route(store,payload)
    expected=snap.get('sourceRoute',snap['draft'])
    for k in ['to','cc']:
        if route[k]!=expected[k]:raise ValueError('Billing recipients changed. Prepare and review the package again.')
    if route['messageId']!=snap['draft']['parent']:raise ValueError('The source email changed. Prepare the package again.')
    if snap.get('reviewedRecipients'):
        reviewed=reviewed_recipients({k:snap['reviewedRecipients'][k] for k in ('to','cc')})
        if any(reviewed[k]!=snap['draft'][k] for k in ('to','cc')):
            raise ValueError('Review the updated billing recipients before sending.')
    transport.validate_size(files)
    large=sum(f['size'] for f in files)>transport.DIRECT_BUDGET
    raw=mime_message(snap['draft'],[] if large else files)
    # One send attempt per job at a time, across all prepared tabs and workers.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        prior=c.execute("SELECT id FROM billing_package_outbox WHERE job_key=? AND state IN ('DISPATCHING','UNCERTAIN','ACCEPTED')",(key,)).fetchone()
        if prior:raise ValueError('Another send is already being processed. Refresh before continuing.')
        n=c.execute("UPDATE billing_package_outbox SET state='DISPATCHING',snapshot=? WHERE id=? AND state='PREPARED'",(json.dumps(snap),r['id'])).rowcount
        if n!=1:raise ValueError('This package is already being processed.')
    try:
        if large:
            def remember(draft_id):
                snap['graphDraftId']=draft_id
                with db() as c:c.execute("UPDATE billing_package_outbox SET snapshot=? WHERE id=?",(json.dumps(snap),r['id']))
            transport.send_large(mail.graph,mail.SENDER,raw,files,remember)
        else:
            mail.graph._request('POST',mail.graph.GRAPH_ROOT+'/users/'+mail.urllib.parse.quote(mail.SENDER,safe='')+'/sendMail',
                token=mail.graph.access_token(),body=base64.b64encode(raw),headers={'Content-Type':'text/plain'})
    except transport.BeforeSendFailure:
        with db() as c:c.execute("UPDATE billing_package_outbox SET state='PREPARED',detail='Attachment preparation failed before sending; no billing email sent.' WHERE id=?",(r['id'],))
        raise
    except Exception:
        with db() as c:c.execute("UPDATE billing_package_outbox SET state='UNCERTAIN',detail='Check Sent Items before any manual action; automatic resend is blocked.' WHERE id=?",(r['id'],))
        raise ValueError('Microsoft did not confirm the result. Check Sent Items; automatic resend is blocked.')
    at=mail.now()
    with db() as c:c.execute("UPDATE billing_package_outbox SET state='ACCEPTED',sent_at=?,detail='Accepted by Microsoft. Delivery is not confirmed.' WHERE id=?",(at,r['id']))
    return {'id':r['id'],'state':'ACCEPTED','sentAt':at,'message':'Accepted by Microsoft. Delivery is not confirmed.'}

def install(app, store, db):
    from intake_review import install as install_intake_review
    install_intake_review(app, store, db)
    from flask import request, jsonify
    @app.post(PATH)
    def portal_billing_package():
        raw=request.get_data()
        secret=os.getenv('GRAYCLIFF_BRIDGE_SECRET','') or store.token
        if not authenticated(raw,request.headers,secret):return jsonify({'error':'Unauthorized'}),403
        try:return jsonify(handle(store,db,request.get_json() or {}))
        except (ValueError,mail.ThreadReview) as ex:return jsonify({'error':str(ex)}),400
        except Exception:return jsonify({'error':'Billing service could not complete this step. No automatic retry will send an uncertain package.'}),503
