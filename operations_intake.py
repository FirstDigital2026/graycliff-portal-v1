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
import email
import email.policy
import mimetypes
import base64
from datetime import date, datetime, timedelta, timezone
import graph_import as graph

AREA = {'Florence': 2515846899388292, 'Columbia': 8067039099834244}
SEARCH_SHEETS = {**AREA, 'History':3889686678294404}
LEDGER = 'Graycliff Email Intake'
GUARD = threading.Lock()
EXTRA = ['Portal Project ID', 'Portal Work Order', 'Portal NTP Due Date',
         'Portal Intake Key', 'Portal Job Type', 'Portal Incident ID',
         'Portal Claimed By', 'Assigned Technician', 'Portal Assigned At',
         'Portal Email Threads', 'Portal Email Updated At']

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
    for market,sid in SEARCH_SHEETS.items():
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

def thread_match(store, message):
    thread = str(message.get('conversationId') or '')
    if not thread:
        return None
    matches = []
    for market,sid in SEARCH_SHEETS.items():
        sheet = store.get_sheet(sid,force=True)
        for row in sheet.get('rows',[]):
            old = vals(sheet,row)
            try:
                threads = json.loads(str(old.get('Portal Email Threads') or '[]'))
            except (ValueError,TypeError):
                threads = []
            if thread in threads:
                matches.append((market,sid,row,old))
    if len(matches)>1:
        raise ValueError('This email chain is linked to multiple jobs. Office review required.')
    return matches[0] if matches else None

def message_item(store, message, attachments):
    linked = thread_match(store,message)
    try:
        item = parse(message,attachments)
    except ValueError as exc:
        if not linked or not str(exc).startswith('Email format needs review:'):
            raise
        # Only an established chain may supply context for an otherwise unrecognized reply.
        subject = str(message.get('subject',''))
        if re.search(r'\b(?:PID|PRISM|CRQ)\s*[:#]?\s*\d+',subject,re.I):
            raise ValueError('Reply identifiers could not be verified. Office review required.')
        old=linked[3]
        item={'pid':str(old.get('Portal Project ID','')),'wo':str(old.get('Portal Work Order','')),
              'address':str(old.get('Task Name','')),'city':str(old.get('City','')),
              'market':linked[0],'ntp_due':'','type':str(old.get('Portal Job Type') or 'Standard'),
              'subject':subject,'body':graph._plain_text((message.get('body') or {}).get('content',''))}
    return item

def save_message(store, token, message, item, attachments, key, state):
    linked = thread_match(store,message)
    found = locate(store,item)
    if linked:
        if found and (found[1],found[2]['id'])!=(linked[1],linked[2]['id']):
            raise ValueError('Reply identifiers conflict with the job linked to this chain. Office review required.')
        old=linked[3]
        if item['pid'] and old.get('Portal Project ID') and item['pid']!=str(old['Portal Project ID']):
            raise ValueError('Reply PID differs from this chain’s job. Office review required.')
        if item['wo'] and old.get('Portal Work Order') and item['wo']!=str(old['Portal Work Order']):
            raise ValueError('Reply work order differs from this chain’s job. Office review required.')
        found=linked
    if found:
        market,sid,row,old = found
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
    closed = market=='History' or bool(old and (str(old.get('Portal Billing Status','')) == 'Submitted' or re.search(r'complete|billed',str(old.get('Status','')),re.I)))
    if item['ntp_due'] and not closed:
        values['Portal NTP Due Date'] = item['ntp_due']
        if jobtype == 'Standard':
            values['Due Date'] = internal_due(item['ntp_due'])
    thread=str(message.get('conversationId') or '')
    try:
        threads=json.loads(str(old.get('Portal Email Threads') or '[]'))
    except (ValueError,TypeError):
        threads=[]
    if thread and thread not in threads:
        threads.append(thread)
    values['Portal Email Threads']=json.dumps(threads)
    values['Portal Email Updated At']=message.get('receivedDateTime') or now()
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
    capture_email_files(store,token,message,sid,row['id'],attachments,key)
    marker='[Email intake '+key+']'
    discussions=store._request('GET',f"/sheets/{sid}/rows/{row['id']}/discussions",query={'includeAll':'true','include':'comments'}).get('data',[])
    if not any(marker in str(c.get('text','')) for d in discussions for c in d.get('comments',[])):
        sender=((message.get('from') or {}).get('emailAddress') or {}).get('address','')
        store.add_row_comment(sid,row['id'],marker+'\n'+str(message.get('receivedDateTime',''))+' · '+sender+'\n'+item['subject'][:500]+'\n'+item['body'][:7000])
    return market.lower()+':'+str(row['id'])

def capture_email_files(store,token,message,sid,row_id,attachments,key):
    names = {a['name'] for a in store.list_row_attachments(sid,row_id)}
    raw = graph.get_message_mime(token,message['id'])
    if not raw:
        raise RuntimeError('Original email copy unavailable; intake requires review.')
    eml_name = 'Email-'+key[:20]+'.eml'
    if eml_name not in names:
        store.attach_file_to_row(sid,row_id,filename=eml_name,mime_type='message/rfc822',data=raw)
    parts = source_attachments(raw)
    decoded_files = [d for a in attachments if (d:=graph.attachment_bytes(a))] + parts
    saved_hashes={m.group(1) for n in names if (m:=re.search(r'-([a-f0-9]{10})(?:\.[^.]+)?$',n))}
    for name,mime,data in decoded_files:
        # Hash content, so revised files with the same filename are retained.
        digest=hashlib.sha256(data).hexdigest()[:10]
        if digest in saved_hashes:
            continue
        stem,ext = os.path.splitext(name)
        name = stem[:110]+'-'+digest+ext
        if name not in names:
            store.attach_file_to_row(sid,row_id,filename=name,mime_type=mime,data=data)
            names.add(name)
        saved_hashes.add(digest)
    for attachment in attachments:
        kind=attachment.get('@odata.type','')
        if kind=='#microsoft.graph.referenceAttachment':
            raise ValueError('Cloud-linked attachment requires office review; original email retained. Download and attach the referenced file.')
        if kind=='#microsoft.graph.fileAttachment' and not graph.attachment_bytes(attachment) and not any(n==attachment.get('name') for n,_,_ in parts):
            raise ValueError('An attachment could not be extracted; original email retained. Office review required.')

def source_attachments(raw):
    """Keep regular files, inline/CID images, attached emails and data-URI pictures."""
    source=email.message_from_bytes(raw,policy=email.policy.default)
    files=[]
    for part in source.walk():
        mime=part.get_content_type();name=part.get_filename()
        if mime=='message/rfc822':
            payload=part.get_payload()
            if isinstance(payload,list) and payload:
                files.append((name or 'attached-message.eml',mime,payload[0].as_bytes(policy=email.policy.default)))
            continue
        if part.is_multipart():
            continue
        data=part.get_payload(decode=True)
        if not data:
            continue
        if name or part.get_content_disposition() in {'attachment','inline'} or part.get('Content-ID'):
            files.append((name or 'inline-picture'+(mimetypes.guess_extension(mime) or '.bin'),mime,data))
        if mime=='text/html':
            html=data.decode(part.get_content_charset() or 'utf-8',errors='replace')
            for image_mime,encoded in re.findall(r'data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)',html):
                try:
                    picture=base64.b64decode(re.sub(r'\s','',encoded),validate=True)
                except ValueError:
                    continue
                if picture:
                    files.append(('embedded-picture'+(mimetypes.guess_extension(image_mime) or '.bin'),image_mime,picture))
    return files

def seed_imported_threads(store, token, folder, db):
    # Link previously imported messages once, using the existing recorded job key.
    # Never infer a new job from historical mail during this migration.
    with db() as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS operations_mail (message_key TEXT PRIMARY KEY, job_key TEXT, state TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS operations_meta (key TEXT PRIMARY KEY, value TEXT)')
        if connection.execute("SELECT value FROM operations_meta WHERE key='thread_seed_v2'").fetchone():
            return
        recorded={r['message_key']:r['job_key'] for r in connection.execute("SELECT * FROM operations_mail WHERE state='Imported'").fetchall()}
    if recorded:
        import urllib.parse
        user=urllib.parse.quote(graph.mailbox(),safe='')
        fid=urllib.parse.quote(folder,safe='')
        result=graph._request('GET',f'{graph.GRAPH_ROOT}/users/{user}/mailFolders/{fid}/messages?$top=100&$orderby=receivedDateTime%20desc&$select=id,internetMessageId,conversationId',token=token)
        for message in result.get('value',[]):
            key=hashlib.sha256(str(message.get('internetMessageId') or message['id']).encode()).hexdigest()
            jobkey=recorded.get(key,'');thread=message.get('conversationId')
            if not jobkey or not thread:
                continue
            area,rid=jobkey.split(':',1);market=area.title();sid=SEARCH_SHEETS.get(market)
            if not sid:
                continue
            sheet=ensure_columns(store,sid,EXTRA)
            rows=[r for r in sheet.get('rows',[]) if str(r['id'])==rid]
            if len(rows)!=1:
                continue
            old=vals(sheet,rows[0]);threads=json.loads(str(old.get('Portal Email Threads') or '[]'))
            if thread not in threads:
                write(store,sid,{'Portal Email Threads':json.dumps(threads+[thread])},rows[0]['id'])
            try:
                capture_email_files(store,token,message,sid,rows[0]['id'],[],key)
            except Exception as exc:
                audit(store,ledger_id(store),key,State='Needs review',Detail='Attachment backfill needs office review ('+type(exc).__name__+'). Original email remains in Outlook.',**{'Job Key':jobkey})
    with db() as connection:
        connection.execute("INSERT OR REPLACE INTO operations_meta VALUES ('thread_seed_v2','done')")

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
        seed_imported_threads(store,token,imported,db)
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
                # Graph's hasAttachments flag is false for messages containing only inline pictures.
                attachments = graph.list_attachments(token,message['id'])
                item = message_item(store,message,attachments)
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
