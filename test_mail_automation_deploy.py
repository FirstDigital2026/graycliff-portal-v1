from pathlib import Path
import sys, tempfile, sqlite3, os, json, uuid
from contextlib import contextmanager
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy
import mail_automation as m
m.SEND_ARMED=False  # Isolated fixture; real email transport is always mocked.

scratch=Path(__file__).parent/('qa-mail-'+str(uuid.uuid4()));scratch.mkdir();path=scratch/'qa.sqlite3'
@contextmanager
def db():
 c=sqlite3.connect(path);c.row_factory=sqlite3.Row
 try:
  with c:yield c
 finally:c.close()
m.initialize(db)
event={'eventId':str(uuid.uuid4()),'seq':1,'jobKey':'florence:123','sheetId':'1','rowId':'123','at':m.now(),'action':'status','committed':True,'status':'Working','note':'Splicing underway.','job':{'type':'Outage','title':'TEST ONLY job','city':'Columbia','market':'Florence','incidentId':'TEST-1'},'actor':{'name':'Test Tech','email':'tech@example.com'}}
mail=EmailMessage();mail['From']='Dispatch <dispatch@example.com>';mail['To']='Office <office@firstdigitalsc.com>, Graycliff Jobs <graycliffjobs@firstdigitalsc.com>';mail['Cc']='Coordinator <coord@example.com>';mail['Subject']='Test original outage';mail['Message-ID']='<original@example.com>';mail.set_content('Test fixture only.')
class Store:
 token='fixture-key'
 def list_row_attachments(self,*args):return [{'id':10,'name':'Email-source.eml','createdAt':m.now()}]
 def download_attachment(self,*args):return mail.as_bytes(),'Email-source.eml','message/rfc822'
 def get_sheet(self,*args,**kwargs):return {'columns':[{'id':1,'title':'Status'}],'rows':[{'id':123,'cells':[{'columnId':1,'value':'In Progress'}]}]}
store=Store();route=m.resolve_thread(store,'fake',event);assert route['to']==['dispatch@example.com','office@firstdigitalsc.com'];assert route['cc']==['coord@example.com']
d=m.draft_event(event,route);assert d['subject']=='Re: Test original outage';assert 'Splicing underway.' in d['text'];assert 'graycliff/spectrum field operations' not in d['text'].lower()
message=BytesParser(policy=policy.default).parsebytes(m.mime_message(d));assert message['In-Reply-To']=='<original@example.com>';assert message['From'].addresses[0].addr_spec==m.SENDER;assert str(message['Auto-Submitted'])=='auto-generated'
assert m.eligible(event);assert not m.eligible({**event,'committed':False});assert not m.eligible({**event,'status':'Today'})
complete=m.draft_event({**event,'status':'Field complete'},route);assert complete['text'].startswith('Hands off — job complete\n');assert 'Splicing underway.' in complete['text'];assert 'billing preparation' in complete['text'];assert m.eligible({**event,'job':{'type':'Standard'},'status':'Field complete'})
try:
 m.draft_event({**event,'job':{**event['job'],'type':'CRQ'},'status':'Field complete'},route)
 raise AssertionError('Missing NOC technician accepted')
except m.ThreadReview:pass
noc_preview=m.draft_event({**event,'job':{**event['job'],'type':'CRQ'},'status':'Field complete','nocTechnician':'NOC <Name>'},route)
assert 'NOC ticket closed by: NOC <Name>' in noc_preview['text'] and 'NOC &lt;Name&gt;' in noc_preview['html']
for kind in ['Standard','CRQ']:
 regular={**event,'job':{**event['job'],'type':kind},'status':'Field complete','note':'PRIVATE FIELD NOTE <do not email>','nocTechnician':'Sample NOC Tech'}
 preview=m.draft_event(regular,route)
 assert m.eligible(regular)
 assert 'PRIVATE FIELD NOTE' not in preview['text']+preview['html']
 assert 'Completion findings' not in preview['text']+preview['html']
 assert not m.eligible({**regular,'status':'Working'})
 if kind=='CRQ':
  assert preview['text'].startswith('Hands off — job complete\n')
  assert 'Status: Hands off' in preview['text']
  assert 'billing preparation' not in preview['text']+preview['html']
 else:assert 'Field work complete' in preview['text'] and 'billing preparation' in preview['text']
assert 'Splicing underway.' in complete['text'] and 'Splicing underway.' in complete['html']
try:m.dispatch(db,event['eventId']);raise AssertionError('send guard failed')
except RuntimeError as ex:assert 'disarmed' in str(ex)

# A forward is resolved using its exact parent Message-ID, never quoted body text.
forward=EmailMessage();forward['From']='Office <office@firstdigitalsc.com>';forward['To']=m.SENDER;forward['Subject']='FW: Test original outage';forward['Message-ID']='<forward@example.com>';forward['In-Reply-To']='<original@example.com>';forward.set_content('From: unrelated@example.org (not a routing source)')
mail=forward
m.exact_message=lambda token,box,mid:route if mid=='<original@example.com>' else None
resolved=m.resolve_thread(store,'fake',event)
assert resolved['messageId']=='<original@example.com>'
m.exact_message=lambda *args:None
try:m.resolve_thread(store,'fake',event);raise AssertionError('ambiguous forward routed')
except m.ThreadReview:pass
try:m.validate_route({**route,'messageId':''});raise AssertionError('missing id routed')
except m.ThreadReview:pass

# Full prepare cycle is paused, idempotent, and never calls an email transport.
os.environ['RENDER_SERVICE_ID']=m.OWNER_SERVICE
reports=[]
def bridge(store,method,path,payload=None,after=None):
 if method=='POST':reports.append(payload);return {'ok':True}
 return {'events':[event]}
m.bridge=bridge;m.resolve_thread=lambda *args:route;m.graph.access_token=lambda:'fake-token'
transports=[]
m.graph._request=lambda *args,**kwargs:transports.append((args,kwargs))
m.prepare(store,db);m.prepare(store,db)
with db() as c:
 rows=c.execute('SELECT state FROM mail_event_outbox').fetchall();assert len(rows)==1 and rows[0]['state']=='PAUSED'
assert not transports;assert reports[-1]['sending']=='PAUSED'
m.SEND_ARMED=True
with db() as c:c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('live_after',?)",(m.now(),))
m.prepare(store,db)
assert reports[-1]['sending']=='LIVE' and not transports
with db() as c:assert c.execute('SELECT state FROM mail_event_outbox').fetchone()[0]=='PAUSED'
# Deployment alone cannot release mail, even if a row were marked READY.
with db() as c:
 c.execute("DELETE FROM mail_bridge_meta WHERE key='live_after'")
 c.execute("UPDATE mail_event_outbox SET state='READY',approved_at=?",(m.now(),))
try:m.dispatch(db,event['eventId'],store);raise AssertionError('missing activation gate')
except RuntimeError as ex:assert 'activation' in str(ex) or 'paused' in str(ex)
assert not transports
with db() as c:
 c.execute("UPDATE mail_event_outbox SET state='PAUSED',approved_at=NULL")
 c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('live_after','2000-01-01T00:00:00+00:00')")
m.SEND_ARMED=False
check=m.backup_check(db);assert check['ok'];assert list((path.parent/'graycliff-backups').glob('*.sqlite3'))

# Even if deliberately armed in this isolated fixture, sending requires a
# separately approved READY row, and accepted/uncertain outcomes cannot retry.
m.SEND_ARMED=True
try:
 try:m.dispatch(db,event['eventId'],store);raise AssertionError('paused draft sent')
 except RuntimeError:pass
 with db() as c:c.execute("UPDATE mail_event_outbox SET state='READY',approved_at=?",(m.now(),))
 m.dispatch(db,event['eventId'],store);assert len(transports)==1
 try:m.dispatch(db,event['eventId'],store);raise AssertionError('duplicate send')
 except RuntimeError:pass
 assert len(transports)==1
 with db() as c:c.execute("UPDATE mail_event_outbox SET state='READY',approved_at=?",(m.now(),))
 def fail(*args,**kwargs):raise TimeoutError('simulated')
 m.graph._request=fail
 try:m.dispatch(db,event['eventId'],store)
 except TimeoutError:pass
 with db() as c:assert c.execute('SELECT state FROM mail_event_outbox').fetchone()[0]=='UNCERTAIN'
 with db() as c:
  c.execute("UPDATE mail_event_outbox SET state='NEW'")
  c.execute("INSERT OR REPLACE INTO mail_bridge_meta VALUES('live_after','2000-01-01T00:00:00+00:00')")
 m.prepare(store,db)
 with db() as c:assert c.execute('SELECT state FROM mail_event_outbox').fetchone()[0]=='UNCERTAIN'
finally:m.SEND_ARMED=False
for f in (scratch/'graycliff-backups').glob('*'):f.unlink()
(scratch/'graycliff-backups').rmdir();path.unlink();scratch.rmdir()
print('PASS: original-thread resolution, forwarded-parent lookup, recipient handling, completion wording, paused ingestion, idempotency, disabled sends, explicit release, uncertain-send protection, backup and isolated restore.')
