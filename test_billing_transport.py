import base64
import json
import hashlib
import unittest
from email.message import EmailMessage
from unittest.mock import patch
import billing_transport as b


class Graph:
    GRAPH_ROOT='https://graph.microsoft.com/v1.0'
    contents={}
    def __init__(self):self.calls=[];self.files=[];self.fail=None;self.to='test@example.invalid'
    def access_token(self):return 'fictional-token'
    def _request(self,method,url,**kw):
        self.calls.append((method,url,kw))
        if self.fail and (url.endswith('/send') if self.fail=='/send' else self.fail in url):raise TimeoutError('fictional failure')
        if method=='POST' and url.endswith('/messages'):return {'id':'test-draft','isDraft':True}
        if url.endswith('/attachments/createUploadSession'):return {'uploadUrl':'https://outlook.office.com/TEST-ONLY','nextExpectedRanges':['0-']}
        if method=='POST' and url.endswith('/attachments'):
            body=json.loads(kw['body']);raw=base64.b64decode(body['contentBytes']);aid='file'+str(len(self.files));Graph.contents[aid]=raw;self.files.append({'id':aid,'name':body['name'],'size':len(raw)+219});return {'id':aid}
        if method=='GET' and '/attachments?' in url:return {'value':self.files}
        if method=='GET':return {'isDraft':True,'toRecipients':[{'emailAddress':{'address':self.to}}],'ccRecipients':[],
                                  'subject':'Re: TEST ONLY','internetMessageHeaders':[{'name':'In-Reply-To','value':'<test-parent@example.invalid>'}]}
        return {}


def draft():
    m=EmailMessage();m['To']='test@example.invalid';m['Subject']='Re: TEST ONLY';m['In-Reply-To']='<test-parent@example.invalid>';m.set_content('Fictional test only')
    return m.as_bytes()


class Tests(unittest.TestCase):
    def setUp(self):
        Graph.contents={}
        def digest(url,token):
            raw=Graph.contents[url.split('/')[-2]];return len(raw),hashlib.sha256(raw).hexdigest()
        self.digest=patch.object(b,'attachment_digest',side_effect=digest);self.digest.start();self.addCleanup(self.digest.stop)
    def test_small_attachments_in_large_package_sent_once(self):
        g=Graph();f={'name':'TEST.xlsx','size':10,'data':b'PKtestdata'}
        b.send_large(g,'sender@example.invalid',draft(),[f]);self.assertEqual(sum(u.endswith('/send') for m,u,k in g.calls),1)
        self.assertEqual(g.calls[0][2]['headers']['Content-Type'],'text/plain')
    def test_upload_chunks_preserve_large_bytes(self):
        g=Graph();raw=b'0123456789'*400000;f={'name':'TEST.xlsm','size':len(raw),'data':raw};chunks=[]
        def put(url,data,start,total):
            chunks.append(data)
            if start+len(data)==total:
                Graph.contents['large']=raw;g.files.append({'id':'large','name':f['name'],'size':total+219})
        with patch.object(b,'put_chunk',side_effect=put):b.send_large(g,'sender@example.invalid',draft(),[f])
        self.assertEqual(b''.join(chunks),raw);self.assertLessEqual(max(map(len,chunks)),b.CHUNK)
    def test_upload_failure_does_not_send(self):
        g=Graph();g.fail='/attachments';f={'name':'TEST.xlsx','size':2,'data':b'PK'}
        with self.assertRaises(b.BeforeSendFailure):b.send_large(g,'sender@example.invalid',draft(),[f])
        self.assertFalse(any(u.endswith('/send') for m,u,k in g.calls));self.assertTrue(any(m=='DELETE' for m,u,k in g.calls))
    def test_send_timeout_is_not_retryable_upload_error(self):
        g=Graph();g.fail='/send';f={'name':'TEST.xlsx','size':2,'data':b'PK'}
        with self.assertRaises(TimeoutError):b.send_large(g,'sender@example.invalid',draft(),[f])
        self.assertEqual(sum(u.endswith('/send') for m,u,k in g.calls),1)
        self.assertFalse(any(m=='DELETE' for m,u,k in g.calls))
    def test_changed_recipients_block_send(self):
        g=Graph();g.to='changed@example.invalid';f={'name':'TEST.xlsx','size':2,'data':b'PK'}
        with self.assertRaises(b.BeforeSendFailure):b.send_large(g,'sender@example.invalid',draft(),[f])
        self.assertFalse(any(u.endswith('/send') for m,u,k in g.calls))
    def test_changed_upload_bytes_block_send(self):
        g=Graph();f={'name':'TEST.xlsx','size':2,'data':b'PK'}
        with patch.object(b,'attachment_digest',return_value=(2,'wrong-digest')):
            with self.assertRaises(b.BeforeSendFailure):b.send_large(g,'sender@example.invalid',draft(),[f])
        self.assertFalse(any(u.endswith('/send') for m,u,k in g.calls))
    def test_unsupported_upload_url_has_no_network(self):
        with patch.object(b.urllib.request,'build_opener') as opener:
            with self.assertRaises(ValueError):b.put_chunk('https://untrusted.invalid/upload',b'x',0,1)
            opener.assert_not_called()
    def test_package_limit_before_network(self):
        g=Graph()
        with self.assertRaises(ValueError):b.send_large(g,'sender@example.invalid',draft(),[{'name':'TEST','size':b.MAX_FILE+1,'data':b''}])
        self.assertFalse(g.calls)


if __name__=='__main__':unittest.main()
