"""Dedicated mailbox intake into the existing operational area sheets.

Uses existing Render credentials; never exports them to WordPress or the browser.
Only the original graycliff Render service consumes the mailbox.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
import graph_import as graph

AREA = {'Florence': 2515846899388292, 'Columbia': 8067039099834244}
LEDGER = 'Graycliff Email Intake'
GUARD = threading.Lock()
EXTRA = ['Portal Project ID', 'Portal Work Order', 'Portal NTP Due Date',
         'Portal Intake Key', 'Portal Job Type', 'Portal Incident ID',
         'Portal Claimed By', 'Assigned Technician', 'Portal Assigned At']

def now():
    return datetime.now(timezone.utc).isoformat()

def internal_due(original):
    return (date.fromisoformat(original) - timedelta(days=1)).isoformat() if original else ''

def vals(sheet, row):
    columns = {str(c['id']): c['title'] for c in sheet['columns']}
    return {columns.get(str(c['columnId']), ''): c.get('value', c.get('displayValue', '')) for c in row.get('cells', [])}

def write(store, sid, values, rid=None):
    # Mutation timeouts are ambiguous. Never blindly retry a row creation.
    sheet = store.get_sheet(sid, force=True)
    cols = {c['title']: c['id'] for c in sheet['columns']}
    cells = [{'columnId': cols[k], 'value': v, 'strict': False} for k,v in values.items() if k in cols]
    row = {'id': rid, 'cells': cells} if rid else {'toBottom': True, 'cells': cells}
    result = store._request('PUT' if rid else 'POST', f'/sheets/{sid}/rows', body=[row], retries=1)
    store.invalidate(f'sheet:{sid}')
    return result['result'][0]

def ensure_columns(store, sid, names):
    sheet = store.get_sheet(sid, force=True)
    existing = {c['title'] for c in sheet['columns']}
    add = [{'title': n, 'type': 'TEXT_NUMBER', 'index': len(existing)} for n in names if n not in existing]
    if add:
        store._request('POST', f'/sheets/{sid}/columns', body=add, retries=1)
        store.invalidate(f'sheet:{sid}')
    return store.get_sheet(sid, force=True)

def ledger_id(store):
    sheets = [s for s in store.list_sheets() if s['name'] == LEDGER]
    if len(sheets) > 1:
        raise RuntimeError('Duplicate intake sheets require office review.')
    if sheets:
        return sheets[0]['id']
    columns = [{'title': 'Message Key', 'type': 'TEXT_NUMBER', 'primary': True}]
    columns += [{'title': n, 'type': 'TEXT_NUMBER'} for n in ['Subject','Received At','State','Detail','Job Key','Last Checked']]
    return store._request('POST','/sheets', body={'name': LEDGER, 'columns': columns}, retries=1)['result']['id']

def audit(store, sid, key, **values):
    sheet = store.get_sheet(sid, force=True)
    found = [r for r in sheet.get('rows',[]) if str(vals(sheet,r).get('Message Key','')) == key]
    return write(store,sid,{'Message Key':key,'Last Checked':now(),**values},found[0]['id'] if found else None)

def parse(message, attachments):
    subject = str(message.get('subject',''))
    body = graph._plain_text((message.get('body') or {}).get('content',''))
    ntp = graph.parse_recognized_ntp(message)
    pid = re.search(r'\b(?:PID|PRISM(?:\s+ID)?)\s*[:#]?\s*(\d{6,12})(?!\d)', subject,re.I)
    crq = re.search(r'\bCRQ\d+\b',subject,re.I)
    outage = bool(re.search(r'\b(?:no light|down fiber|outage|request map location|emergency)\b',subject,re.I))
    if not ntp and not pid and not crq and not outage:
        raise ValueError('Email format needs review: no reliable PID, NTP, CRQ or outage subject.')
    parsed = ntp or graph.ParsedNtp('',pid.group(1) if pid else '',subject)
    # A coordinator's signature address is never a job location.
    if ntp:
        parsed.address = ''
        parsed.city = ''
    parsed = graph.enrich_ntp_from_attachments(parsed,attachments)
    for filename,text in graph._candidate_document_texts(attachments):
        document_pid = re.search(r'PRISM\s+ID\s*:\s*(\d+)',text,re.I)
        if not document_pid or (pid and document_pid.group(1)!=pid.group(1)):
            continue
        work_order = re.search(r'Work\s+Order\s*#?\s*:\s*(\d+)',text,re.I)
        city = re.search(r'\b([A-Za-z]+(?:\s+[A-Za-z]+)?),\s*SC\s+\d{5}\b',text,re.I)
        if work_order:
            parsed.work_order = work_order.group(1)
        if city:
            parsed.city = city.group(1).strip().title()
        lines=text.splitlines()
        for index,line in enumerate(lines):
            if 'Job Address' in line and 'Site Contact' in line:
                left,right=line.index('Job Address'),line.index('Site Contact')
                candidate=next((v for v in lines[index+1:] if v.strip()),'')[left:right].strip()
                if re.match(r'^\d+\s+',candidate):
                    parsed.address=candidate
    if pid and parsed.prism and pid.group(1) != parsed.prism:
        raise ValueError('Subject PID conflicts with the attached NTP. Review the documents.')
    if not parsed.prism and pid:
        parsed.prism = pid.group(1)
    # Address in the subject is safer than a coordinator's address in a signature.
    address = re.search(r'\b(\d{1,6}\s+(?:US\s+\d+|[A-Za-z0-9 .#\'-]+?\s(?:RD|ROAD|ST|STREET|AVE|AVENUE|DR|DRIVE|CT|COURT|LN|LANE|HWY|HIGHWAY)))(?![A-Za-z])',subject,re.I)
    if address:
        parsed.address = address.group(1).strip()
    if outage:
        # Incident emails frequently have no PID and put the site only in the body.
        # Require a full location with city/state; exclude known signature offices.
        pattern=r'\b(\d{1,6}\s+[A-Za-z0-9 .#\'-]{2,65}?\s(?:RD|ROAD|ST|STREET|AVE|AVENUE|DR|DRIVE|CT|COURT|LN|LANE|HWY|HIGHWAY))\.?\s*(?:NE|NW|SE|SW)?[, ]+([A-Za-z ]{2,30})[, ]+SC\b'
        sites={}
        for found in re.finditer(pattern,body,re.I):
            street,city=found.group(1).strip(),found.group(2).strip()
            compact=re.sub(r'[^a-z0-9]','',street.lower())
            if compact in {'170pontiaccenterdr','3998claussenroad','3235scashuadr'}:
                continue
            if parsed.address and re.sub(r'[^a-z0-9]','',parsed.address.lower())!=compact:
                continue
            sites[compact]=(street,city)
        if len(sites)==1:
            parsed.address,parsed.city=next(iter(sites.values()))
        elif len(sites)>1:
            raise ValueError('Incident email contains multiple possible sites. Office review is required.')
        if parsed.city.lower()=='orangeburge':
            parsed.city='Orangeburg'
    location_text = subject + ' ' + parsed.city + ' ' + parsed.address
    if not parsed.city:
        cities = graph.FLORENCE_CITIES | graph.COLUMBIA_CITIES | {'cheraw','sumter','orangeburg','barnwell','blair','lynchburg'}
        matches = [c for c in cities if re.search(r'\b'+re.escape(c)+r'\b',location_text,re.I)]
        if matches:
            parsed.city = max(matches,key=len).title()
    parsed.market = graph._market_for(parsed.city,parsed.address)
    if parsed.city.lower() in {'cheraw','lynchburg'}:
        parsed.market = 'Florence'
    if parsed.city.lower() in {'orangeburg','barnwell','blair'}:
        parsed.market = 'Columbia'
    # Do not use dates in a quoted chain as a current NTP deadline.
    return {'pid':parsed.prism,'wo':parsed.work_order,'address':parsed.address,
            'city':parsed.city,'market':parsed.market,'ntp_due':parsed.due_date,
            'type':'Outage' if outage else ('CRQ' if crq else 'Standard'),
            'subject':subject,'body':body}

def locate(store, item):
    found = []
    for market,sid in AREA.items():
        sheet = store.get_sheet(sid,force=True)
        for row in sheet.get('rows',[]):
            v = vals(sheet,row)
            title = str(v.get('Task Name',''))
            match_pid = item['pid'] and (str(v.get('Portal Project ID','')) == item['pid'] or re.search(r'(?<!\d)'+re.escape(item['pid'])+r'(?!\d)',title))
            match_wo = item['wo'] and str(v.get('Portal Work Order','')) == item['wo']
            normalize = lambda text: re.sub(r'[^a-z0-9]','',str(text).lower())
            address = normalize(item['address'])
            # Legacy rows often have a CRQ in the title but no PID column.
            # Accept only the full extracted address on one unique row; never fuzzy-match.
            address_pattern=r'(?<![a-z0-9])'+r'[\W_]*'.join(map(re.escape,address))+r'(?![a-z0-9])'
            match_address = (len(address)>7 and re.search(address_pattern,title.lower())
                             and not v.get('Portal Project ID')
                             and (not item['city'] or not v.get('City') or normalize(v['City']).startswith(normalize(item['city']))))
            if match_pid or match_wo or match_address:
                found.append((market,sid,row,v))
    if len(found)>1:
        raise ValueError('Multiple existing jobs match this identifier. Review before changing a job.')
    return found[0] if found else None

def save_message(store, token, message, item, attachments, key, state):
    found = locate(store,item)
    if found:
        market,sid,row,old = found
        if str(old.get('Portal Billing Status','')) == 'Submitted' or re.search(r'complete|billed',str(old.get('Status','')),re.I):
            raise ValueError('Email matches completed work. Office must review before reopening it.')
    else:
        market = item['market']
        if market not in AREA or not item['address']:
            raise ValueError('Area or job location is missing. Office review required before releasing to techs.')
        if item['type']=='Standard' and not item['ntp_due']:
            raise ValueError('NTP completion date is missing. Office review required before scheduling this normal job.')
        if item['type']=='Outage' and re.search(r'\b(?:resolved|restored|completed)\b',item['body'],re.I):
            raise ValueError('Incident thread includes a resolution. Review before opening another active outage.')
        sid = AREA[market]
        row,old = None,{}
    ensure_columns(store,sid,EXTRA)
    values = {'Portal Project ID':item['pid'] or old.get('Portal Project ID',''),
              'Portal Work Order':item['wo'] or old.get('Portal Work Order','')}
    # Existing CRQ classification and scheduled windows always take precedence.
    jobtype = str(old.get('Portal Job Type','')) or ('CRQ' if re.search('CRQ',str(old.get('Task Name','')),re.I) else item['type'])
    values['Portal Job Type'] = jobtype
    if item['ntp_due']:
        values['Portal NTP Due Date'] = item['ntp_due']
        if jobtype == 'Standard':
            values['Due Date'] = internal_due(item['ntp_due'])
    if not row:
        # Include the stable intake key in the creation itself for ambiguous failures.
        sheet = store.get_sheet(sid,force=True)
        prior = [r for r in sheet.get('rows',[]) if str(vals(sheet,r).get('Portal Intake Key','')) == key]
        if prior:
            row = prior[0]
        else:
            incident = 'EV-'+hashlib.sha256(key.encode()).hexdigest()[:12].upper() if jobtype=='Outage' else ''
            values.update({'Task Name':(item['pid']+' · ' if item['pid'] else '')+item['address'],
                           'City':item['city'],'Portal Intake Key':key,'Portal Incident ID':incident,
                           'Notes':'Email intake: '+item['subject']+'\n'+item['body'][:3000]})
            if jobtype=='Outage':
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%dT%H:%M')
                roster = [s for s in store.list_sheets() if s['name']=='Graycliff On Call Rotation']
                if len(roster)==1:
                    schedule=store.get_sheet(roster[0]['id'],force=True)
                    active=[vals(schedule,r) for r in schedule.get('rows',[]) if vals(schedule,r).get('Area')==market and str(vals(schedule,r).get('Start Date',''))<=today<str(vals(schedule,r).get('End Date','')) and vals(schedule,r).get('Email') and vals(schedule,r).get('Not On Call')!='Yes']
                    if len(active)==1:
                        values.update({'Portal Claimed By':active[0].get('Email',''),'Assigned Technician':active[0].get('Technician',''),'Portal Assigned At':now(),'Status':'Assigned'})
            row = write(store,sid,values)
            state['created'] += 1
    else:
        write(store,sid,values,row['id'])
        state['updated'] += 1
    names = {a['name'] for a in store.list_row_attachments(sid,row['id'])}
    for attachment in attachments:
        if attachment.get('isInline'):
            continue
        decoded = graph.attachment_bytes(attachment)
        if not decoded:
            continue
        name,mime,data = decoded
        # Hash content, so revised files with the same filename are retained.
        stem,ext = os.path.splitext(name)
        name = stem[:110]+'-'+hashlib.sha256(data).hexdigest()[:10]+ext
        if name not in names:
            store.attach_file_to_row(sid,row['id'],filename=name,mime_type=mime,data=data)
            names.add(name)
    eml_name = 'Email-'+key[:20]+'.eml'
    if eml_name not in names:
        data = graph.get_message_mime(token,message['id'])
        if not data:
            raise RuntimeError('Original email copy unavailable; intake requires review.')
        store.attach_file_to_row(sid,row['id'],filename=eml_name,mime_type='message/rfc822',data=data)
    return market.lower()+':'+str(row['id'])

def run(store, db):
    # Three historical services use this repository. Only one may consume Inbox.
    if os.getenv('RENDER_SERVICE_ID','') != 'srv-d9p0jflbedkc73ds5tn0':
        return {'ok':True,'configured':False,'message':'Mailbox processing is owned by the original graycliff service.'}
    if not GUARD.acquire(blocking=False):
        return {'ok':True,'message':'Intake already running.'}
    sid = None
    try:
        sid = ledger_id(store)
        token = graph.access_token()
        imported = graph.ensure_mail_folder(token,'Imported')
        review = graph.ensure_mail_folder(token,'Manual Review')
        failed = graph.ensure_mail_folder(token,'Import Failed')
        stats = {'created':0,'updated':0,'failed':0,'ignored':0,'replies':0}
        messages = graph.list_recent_messages(token,top=40)
        for message in reversed(messages):
            key = hashlib.sha256(str(message.get('internetMessageId') or message['id']).encode()).hexdigest()
            with db() as connection:
                connection.execute('CREATE TABLE IF NOT EXISTS operations_mail (message_key TEXT PRIMARY KEY, job_key TEXT, state TEXT)')
                prior = connection.execute('SELECT * FROM operations_mail WHERE message_key=?',(key,)).fetchone()
            phase='recording receipt'
            try:
                if prior and prior['state']=='Imported':
                    graph.move_message(token,message['id'],imported)
                    continue
                audit(store,sid,key,Subject=message.get('subject',''),**{'Received At':message.get('receivedDateTime',''),'State':'Received','Detail':'Processing email and documents.'})
                phase='reading documents'
                attachments = graph.list_attachments(token,message['id']) if message.get('hasAttachments') else []
                item = parse(message,attachments)
                phase='saving job and source files'
                jobkey = save_message(store,token,message,item,attachments,key,stats)
                phase='confirming import'
                audit(store,sid,key,State='Imported',Detail='Job and original email saved; existing field status preserved.',**{'Job Key':jobkey})
                with db() as connection:
                    connection.execute('INSERT OR REPLACE INTO operations_mail VALUES (?,?,?)',(key,jobkey,'Imported'))
                graph.move_message(token,message['id'],imported)
            except ValueError as exc:
                audit(store,sid,key,State='Needs review',Detail=str(exc))
                graph.move_message(token,message['id'],review)
                stats['ignored'] += 1
            except Exception as exc:
                # Do not record provider payloads, credentials or private HTTP data.
                diagnostic=type(exc).__name__
                if diagnostic=='SmartsheetError':
                    diagnostic+=': '+str(exc)[:700]
                audit(store,sid,key,Subject=message.get('subject',''),State='Import failed',Detail=f'Failed while {phase}. {diagnostic}. Check any partially saved job before retrying.',**{'Received At':message.get('receivedDateTime','')})
                graph.move_message(token,message['id'],failed)
                stats['failed'] += 1
        audit(store,sid,'SYSTEM',Subject='Graycliff mailbox connection',State='Connected',Detail='Inbox checked. '+json.dumps(stats))
        return {'ok':True,'configured':True,**stats,'message':'Mailbox intake checked the operational area sheets.'}
    except Exception:
        if sid:
            try:
                audit(store,sid,'SYSTEM',Subject='Graycliff mailbox connection',State='Connection error',Detail='Automatic intake requires attention. Emails remain in the mailbox for review.')
            except Exception:
                pass
        raise
    finally:
        # Outage reminders must keep running even when mailbox intake fails.
        try:
            request=urllib.request.Request('https://firstdigitalsc.com/wp-cron.php',headers={'User-Agent':'Mozilla/5.0 FirstDigitalGraycliff/0.2'})
            urllib.request.urlopen(request,timeout=15).close()
        except Exception:
            pass
        GUARD.release()
