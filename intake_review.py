"""Signed portal workflow for held assignment emails. Originals are never deleted."""
import hashlib, hmac, json, os, re, time, urllib.parse
from datetime import date
import graph_import as graph
import operations_intake as intake

PATH = '/api/portal/intake-review'
STATES = {'Needs review', 'Import failed', 'Received'}

def authenticated(raw, headers, secret):
    stamp = headers.get('X-FDGC-Time', '')
    if not secret or not stamp.isdigit() or abs(time.time()-int(stamp)) > 300:
        return False
    canonical = 'fdgc-mail-bridge-v1\n'+stamp+'\nPOST\n'+PATH+'\n'+hashlib.sha256(raw).hexdigest()
    return hmac.compare_digest(hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest(), headers.get('X-FDGC-Signature',''))

def records(store):
    sid = intake.ledger_id(store)
    sheet = store.get_sheet(sid, force=True)
    return sid, [intake.vals(sheet, row) for row in sheet.get('rows', [])]

def find_message(token, key):
    # Resolve by stable source hash, never by a browser-supplied mailbox or Graph ID.
    user = urllib.parse.quote(graph.mailbox(), safe='')
    for name in ('Manual Review', 'Import Failed'):
        folder = graph.find_mail_folder_id(token, name)
        if not folder:
            continue
        url = graph.GRAPH_ROOT+'/users/'+user+'/mailFolders/'+urllib.parse.quote(folder,safe='')+'/messages?$top=100&$select=id,internetMessageId'
        while url:
            if not url.startswith(graph.GRAPH_ROOT+'/users/'+user+'/'):
                raise ValueError('Unexpected source page.')
            page = graph._request('GET',url,token=token)
            for item in page.get('value',[]):
                if hashlib.sha256(str(item.get('internetMessageId') or item['id']).encode()).hexdigest() == key:
                    return graph.get_message_details(token,item['id'])
            url = page.get('@odata.nextLink','')
    raise ValueError('This email is no longer in the review folders. Refresh the queue before continuing.')

def reviewed_item(payload, message):
    item = {k:str(payload.get(k,'')).strip() for k in ('address','city','market','type','pid','wo','ntp_due','body')}
    if item['market'] not in intake.AREA or not item['address'] or len(item['address'])>250:
        raise ValueError('Enter a job location and select Florence or Columbia.')
    if item['type'] not in ('Standard','CRQ','Outage'):
        raise ValueError('Select Regular, CRQ or Outage. A manager can convert the job to Project afterward.')
    if not item['body'] or len(item['body'])>3000:
        raise ValueError('Enter reviewed work instructions, up to 3,000 characters.')
    if item['ntp_due']:
        date.fromisoformat(item['ntp_due'])
    item['subject'] = message.get('subject','')
    return item

def handle(store, db, payload):
    if payload.get('role') not in ('office','lead') or not str(payload.get('actor','')).strip():
        raise ValueError('Office or lead technician access required.')
    key = str(payload.get('messageKey',''))
    if not re.fullmatch(r'[a-f0-9]{64}',key):
        raise ValueError('Select an email from Manual Review.')
    sid, rows = records(store)
    row = next((r for r in rows if r.get('Message Key') == key),None)
    if not row:
        raise ValueError('Email intake record not found.')
    if row.get('State') == 'Imported':
        return {'jobKey':row.get('Job Key',''),'message':'This email has already been imported.'}
    if row.get('State') not in STATES:
        raise ValueError('This record is not awaiting intake review.')
    token = graph.access_token()
    message = find_message(token,key)
    if payload.get('action') == 'detail':
        # Raw source is office-only; leads receive the subject, reason and operational form.
        result = {'subject':message.get('subject',''),'received':message.get('receivedDateTime',''),'reason':row.get('Detail','')}
        if payload['role']=='office':
            result['body'] = graph._plain_text(message.get('body',{}).get('content',''))[:24000]
            result['files'] = [a.get('name','File') for a in graph.list_attachments(token,message['id'])]
        return result
    if payload.get('action') != 'resolve':
        raise ValueError('Unknown review action.')
    item = reviewed_item(payload,message)
    expected = str(payload.get('target',''))
    found = intake.locate(store,item,intake.thread_match(store,message))
    if expected:
        if not found or found[0].lower()+':'+str(found[2]['id']) != expected:
            raise ValueError('The reviewed details do not uniquely match the selected existing job. Check its location, PID and work order.')
        if found[0]=='History':
            raise ValueError('Historical jobs require office review before changing their source record.')
    elif found:
        raise ValueError('An existing job matches this email. Select that job to link the source instead of creating a duplicate.')
    elif item['type']=='Standard' and not item['ntp_due']:
        raise ValueError('Enter the original NTP completion date. The job due date will be one calendar day earlier.')
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS portal_intake_review (message_key TEXT PRIMARY KEY, actor TEXT, started REAL, job_key TEXT)')
        prior = c.execute('SELECT * FROM portal_intake_review WHERE message_key=?',(key,)).fetchone()
        if prior:
            raise ValueError('A review save is already recorded or awaiting confirmation. Refresh the queue and check the linked job before retrying.')
        c.execute('INSERT INTO portal_intake_review VALUES (?,?,?,?)',(key,payload['actor'],time.time(),''))
    # Keep the durable claim after uncertain network errors; never automatically repeat a partial import.
    files = graph.list_attachments(token,message['id'])
    stats = dict(created=0,updated=0,failed=0,ignored=0,replies=0)
    jobkey = intake.save_message(store,token,message,item,files,key,stats,db=db)
    if expected and jobkey != expected:
        raise ValueError('Source job changed while saving. Office review required.')
    area,rid = jobkey.split(':')
    attachments = store.list_row_attachments(intake.AREA[area.title()],int(rid))
    if not any(a.get('name','').lower().endswith('.eml') for a in attachments):
        raise ValueError('Original email retention could not be confirmed. Review the saved job before retrying.')
    detail = 'Reviewed in portal by '+str(payload['actor'])[:200]+'. Source email and work files retained.'
    if stats.get('acknowledgment'):detail+=' Receipt acknowledgment: '+stats['acknowledgment']+'.'
    intake.audit(store,sid,key,State='Imported',Detail=detail,**{'Job Key':jobkey})
    with db() as c:
        c.execute('INSERT OR REPLACE INTO operations_mail VALUES (?,?,?)',(key,jobkey,'Imported'))
        c.execute('UPDATE portal_intake_review SET job_key=? WHERE message_key=?',(jobkey,key))
    warning = ''
    try:
        graph.move_message(token,message['id'],graph.ensure_mail_folder(token,'Imported'))
    except Exception:
        warning = ' The original remains in Outlook; its folder move needs office attention.'
    return {'jobKey':jobkey,'message':'Job saved. Original email and work files retained.'+warning}

def install(app, store, db):
    from flask import request, jsonify
    @app.post(PATH)
    def portal_intake_review():
        secret=os.getenv('GRAYCLIFF_BRIDGE_SECRET','') or store.token
        if not authenticated(request.get_data(),request.headers,secret):
            return jsonify({'error':'Unauthorized'}),403
        try:
            return jsonify(handle(store,db,request.get_json() or {}))
        except ValueError as exc:
            return jsonify({'error':str(exc)}),400
        except Exception:
            return jsonify({'error':'Review could not confirm the result. Refresh and check for a saved job before retrying.'}),503
