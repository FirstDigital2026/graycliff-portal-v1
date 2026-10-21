"""One receipt per genuinely new intake job; never scans historical jobs."""
import base64, html, json, urllib.parse
import mail_automation as mail
import graph_import as graph

def initialize(db):
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS intake_receipts (job_key TEXT PRIMARY KEY,message_key TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,draft TEXT,detail TEXT)')

def reserve(db,jobkey,key):
    # Called only immediately after a new operational row is created.
    initialize(db)
    with db() as c:c.execute('INSERT OR IGNORE INTO intake_receipts VALUES (?,?,?,?,?,?)',(jobkey,key,'WAITING_SOURCE',mail.now(),None,'Waiting for the original email and work files to be saved.'))

def draft(jobkey,title,route):
    text="Thank you—we’ve received this job and added it to our work queue.\n\n"+title+"\n\nFirst Digital Operations\n"+mail.SENDER
    body='<p>Thank you—we’ve received this job and added it to our work queue.</p><p>'+html.escape(title)+'</p><hr><img src="'+mail.LOGO+'" width="170" alt="First Digital"><p><strong>First Digital Operations</strong><br>'+mail.SENDER+'</p>'
    subject=route['subject'] if route['subject'].lower().startswith('re:') else 'Re: '+route['subject']
    import hashlib
    return {'subject':subject,'to':route['to'],'cc':route['cc'],'parent':route['messageId'],'references':list(dict.fromkeys(route.get('references',[])+[route['messageId']]))[-30:],'text':text,'html':body,'eventId':'receipt-'+hashlib.sha256(jobkey.encode()).hexdigest(),'title':title,'jobKey':jobkey}

def finish(store,db,token,jobkey,key):
    initialize(db)
    with db() as c:row=c.execute('SELECT * FROM intake_receipts WHERE job_key=?',(jobkey,)).fetchone()
    if not row or row['message_key']!=key:return None
    if row['state'] not in ('WAITING_SOURCE',):return row['state']
    try:
        mail.initialize(db)
        with db() as c:policy=c.execute("SELECT value FROM mail_bridge_meta WHERE key='live_after'").fetchone()
        if not mail.SEND_ARMED or not policy or not mail.after_activation(row['created_at'],policy[0]):
            with db() as c:c.execute("UPDATE intake_receipts SET state='PAUSED',detail='Receipt sending is paused. No automatic release.' WHERE job_key=? AND state='WAITING_SOURCE'",(jobkey,))
            return 'PAUSED'
        import operations_intake as intake
        area,rid=jobkey.split(':');sid=intake.AREA[area.title()]
        sheet=store.get_sheet(sid,force=True);record=next((r for r in sheet['rows'] if str(r['id'])==rid),None)
        if not record:raise ValueError('New job no longer exists.')
        values=intake.vals(sheet,record)
        if values.get('Portal Intake Key')!=key:raise ValueError('Receipt source does not identify this new job.')
        class SourceStore:
            def list_row_attachments(self,sheet_id,row_id):
                return [a for a in store.list_row_attachments(sheet_id,row_id) if a.get('name')=='Email-'+key[:20]+'.eml']
            def download_attachment(self,*args):return store.download_attachment(*args)
        route=mail.resolve_thread(SourceStore(),token,{'sheetId':sid,'rowId':rid})
        message=draft(jobkey,str(values.get('Task Name','')),route)
        with db() as c:
            changed=c.execute("UPDATE intake_receipts SET state='DISPATCHING',draft=?,detail='Receipt claimed before transport.' WHERE job_key=? AND state='WAITING_SOURCE'",(json.dumps(message),jobkey)).rowcount
        if changed!=1:return 'ALREADY_CLAIMED'
        try:
            graph._request('POST',graph.GRAPH_ROOT+'/users/'+urllib.parse.quote(mail.SENDER,safe='')+'/sendMail',token=token,body=base64.b64encode(mail.mime_message(message)),headers={'Content-Type':'text/plain'})
        except Exception:
            with db() as c:c.execute("UPDATE intake_receipts SET state='UNCERTAIN',detail='Provider outcome uncertain. Check Sent Items; no automatic resend.' WHERE job_key=?",(jobkey,))
            return 'UNCERTAIN'
        with db() as c:c.execute("UPDATE intake_receipts SET state='ACCEPTED',detail='Receipt accepted by Microsoft; delivery not confirmed.' WHERE job_key=?",(jobkey,))
        return 'ACCEPTED'
    except Exception:
        with db() as c:c.execute("UPDATE intake_receipts SET state='NEEDS_REVIEW',detail='Original-thread receipt needs review. Job import remains saved; no recipients guessed.' WHERE job_key=? AND state='WAITING_SOURCE'",(jobkey,))
        return 'NEEDS_REVIEW'
