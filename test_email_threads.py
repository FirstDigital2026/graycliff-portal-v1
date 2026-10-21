import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import operations_intake as op
class Store:
 def __init__(self):
  names=list(dict.fromkeys(['Task Name','City','Status','Due Date','Notes','Portal Billing Status','Portal Office Due Date','Portal Planned Date',*op.EXTRA]));self.columns=[{'id':i+1,'title':n}for i,n in enumerate(names)];self.comments=[];self.files=[];self.created=0
  self.row={'id':99,'cells':[{'columnId':c['id'],'value':{'Task Name':'901 Fictional Ct','City':'Elgin','Status':'In Progress','Portal Project ID':7999991.0,'Portal Work Order':3999991.0,'Portal Job Type':'Standard','Due Date':'2026-10-20','Portal Office Due Date':'2026-10-19','Portal Planned Date':'2026-10-18','Portal Email Threads':json.dumps(['known-chain'])}.get(c['title'],'')}for c in self.columns]}
 def get_sheet(self,sid,force=False):return {'columns':self.columns,'rows':[self.row]if sid==op.AREA['Columbia']else []}
 def _request(self,method,path,**kw):
  if method=='GET':
   assert kw['query']['include']=='comments';return {'data':self.list_row_discussions()}
  assert method=='PUT'; values={c['columnId']:c['value']for c in kw['body'][0]['cells']}
  for c in self.row['cells']:
   if c['columnId']in values:c['value']=values[c['columnId']]
  return {'result':[self.row]}
 def invalidate(self,*a):pass
 def list_row_attachments(self,*a):return [{'name':n}for n in self.files]
 def attach_file_to_row(self,*a,filename,**kw):self.files.append(filename)
 def list_row_discussions(self,*a):return [{'comments':[{'text':t}for t in self.comments]}]
 def add_row_comment(self,*a):raise AssertionError('Incoming mail triggered the legacy comment-to-Complete workflow')
op.graph.get_message_mime=lambda *a:b'fake original email'
s=Store();m={'id':'m1','conversationId':'known-chain','subject':'Re: PID#7999991 - 901 Fictional Ct','body':{'content':'Use the rear entrance. Updated map attached.'},'receivedDateTime':'2026-10-14T20:00:00Z'}
item=op.message_item(s,m,[]);item['wo']='3999991';state={'created':0,'updated':0};assert op.save_message(s,'',m,item,[],'reply1',state)=='columbia:99';v=op.vals(s.get_sheet(op.AREA['Columbia']),s.row);assert v['Status']=='In Progress'and v['Portal Office Due Date']=='2026-10-19'and v['Portal Planned Date']=='2026-10-18';assert len(s.comments)==0;op.save_message(s,'',m,item,[],'reply1',state);assert len(s.comments)==0 and len(s.files)==1
op.save_message(s,'',m,dict(item,ntp_due='2026-10-25'),[],'reply1',state)
v=op.vals(s.get_sheet(op.AREA['Columbia']),s.row)
assert v['Due Date']=='2026-10-24' and v['Portal NTP Due Date']=='2026-10-25'
assert v['Portal Office Due Date']=='2026-10-19' and v['Portal Planned Date']=='2026-10-18'
for c in s.row['cells']:
 if next(x['title']for x in s.columns if x['id']==c['columnId'])=='Portal Job Type':c['value']=''
op.save_message(s,'',m,dict(item,type='Outage'),[],'reply1',state)
assert op.vals(s.get_sheet(op.AREA['Columbia']),s.row)['Portal Job Type']=='Standard'
bad=dict(item,pid='9999999')
try:op.save_message(s,'',m,bad,[],'bad',state);raise AssertionError('Conflict accepted')
except ValueError:pass
for c in s.row['cells']:
 if next(x['title']for x in s.columns if x['id']==c['columnId'])=='Status':c['value']='Complete'
op.save_message(s,'',dict(m,id='m2'),item,[],'reply2',state);assert op.vals(s.get_sheet(op.AREA['Columbia']),s.row)['Status']=='Complete'
try:op.message_item(s,dict(m,conversationId='unknown',subject='More details'),[]);raise AssertionError('Unknown chain accepted')
except ValueError:pass
print('Same-chain replies, conflict review, deduplication, completed preservation and office schedule preservation passed.')
import sqlite3,tempfile,hashlib,os
fd,path=tempfile.mkstemp();os.close(fd)
from contextlib import contextmanager
@contextmanager
def db():
 c=sqlite3.connect(path);c.row_factory=sqlite3.Row
 try:
  with c:yield c
 finally:c.close()
with db()as c:
 c.execute('CREATE TABLE operations_mail (message_key TEXT PRIMARY KEY, job_key TEXT, state TEXT)');c.execute('INSERT INTO operations_mail VALUES (?,?,?)',(hashlib.sha256(b'old-message-id').hexdigest(),'columbia:99','Imported'))
op.graph.mailbox=lambda:'fake@example.invalid'
op.graph._request=lambda *a,**k:{'value':[{'id':'old','internetMessageId':'old-message-id','conversationId':'previous-chain'}]}
op.seed_imported_threads(s,'fake-token','fake-folder',db)
assert 'previous-chain'in json.loads(op.vals(s.get_sheet(op.AREA['Columbia']),s.row)['Portal Email Threads'])
op.graph._request=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Seed ran twice'))
op.seed_imported_threads(s,'fake-token','fake-folder',db)
os.unlink(path)
print('Previously imported thread backfill and one-time migration passed.')

archived=Store()
original_get=archived.get_sheet
archived.get_sheet=lambda sid,force=False:{'columns':archived.columns,'rows':[archived.row]if sid==op.SEARCH_SHEETS['History']else []}
archived_item=op.message_item(archived,m,[])
assert op.save_message(archived,'',m,archived_item,[],'archive-reply',{'created':0,'updated':0})=='history:99'
assert op.vals(archived.get_sheet(op.SEARCH_SHEETS['History']),archived.row)['Due Date']=='2026-10-20'
print('Archived job replies remain attached to history; no active job created.')
