import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import operations_intake as op
class Store:
 def __init__(self):
  names=list(dict.fromkeys(['Task Name','City','Status','Due Date','Notes','Portal Billing Status','Portal Office Due Date','Portal Planned Date',*op.EXTRA]));self.columns=[{'id':i+1,'title':n}for i,n in enumerate(names)];self.comments=[];self.files=[];self.created=0
  self.row={'id':99,'cells':[{'columnId':c['id'],'value':{'Task Name':'5033 Erwin Ct','City':'Elgin','Status':'In Progress','Portal Project ID':7021433.0,'Portal Work Order':3721849.0,'Portal Job Type':'Standard','Due Date':'2026-10-20','Portal Office Due Date':'2026-10-19','Portal Planned Date':'2026-10-18','Portal Email Threads':json.dumps(['known-chain'])}.get(c['title'],'')}for c in self.columns]}
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
 def add_row_comment(self,*a):raise AssertionError('Email must not trigger a completion comment')
op.graph.get_message_mime=lambda *a:b'fake original email'
s=Store();m={'id':'m1','conversationId':'known-chain','subject':'Re: PID#7021433 - 5033 Erwin Ct','body':{'content':'Use the rear entrance. Updated map attached.'},'receivedDateTime':'2026-10-14T20:00:00Z'}
item=op.message_item(s,m,[]);item['wo']='3721849';state={'created':0,'updated':0};assert op.save_message(s,'',m,item,[],'reply1',state)=='columbia:99';v=op.vals(s.get_sheet(op.AREA['Columbia']),s.row);assert v['Status']=='In Progress'and v['Portal Office Due Date']=='2026-10-19'and v['Portal Planned Date']=='2026-10-18';assert len(s.comments)==0;op.save_message(s,'',m,item,[],'reply1',state);assert len(s.comments)==0 and len(s.files)==1
bad=dict(item,pid='9999999')
try:op.save_message(s,'',m,bad,[],'bad',state);raise AssertionError('Conflict accepted')
except ValueError:pass
for c in s.row['cells']:
 if next(x['title']for x in s.columns if x['id']==c['columnId'])=='Status':c['value']='Complete'
op.save_message(s,'',dict(m,id='m2'),item,[],'reply2',state);assert op.vals(s.get_sheet(op.AREA['Columbia']),s.row)['Status']=='Complete'
try:op.message_item(s,dict(m,conversationId='unknown',subject='More details'),[]);raise AssertionError('Unknown chain accepted')
except ValueError:pass
print('Same-chain replies, conflict review, deduplication, completed preservation and office schedule preservation passed.')

from email.message import EmailMessage
import base64
mail=EmailMessage();mail['Subject']='Re: More details';mail.set_content('Work photos included');mail.add_alternative('<html><img src="cid:work-photo"><img src="data:image/png;base64,'+base64.b64encode(b'data-uri-photo').decode()+'"></html>',subtype='html');mail.get_payload()[1].add_related(b'inline-work-photo',maintype='image',subtype='png',cid='<work-photo>',filename='work-photo.png',disposition='inline');mail.add_attachment(b'PDF content',maintype='application',subtype='pdf',filename='updated-map.pdf')
raw=mail.as_bytes();parts=op.source_attachments(raw);assert any(n=='work-photo.png'and d==b'inline-work-photo'for n,_,d in parts);assert any(n=='updated-map.pdf'for n,_,_ in parts);assert any(d==b'data-uri-photo'for _,_,d in parts)
s=Store();op.graph.get_message_mime=lambda *a:raw;m['hasAttachments']=False;item=op.message_item(s,m,[]);op.save_message(s,'',m,item,[],'inline-only',{'created':0,'updated':0});assert len(s.files)==4,s.files
before=list(s.files);op.save_message(s,'',m,item,[],'inline-only',{'created':0,'updated':0});assert s.files==before
attachment={'@odata.type':'#microsoft.graph.fileAttachment','name':'work-photo.png','isInline':True,'contentBytes':base64.b64encode(b'revised-work-photo').decode(),'contentType':'image/png'};op.save_message(s,'',m,item,[attachment],'revision',{'created':0,'updated':0});assert len([f for f in s.files if f.startswith('work-photo-')])==2
responses=[{'value':[{'name':'page1'}],'@odata.nextLink':op.graph.GRAPH_ROOT+'/next'},{'value':[{'name':'page2'}]}];op.graph._request=lambda *a,**k:responses.pop(0);op.graph.mailbox=lambda:'fake@example.invalid';assert len(op.graph.list_attachments('fake','fake-message'))==2
print('Inline/CID pictures, HTML data images, documents, inline-only mail, revised photo preservation and attachment pagination passed.')
