"""Original-thread email updates, enabled only after an explicit activation time.

No historical job scan creates messages. Only committed, durable portal events
after installation enter the outbox. Paused drafts never become sendable merely
because an environment variable changes. Explicit release is required as well.
"""
from __future__ import annotations
import base64, hashlib, hmac, html, json, os, re, sqlite3, threading, time, uuid
import urllib.parse, urllib.request
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, formatdate
from pathlib import Path
import graph_import as graph
import completion_photos

SEND_ARMED = True  # User approved activation; the database policy is also required.
OWNER_SERVICE = 'srv-d9p0jflbedkc73ds5tn0'
SENDER = 'graycliffjobs@firstdigitalsc.com'
ROOT = 'https://firstdigitalsc.com/wp-json'
LOGO = 'https://firstdigitalsc.com/wp-content/uploads/2026/07/cropped-cropped-FirstDigital_Tagline_Clean_Fixed_WhiteBG.jpg'
GUARD = threading.Lock()
WAKE = threading.Event()
WAKE_GUARD = threading.Lock()

def wake(store,db):
    """Start processing now; coalesce repeated wakes without losing a newer update."""
    WAKE.set()
    if not WAKE_GUARD.acquire(blocking=False):return
    def worker():
        try:
            while WAKE.is_set():
                WAKE.clear();result=prepare(store,db)
                if result.get('skipped') and os.getenv('RENDER_SERVICE_ID','')==OWNER_SERVICE:
                    time.sleep(.25);WAKE.set()
        finally:
            WAKE_GUARD.release()
            if WAKE.is_set():wake(store,db)
    threading.Thread(target=worker,name='graycliff-status-email',daemon=True).start()

def install(app,store,db):
    from flask import request,jsonify
    path='/api/portal/process-updates'
    @app.post(path)
    def process_updates_now():
        raw=request.get_data();ts=request.headers.get('X-FDGC-Time','');given=request.headers.get('X-FDGC-Signature','');key=os.getenv('GRAYCLIFF_BRIDGE_SECRET','') or store.token
        canonical='fdgc-mail-bridge-v1\n'+ts+'\nPOST\n'+path+'\n'+hashlib.sha256(raw).hexdigest()
        if not ts.isdigit() or abs(time.time()-int(ts))>300 or not hmac.compare_digest(hmac.new(key.encode(),canonical.encode(),hashlib.sha256).hexdigest(),given):return jsonify({'error':'Unauthorized'}),403
        p=request.get_json(silent=True) or {}
        if not re.fullmatch(r'[a-f0-9-]{36}',str(p.get('eventId',''))):return jsonify({'error':'Invalid event reference'}),400
        wake(store,db)
        return jsonify({'queued':True,'eventId':p['eventId']}),202

class ThreadReview(RuntimeError):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def initialize(db):
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS mail_bridge_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mail_event_outbox (
          event_id TEXT PRIMARY KEY,seq INTEGER UNIQUE NOT NULL,job_key TEXT NOT NULL,
          payload TEXT NOT NULL,state TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',
          draft TEXT,created_at TEXT NOT NULL,prepared_at TEXT,approved_at TEXT,sent_at TEXT);
        ''')

def bridge(store, method, path, payload=None, after=None):
    body=json.dumps(payload,separators=(',',':')).encode() if payload is not None else b''
    ts=str(int(time.time()));key=os.getenv('GRAYCLIFF_BRIDGE_SECRET','') or store.token
    canonical='fdgc-mail-bridge-v1\n'+ts+'\n'+method+'\n'+path+'\n'+hashlib.sha256(body).hexdigest()
    signature=hmac.new(key.encode(),canonical.encode(),hashlib.sha256).hexdigest()
    query={'after':after} if after is not None else {}
    if method=='GET':query['_fdgc']=uuid.uuid4().hex
    url=ROOT+path+('?' + urllib.parse.urlencode(query) if query else '')
    req=urllib.request.Request(url,data=body if method=='POST' else None,method=method,
        headers={'Content-Type':'application/json','X-FDGC-Time':ts,'X-FDGC-Signature':signature,
                 'Cache-Control':'no-store, max-age=0','Pragma':'no-cache',
                 'User-Agent':'FirstDigitalGraycliffMailPreparation/1.0'})
    with urllib.request.urlopen(req,timeout=35) as response:
        return json.loads(response.read())

def eligible(e):
    if not e.get('committed') or e.get('action')!='status':
        return False
    return e.get('status')=='Field complete' or (
        e.get('job',{}).get('type')=='Outage' and e.get('status') in {'Today','En route','On site','Working','On hold','Canceled'})

def addresses(value):
    return [a.lower() for _,a in getaddresses([str(value or '')]) if re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+',a)]

def from_mime(raw):
    m=BytesParser(policy=policy.default).parsebytes(raw)
    return {'subject':str(m.get('Subject','')),'from':addresses(m.get('From','')),
            'to':addresses(m.get('To','')),'cc':addresses(m.get('Cc','')),
            'replyTo':addresses(m.get('Reply-To','')),'messageId':str(m.get('Message-ID','')).strip(),
            'references':re.findall(r'<[^<>\r\n]+>',str(m.get('References',''))),
            'parents':re.findall(r'<[^<>\r\n]+>',str(m.get('In-Reply-To',''))),
            'autoSubmitted':str(m.get('Auto-Submitted','no')).lower()}

def from_graph(m):
    headers={h['name'].lower():h['value'] for h in m.get('internetMessageHeaders',[])}
    def field(name):
        values=m.get(name,[])
        if isinstance(values,dict):values=[values]
        return [v.get('emailAddress',{}).get('address','').lower() for v in values if v.get('emailAddress',{}).get('address')]
    return {'subject':m.get('subject',''),'from':field('from'),'to':field('toRecipients'),
            'cc':field('ccRecipients'),'replyTo':field('replyTo'),'messageId':m.get('internetMessageId',''),
            'references':re.findall(r'<[^<>\r\n]+>',headers.get('references','')),
            'parents':re.findall(r'<[^<>\r\n]+>',headers.get('in-reply-to','')),
            'autoSubmitted':headers.get('auto-submitted','no').lower()}

def external(route):
    return any(not a.endswith('@firstdigitalsc.com') for a in route['from']+route['to']+route['cc'])

def exact_message(token, mailbox, message_id):
    query=urllib.parse.urlencode({'$filter':"internetMessageId eq '"+message_id.replace("'","''")+"'",
        '$select':'subject,from,toRecipients,ccRecipients,replyTo,internetMessageId,internetMessageHeaders','$top':'2'})
    result=graph._request('GET',graph.GRAPH_ROOT+'/users/'+urllib.parse.quote(mailbox,safe='')+'/messages?'+query,token=token)
    values=result.get('value',[])
    if len(values)>1:raise ThreadReview('More than one exact original message matched; office review required.')
    return from_graph(values[0]) if values else None

def validate_route(route):
    mid=route['messageId']
    if not re.fullmatch(r'<[^<>\s\r\n]+>',mid):raise ThreadReview('Original Message-ID is missing or invalid.')
    if route.get('autoSubmitted','no') not in {'','no'}:raise ThreadReview('Original message is automated; recipient review required.')
    to=list(dict.fromkeys((route['replyTo'] or route['from'])+route['to']))
    cc=list(dict.fromkeys(route['cc']))
    to=[a for a in to if a!=SENDER];cc=[a for a in cc if a!=SENDER and a not in to]
    if not to or not external(route):raise ThreadReview('No verified external original-thread recipient was found.')
    if any(re.search(r'(^|[._-])(?:no-?reply|do-?not-?reply)(@|[._-])',a,re.I) for a in to+cc):
        raise ThreadReview('Original recipients include a no-reply address; office review required.')
    return {**route,'to':to,'cc':cc}

def resolve_thread(store,token,e):
    sid=int(e['sheetId']);rid=int(e['rowId'])
    files=[a for a in store.list_row_attachments(sid,rid) if str(a.get('name','')).lower().endswith('.eml')]
    files.sort(key=lambda a:(a.get('createdAt',''),str(a.get('id',''))),reverse=True)
    if not files:raise ThreadReview('No original email is attached. Office must link the correct thread.')
    raw,_,_=store.download_attachment(sid,files[0]['id']);source=from_mime(raw)
    if external(source):return validate_route(source)
    # Forwarded mail addressed only to the shared inbox is not the customer chain.
    # Follow exact Message-ID references in the forwarding employee's mailbox.
    boxes=list(dict.fromkeys([a for a in source['from'] if a.endswith('@firstdigitalsc.com')]+[SENDER]))
    ids=list(dict.fromkeys(source['parents']+list(reversed(source['references']))))[:8]
    for mid in ids:
        for box in boxes:
            parent=exact_message(token,box,mid)
            if parent and external(parent):return validate_route(parent)
    raise ThreadReview('Forwarded intake email could not be linked to a unique original customer message. No recipients guessed.')

def resolve_field_thread(store,token,e):
    from field_thread import resolve
    return resolve(store,token,e,resolve_thread(store,token,e))

def draft_event(e,route):
    job=e.get('job',{});complete=e['status']=='Field complete'
    outage=job.get('type')=='Outage'
    hands_off=complete and job.get('type')=='CRQ'
    title='Hands off — job complete' if complete and (hands_off or outage) else ('Field work complete — moved to billing preparation' if complete else 'Outage progress update')
    location=', '.join(x for x in [job.get('title',''),job.get('city','')] if x)
    fields=[('Location',location),('Area',job.get('market',''))]
    if job.get('incidentId'):fields.append(('Incident',job['incidentId']))
    fields.append(('Status','Hands off' if hands_off else ('Scheduled for today' if e['status']=='Today' else e['status'])))
    if hands_off:
        noc=str(e.get('nocTechnician') or e.get('after',{}).get('Portal NOC Closed By') or '').strip()
        if not noc:raise ThreadReview('CRQ completion is missing the closing NOC technician. Office review required.')
        fields.append(('NOC ticket closed by',noc))
    # Only outage emails include submitted comments. Regular completions are
    # brief notices; CRQ completions announce hands off without field notes.
    note=(e.get('note','').strip() or 'Technician reported: '+e['status']+'.') if outage else ''
    ending='' if hands_off else ('The job has moved to billing preparation. The billing package will follow separately.' if complete else 'Progress will be updated as field reports are saved.')
    role='Operations' if e.get('actor',{}).get('role')=='office' else 'Field Technician'
    footer='This automated message was sent by the First Digital field processing app using '+('the saved job update.' if role=='Operations' else 'the technician’s submitted update.')
    comments=('Completion findings' if complete else 'Technician update')+':\n'+note if outage else ''
    text='\n\n'.join(part for part in [title,'\n'.join(k+': '+v for k,v in fields if v),comments,ending,'First Digital Operations\n'+SENDER+'\nSecond to None',footer] if part)
    esc=html.escape
    body='<h2>'+esc(title)+'</h2>'+''.join('<p><strong>'+esc(k)+':</strong> '+esc(v)+'</p>' for k,v in fields if v)
    if outage:body+='<p><strong>'+('Completion findings' if complete else 'Technician update')+'</strong></p><p>'+esc(note).replace('\n','<br>')+'</p>'
    if ending:body+='<p>'+esc(ending)+'</p>'
    body+='<hr>'
    body+='<img src="'+LOGO+'" width="170" alt="First Digital — Second to None"><p><strong>First Digital Operations</strong><br>'+SENDER+'</p><p><em>'+esc(footer)+'</em></p>'
    subject=route['subject'] if re.match(r'^\s*re:',route['subject'],re.I) else 'Re: '+route['subject']
    return {'subject':subject,'to':route['to'],'cc':route['cc'],'parent':route['messageId'],
            'references':list(dict.fromkeys(route.get('references',[])+[route['messageId']]))[-30:],
            'text':text,'html':body,'eventId':e['eventId'],'title':job.get('title',''),'jobKey':e['jobKey']}

def attach_update_photos(draft,event,store):
    photos=event.get('photos') or []
    if not photos:return draft
    if event.get('job',{}).get('type')!='Outage' or len(photos)>10 or len({str(p.get('id','')) for p in photos})!=len(photos):raise ThreadReview('Unexpected outage photo selection. Office review required.')
    allowed={str(a['id']):a for a in store.list_row_attachments(int(event['sheetId']),int(event['rowId']))}
    result={**draft,'inlinePhotos':[]};images=[];total=0
    for index,ref in enumerate(photos,1):
        aid=str(ref.get('id',''));sid=str(ref.get('sheetId',''));a=allowed.get(aid)
        if not aid.isdigit() or sid!=str(event['sheetId']) or not a or a.get('name')!=ref.get('name') or not re.fullmatch(r'Outage-update-[a-f0-9]{24}\.jpg',ref.get('name','')):raise ThreadReview('The update photo could not be verified on this job. No email sent.')
        raw,_,_=store.download_attachment(int(sid),int(aid))
        total+=len(raw)
        if total>2*1024*1024:raise ThreadReview('The selected update photos are too large for one email. Office review required; no photos were omitted.')
        if not raw or len(raw)>1024*1024 or not raw.startswith(b'\xff\xd8') or hashlib.sha256(raw).hexdigest()!=ref.get('sha256'):raise ThreadReview('The saved update photo changed or is unavailable. No email sent.')
        cid='fdgc-'+hashlib.sha256((event['eventId']+':'+aid).encode()).hexdigest()[:32]+'@firstdigitalsc.com'
        label='Outage update photo '+str(index)+' of '+str(len(photos))
        result['inlinePhotos'].append({'cid':cid,'name':'Outage-update-photo-'+str(index)+'.jpg','content':base64.b64encode(raw).decode('ascii')})
        images.append('<p><strong>'+label+'</strong><br><img src="cid:'+html.escape(cid)+'" alt="'+label+'" width="640" style="width:100%;max-width:640px;height:auto"></p>')
    result['html']=result['html'].replace('<hr>',''.join(images)+'<hr>',1)
    result['text']+='\n\n'+str(len(photos))+' outage update photo(s) included with this message.'
    return result

def mime_message(draft):
    m=EmailMessage(policy=policy.SMTP);m['From']='First Digital <'+SENDER+'>';m['To']=', '.join(draft['to'])
    if draft['cc']:m['Cc']=', '.join(draft['cc'])
    m['Subject']=draft['subject'];m['Date']=formatdate(localtime=False)
    m['Message-ID']='<fdgc-'+draft['eventId']+'@firstdigitalsc.com>'
    m['In-Reply-To']=draft['parent'];m['References']=' '.join(draft['references'])
    m['Auto-Submitted']='auto-generated';m['X-Auto-Response-Suppress']='All';m['X-FirstDigital-Event']=draft['eventId']
    # Short ASCII IDs avoid RFC 2047 encoding, which breaks Outlook cid matching.
    # Normalize saved drafts too, without changing their selected photo bytes.
    body=draft['html'];photos=[]
    for photo in draft.get('inlinePhotos',[]):
        cid=photo['cid'] if len(photo['cid'])<=60 else 'fdgc-'+hashlib.sha256(photo['cid'].encode()).hexdigest()[:32]+'@firstdigitalsc.com'
        body=body.replace('cid:'+photo['cid'],'cid:'+cid)
        photos.append({**photo,'cid':cid})
    m.set_content(draft['text']);m.add_alternative(body,subtype='html')
    for photo in photos:
        m.get_payload()[-1].add_related(base64.b64decode(photo['content'],validate=True),maintype='image',subtype='jpeg',cid='<'+photo['cid']+'>',filename=photo['name'],disposition='inline')
    for attachment in draft.get('attachments',[]):
        m.add_attachment(base64.b64decode(attachment['content'],validate=True),maintype='image',subtype='jpeg',filename=attachment['name'])
    return m.as_bytes()

def after_activation(event_time,activation_time):
    try:return datetime.fromisoformat(event_time.replace('Z','+00:00'))>=datetime.fromisoformat(activation_time.replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError):return False

def dispatch(db,event_id,store=None):
    # This guard executes before tokens, transport, or changes to sent status.
    if not SEND_ARMED:raise RuntimeError('Email sending is disarmed. No email was sent.')
    with db() as c:
        row=c.execute('SELECT * FROM mail_event_outbox WHERE event_id=?',(event_id,)).fetchone()
        if not row or row['state']!='READY' or not row['approved_at']:
            raise RuntimeError('This event has not been explicitly released for sending.')
        policy=c.execute("SELECT value FROM mail_bridge_meta WHERE key='live_after'").fetchone()
        e=json.loads(row['payload'])
        if not policy or not after_activation(e['at'],policy[0]):
            raise RuntimeError('Email sending is paused or this update predates activation.')
        if not eligible(e):
            c.execute("UPDATE mail_event_outbox SET state='NOT_REQUIRED',detail='Only outage field updates or job completion require email.' WHERE event_id=?",(event_id,))
            return
        if store is None:raise RuntimeError('Current job status must be verified before sending.')
        sheet=store.get_sheet(int(e['sheetId']),force=True)
        fresh=next((r for r in sheet.get('rows',[]) if str(r['id'])==str(e['rowId'])),None)
        columns={str(col['id']):col['title'] for col in sheet['columns']}
        values={columns.get(str(cell['columnId'])):cell.get('value','') for cell in (fresh or {}).get('cells',[])}
        expected=(e.get('after') or {}).get('Status') or {'Working':'In Progress','Field complete':'Complete'}.get(e['status'],e['status'])
        outage=e.get('job',{}).get('type')=='Outage'
        # Each committed outage report belongs on the original thread, even
        # when another field update arrives before this one reaches transport.
        # Keep completion-only mail protected against reopening a regular job.
        kind_changed=outage and values.get('Portal Job Type') not in (None,'','Outage')
        incident_changed=outage and values.get('Portal Incident ID') and e.get('job',{}).get('incidentId') and str(values['Portal Incident ID'])!=str(e['job']['incidentId'])
        if fresh is None or (not outage and values.get('Status')!=expected) or kind_changed or incident_changed or values.get('Portal Billing Status')=='Submitted':
            c.execute("UPDATE mail_event_outbox SET state='SUPERSEDED',detail='Job moved, changed incident/category, billing was submitted, or a non-outage completion was reopened. Email held.' WHERE event_id=?",(event_id,))
            return
        draft=json.loads(row['draft']);transport=base64.b64encode(mime_message(draft))
        if len(transport)>3900000:raise ThreadReview('The update email is too large. Office review required; no email sent.')
        changed=c.execute("UPDATE mail_event_outbox SET state='DISPATCHING' WHERE event_id=? AND state='READY'",(event_id,)).rowcount
        if changed!=1:raise RuntimeError('Email is already being processed.')
    try:
        graph._request('POST',graph.GRAPH_ROOT+'/users/'+urllib.parse.quote(SENDER,safe='')+'/sendMail',
            token=graph.access_token(),body=transport,headers={'Content-Type':'text/plain'})
    except Exception:
        with db() as c:c.execute("UPDATE mail_event_outbox SET state='UNCERTAIN',detail='Provider outcome uncertain. Check Sent Items before any manual retry.' WHERE event_id=?",(event_id,))
        raise
    with db() as c:c.execute("UPDATE mail_event_outbox SET state='ACCEPTED',sent_at=?,detail='Accepted by Microsoft; delivery is not confirmed.' WHERE event_id=?",(now(),event_id))

def backup_check(db):
    with db() as source:
        path=Path(source.execute('PRAGMA database_list').fetchone()[2])
        if not str(path) or str(path)=='.':return {'ok':False,'detail':'Database has no persistent file.'}
        folder=path.parent/'graycliff-backups';folder.mkdir(exist_ok=True)
        target=folder/('graycliff-'+datetime.now(timezone.utc).strftime('%Y-%m-%d')+'.sqlite3')
        if not target.exists():
            tmp=target.with_suffix('.partial');dest=sqlite3.connect(tmp)
            try:source.backup(dest);result=dest.execute('PRAGMA integrity_check').fetchone()[0]
            finally:dest.close()
            if result!='ok':raise RuntimeError('Database backup integrity check failed.')
            tmp.replace(target)
        backup=sqlite3.connect(target);restored=sqlite3.connect(':memory:')
        try:backup.backup(restored);ok=restored.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        finally:backup.close();restored.close()
        if not ok:raise RuntimeError('Database restore check failed.')
        return {'ok':True,'detail':'Daily database backup and isolated restore integrity check passed.','at':now()}

def report(db):
    with db() as c:
        policy=c.execute("SELECT value FROM mail_bridge_meta WHERE key='live_after'").fetchone()
        backup=c.execute("SELECT value FROM mail_bridge_meta WHERE key='backup_result'").fetchone()
        counts={r['state']:r['n'] for r in c.execute('SELECT state,count(*) n FROM mail_event_outbox GROUP BY state')}
        rows=c.execute("SELECT * FROM mail_event_outbox WHERE state!='NOT_REQUIRED' ORDER BY CASE WHEN state IN ('NEEDS_THREAD_REVIEW','NEEDS_ATTENTION','UNCERTAIN','UNCERTAIN_SOURCE') THEN 0 ELSE 1 END,seq DESC LIMIT 50").fetchall()
    drafts=[]
    for r in rows:
        d=json.loads(r['draft']) if r['draft'] else {}
        event=json.loads(r['payload'])
        drafts.append({'jobKey':r['job_key'],'title':d.get('title') or event.get('job',{}).get('title') or r['job_key'],'state':r['state'],
            'subject':d.get('subject',''),'recipients':d.get('to',[])+d.get('cc',[]),'text':d.get('text',''),'detail':r['detail']})
    live=bool(SEND_ARMED and policy and policy[0])
    return {'sending':'LIVE' if live else 'PAUSED','checkedAt':now(),'counts':counts,'drafts':drafts,'backup':json.loads(backup[0]) if backup else None,
            'liveAfter':policy[0] if live else None,
            'detail':'Email updates are enabled for new saved status changes. Previously paused drafts remain held.' if live else 'Updates are prepared for review. Sending is paused; paused drafts are never automatically released.'}

def prepare(store,db):
    if os.getenv('RENDER_SERVICE_ID','')!=OWNER_SERVICE:return {'skipped':True}
    if not GUARD.acquire(blocking=False):return {'skipped':True}
    try:
        initialize(db)
        with db() as c:
            row=c.execute("SELECT value FROM mail_bridge_meta WHERE key='cursor'").fetchone();cursor=int(row[0]) if row else 0
        response=bridge(store,'POST','/fdgc/v1/events',payload={'after':cursor})
        if 'maxSeq' in response and int(response['maxSeq'])<cursor:
            raise RuntimeError('Portal history was restored or rewound. Reconcile the email cursor before processing updates.')
        for e in response.get('events',[]):
            if not re.fullmatch(r'[a-f0-9-]{36}',str(e.get('eventId',''))):raise RuntimeError('Invalid event identifier.')
            state='NEW' if eligible(e) else ('NOT_REQUIRED' if e.get('committed') else 'UNCERTAIN_SOURCE')
            with db() as c:
                c.execute('INSERT OR IGNORE INTO mail_event_outbox(event_id,seq,job_key,payload,state,created_at) VALUES(?,?,?,?,?,?)',
                          (e['eventId'],e['seq'],e['jobKey'],json.dumps(e),state,e['at']))
                c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('cursor',?)",(str(e['seq']),))
        cutoff=datetime.fromtimestamp(time.time()-300,timezone.utc).isoformat()
        with db() as c:pending=c.execute("SELECT * FROM mail_event_outbox WHERE state='NEW' OR (state='NEEDS_ATTENTION' AND prepared_at<?) ORDER BY seq LIMIT 20",(cutoff,)).fetchall()
        token=None
        with db() as c:
            enabled=c.execute("SELECT value FROM mail_bridge_meta WHERE key='live_after'").fetchone()
        live_after=enabled[0] if SEND_ARMED and enabled else None
        for row in pending:
            e=json.loads(row['payload'])
            try:
                token=token or graph.access_token();route=resolve_field_thread(store,token,e);draft=draft_event(e,route)
                draft=completion_photos.attach(draft,e,store,bridge,ThreadReview) if e.get('status')=='Field complete' else attach_update_photos(draft,e,store)
                # Future activation applies only to new events after an explicit
                # activation timestamp. Existing PAUSED drafts never auto-release.
                ready=bool(live_after and after_activation(e['at'],live_after))
                with db() as c:changed=c.execute("UPDATE mail_event_outbox SET state=?,draft=?,prepared_at=?,approved_at=?,detail=? WHERE event_id=? AND state IN ('NEW','NEEDS_ATTENTION')",
                    ('READY' if ready else 'PAUSED',json.dumps(draft),now(),now() if ready else None,
                     'New event allowed by explicitly enabled policy.' if ready else 'Prepared only. Sending is disarmed.',e['eventId'])).rowcount
                if not changed:continue
                if ready:dispatch(db,e['eventId'],store)
            except ThreadReview as ex:
                with db() as c:c.execute("UPDATE mail_event_outbox SET state='NEEDS_THREAD_REVIEW',detail=? WHERE event_id=? AND state NOT IN ('ACCEPTED','DISPATCHING','UNCERTAIN','SUPERSEDED')",(str(ex),e['eventId']))
            except Exception:
                with db() as c:c.execute("UPDATE mail_event_outbox SET state='NEEDS_ATTENTION',prepared_at=?,detail='Preparation needs attention; no automatic retry of uncertain sends.' WHERE event_id=? AND state NOT IN ('UNCERTAIN','DISPATCHING','ACCEPTED')",(now(),e['eventId']))
        result=report(db)
        with db() as c:last=c.execute("SELECT value FROM mail_bridge_meta WHERE key='backup_checked'").fetchone()
        if not last or last[0][:10]!=now()[:10]:
            result['backup']=backup_check(db)
            with db() as c:
                c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('backup_checked',?)",(now(),))
                c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('backup_result',?)",(json.dumps(result['backup']),))
        bridge(store,'POST','/fdgc/v1/report',result)
        with db() as c:c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('last_ok',?)",(now(),))
        return result
    except Exception as ex:
        mode=report(db)['sending']
        failure={'sending':mode,'checkedAt':now(),'detail':'Email processing needs attention. Uncertain sends are held for review.','error':type(ex).__name__}
        with db() as c:c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('last_error',?)",(json.dumps(failure),))
        try:bridge(store,'POST','/fdgc/v1/report',failure)
        except Exception:pass
        return failure
    finally:GUARD.release()
