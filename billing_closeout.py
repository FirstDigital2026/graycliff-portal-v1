"""Record Jason's issued bills from the shared inbox. Never sends billing mail."""
import hashlib,json,re,time
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from datetime import datetime,timezone
import financial_spreadsheets as sheets
import financial_workflow as workflow


def initialize(db):
    sheets.initialize(db)
    with db() as c:c.execute('CREATE TABLE IF NOT EXISTS private_billing_closeout_messages (message_key TEXT PRIMARY KEY,mime BLOB NOT NULL,mime_hash TEXT NOT NULL,snapshot TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL)')


def candidate(message,attachments):
    sender=(message.get('from') or {}).get('emailAddress',{}).get('address','').lower()
    return sender=='jason.mosqueda@firstdigitalsc.com' and any(re.search(r'work[ _-]*completed.*\.xlsx$',str(a.get('name','')),re.I) for a in attachments)


def validate_message(message,raw):
    parsed=BytesParser(policy=policy.default).parsebytes(raw)
    sender=(message.get('from') or {}).get('emailAddress',{}).get('address','').lower()
    if sender!='jason.mosqueda@firstdigitalsc.com':raise ValueError('Issued billing must come directly from Jason; original is retained.')
    recipients=(message.get('toRecipients') or [])+(message.get('ccRecipients') or [])
    if not any(r.get('emailAddress',{}).get('address','').lower().endswith('@graycliffent.com') for r in recipients):raise ValueError('Customer recipient could not be verified on the issued billing email.')
    subject=message.get('subject','')
    if re.search(r'\btest\b|\bdraft\b|\bquote\b',subject,re.I):raise ValueError('Draft, quote and test emails cannot close jobs.')
    body=re.sub(r'<[^>]+>',' ',(message.get('body') or {}).get('content',''))
    fresh=re.split(r'(?im)^\s*(?:from:|on .+wrote:|_{5,}|-{5,})',body)[0]
    if not re.search(r'\bbill(?:ed|ing)?\b',subject+' '+fresh,re.I):raise ValueError('The current email does not explicitly identify issued billing.')
    when=parsedate_to_datetime(str(parsed.get('Date','')))
    if not when or when.tzinfo is None or when>datetime.now(timezone.utc):raise ValueError('The issued email date could not be verified.')
    return when.isoformat()


def jobs(store):
    from operations_intake import SEARCH_SHEETS,vals
    result={}
    for area,sid in SEARCH_SHEETS.items():
        sheet=store.get_sheet(sid,force=True)
        for row in sheet.get('rows',[]):
            v=vals(sheet,row)
            if not v.get('Task Name') or v.get('Status')=='Canceled':continue
            canonical=str(v.get('Portal Billing Source Sheet') or sid)+':'+str(v.get('Portal Billing Source Row') or row['id'])
            item={'title':v['Task Name'],'sourceKey':canonical,'sheetId':canonical.split(':')[0],'rowId':canonical.split(':')[1],'jobKey':str(sid)+':'+str(row['id']),
                  'jobStatus':v.get('Status',''),'billingStatus':v.get('Portal Billing Status',''),'area':area}
            # A transferred history row keeps its original billing identity.
            if canonical not in result or area=='History':result[canonical]=item
    return list(result.values())


def ingest(store,db,message,attachments,key,token):
    import graph_import as graph
    from mail_automation import bridge
    initialize(db)
    raw=graph.get_message_mime(token,message['id'])
    if not raw or len(raw)>25_000_000:raise ValueError('Billing source exceeds private retention limit.')
    digest=hashlib.sha256(raw).hexdigest()
    with db() as c:
        c.execute('INSERT OR IGNORE INTO private_billing_closeout_messages VALUES (?,?,?,?,?,?)',(key,raw,digest,'[]','Received',time.time()))
        saved=c.execute('SELECT * FROM private_billing_closeout_messages WHERE message_key=?',(key,)).fetchone()
    if hashlib.sha256(saved['mime']).hexdigest()!=saved['mime_hash']:raise RuntimeError('Private billing source integrity check failed.')
    workflow.record(db,key,'Issued billing','Processing','Verifying the issued workbook and matching job.',message.get('subject',''),message.get('receivedDateTime',''))
    if saved['state']=='Completed':return {'state':'Completed','detail':'Previously recorded billing verified; no duplicate closeout.'}
    sent=validate_message(message,saved['mime']);available=jobs(store);records=[]
    retained=BytesParser(policy=policy.default).parsebytes(saved['mime'])
    for part in retained.walk():
        filename=str(part.get_filename() or '')
        if not re.search(r'work[ _-]*completed.*\.xlsx$',filename,re.I):continue
        job=sheets.match_job(filename,available)
        if not job:raise ValueError('No unique job matches '+filename+'. Original retained; no automatic closeout.')
        if job['jobStatus'] not in ('Field complete','Complete','Billed'):raise ValueError('Matched job is not field complete; office review required.')
        record=sheets.extract_total(part.get_payload(decode=True) or b'')
        record.update(job);record.update(source='Issued work completed spreadsheet',sourceFile=filename,sentAt=sent,
                                        subject=message.get('subject',''),privateMessageKey=key)
        with db() as c:prior=c.execute('SELECT snapshot FROM financial_spreadsheet_imports WHERE source_key=?',(job['sourceKey'],)).fetchone()
        if prior and json.loads(prior['snapshot']).get('amountCents')!=record['amountCents']:raise ValueError('Issued amount conflicts with previously retained billing; office review required.')
        from financial_report import report
        with db() as c:has_outbox=c.execute("SELECT 1 FROM sqlite_master WHERE name='billing_package_outbox'").fetchone()
        accepted=next((j for j in report(db)['jobs'] if j['sourceKey']==job['sourceKey'] and j.get('packageId')),None) if has_outbox else None
        if accepted and accepted.get('amountCents')!=record['amountCents']:raise ValueError('Issued amount conflicts with an accepted portal billing package; office review required.')
        records.append(record)
    if not records or len({r['sourceKey'] for r in records})!=len(records):raise ValueError('Issued workbook identity is missing or duplicated.')
    with db() as c:c.execute('UPDATE private_billing_closeout_messages SET snapshot=?,state=? WHERE message_key=?',(json.dumps(records),'Verified',key))
    warnings=[]
    for record in records:
        receipt=bridge(store,'POST','/fdgc/v1/billing-email-closeout',{'operationId':hashlib.sha256((key+record['fileSha256']).encode()).hexdigest(),
          'jobKey':record['jobKey'],'expectedTitle':record['title'],'sentAt':sent,'reference':'Jason issued billing: '+message.get('subject','')+' | '+record['sourceFile']+' | source '+key})
        if not receipt.get('ok'):raise ValueError(receipt.get('message','The portal did not confirm billing closeout.'))
        if receipt.get('warning'):warnings.append(receipt['warning'])
        with db() as c:c.execute('INSERT OR REPLACE INTO financial_spreadsheet_imports VALUES (?,?,?)',(record['sourceKey'],json.dumps(record),time.time()))
    state='Needs attention' if warnings else 'Completed'
    detail=' '.join(warnings) if warnings else str(len(records))+' issued bill(s) recorded; completed jobs removed from Ready to bill. No billing email sent.'
    with db() as c:c.execute('UPDATE private_billing_closeout_messages SET state=? WHERE message_key=?',(state,key))
    workflow.record(db,key,'Issued billing',state,detail)
    return {'state':state,'detail':detail}
